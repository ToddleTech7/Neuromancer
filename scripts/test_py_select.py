"""换台机器也能跑：解释器选择与管道编码。

采集窗口能不能开，取决于用哪个 python 起工作台；日志会不会乱码，取决于
管道两端是不是同一个编码。中文 Windows 上这两件事都容易碎，所以钉成测试：
候选顺序与去重、按“能不能起窗口”挑一个、选不出来时给的是可执行命令，
下游两个入口（nx 与工作台）确实跟着选中的解释器走，以及
读子进程管道的地方都显式写了 UTF-8、requirements.txt 不带非 ASCII 字符。
"""
import io
import os
import re
import sys
import unittest
from pathlib import Path

import nx
import py_select as ps

SCRIPTS = Path(ps.__file__).resolve().parent


class TestCandidates(unittest.TestCase):
    def test_env_override_first_and_dedup(self):
        cur = sys.executable
        env = {'NM_PYTHON': cur, 'PYTHON': cur}
        got = ps.candidates(env=env, current=cur)
        self.assertEqual(got[0], cur)          # 显式指定的排第一
        self.assertEqual(len(got), len(set(got)))   # 同一个解释器不重复出现

    def test_project_venv_is_a_candidate(self):
        root = Path(ps.__file__).resolve().parent.parent
        env = {'NM_PYTHON': sys.executable}
        # 没有 .venv 的机器上也不报错；有 .venv 时它必须在候选里
        exe = ps._exe_in(root / '.venv')
        got = ps.candidates(root=root, env=env, current=sys.executable)
        if exe:
            self.assertIn(exe, got)

    def test_relative_and_tilde_paths_resolved(self):
        home = os.path.expanduser('~')
        env = {'NM_PYTHON': '~/some-missing-python'}
        self.assertNotIn(os.path.join(home, 'some-missing-python'),
                         ps.candidates(env=env, current=sys.executable))


class TestPick(unittest.TestCase):
    def test_picks_first_usable_and_stops(self):
        seen = []

        def prober(exe):
            seen.append(exe)
            return (len(seen) >= 2, 'browser:ok' if len(seen) >= 2 else 'import:no')

        got = ps.pick(prober=prober, current=sys.executable)
        self.assertTrue(got['exe'])
        self.assertEqual(len(seen), 2)         # 命中之后不再试后面的
        self.assertEqual(got['fixes'], [])

    def test_no_usable_interpreter_gives_commands(self):
        got = ps.pick(prober=lambda exe: (False, 'browser:None'),
                      current=sys.executable)
        self.assertIsNone(got['exe'])
        self.assertTrue(any('playwright install chromium' in c for c in got['fixes']))

    def test_fix_commands_match_the_reason(self):
        self.assertEqual(ps.fix_commands('/p', 'browser:None'),
                         ['/p -m playwright install chromium'])
        self.assertEqual(ps.fix_commands('/p', 'playwright:boom'),
                         ['/p -m playwright install chromium'])
        self.assertEqual(len(ps.fix_commands('/p', 'import:No module named')), 2)

    def test_probe_current_interpreter_returns_pair(self):
        ok, why = ps.probe(sys.executable)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(why, str)


class TestDownstreamFollows(unittest.TestCase):
    """nx 选了哪个，工作台与它的采集子进程就用哪个。"""

    def setUp(self):
        self._saved = (os.environ.get('NM_PYTHON'), nx.PY)

    def tearDown(self):
        py, nxpy = self._saved
        if py is None:
            os.environ.pop('NM_PYTHON', None)
        else:
            os.environ['NM_PYTHON'] = py
        nx.PY = nxpy

    def test_nx_switches_interpreter_and_publishes_it(self):
        real = ps.pick
        try:
            ps.pick = lambda need_browser=True: {
                'exe': sys.executable, 'why': 'browser:ok',
                'tried': [(sys.executable, True, 'ok')], 'fixes': []}
            nx.PY = '/should/not/be/used'
            chosen = nx.choose_py('collect')
            self.assertEqual(chosen, sys.executable)
            self.assertEqual(os.environ['NM_PYTHON'], sys.executable)
            self.assertEqual(nx.PY, chosen)
        finally:
            ps.pick = real

    def test_nx_stops_before_opening_a_window_it_cannot_get(self):
        real = ps.pick
        try:
            ps.pick = lambda need_browser=True: {
                'exe': None, 'why': 'browser:None',
                'tried': [(sys.executable, False, 'browser:None')],
                'fixes': [sys.executable + ' -m playwright install chromium']}
            with self.assertRaises(SystemExit):
                nx.choose_py('collect')
            # 不开窗口的命令不该拦人：看包在没有浏览器的机器上照样能看
            self.assertEqual(nx.choose_py('pack'), nx.PY)
        finally:
            ps.pick = real

    def test_studio_uses_nm_python_when_it_exists(self):
        import cave_replay_studio as cs
        saved = os.environ.get('NM_PYTHON')
        try:
            os.environ['NM_PYTHON'] = sys.executable
            self.assertEqual(cs._child_py(), sys.executable)
            os.environ['NM_PYTHON'] = sys.executable + '-not-here'
            self.assertEqual(cs._child_py(), sys.executable)   # 不存在就回退
        finally:
            if saved is None:
                os.environ.pop('NM_PYTHON', None)
            else:
                os.environ['NM_PYTHON'] = saved


class TestUtf8Pipes(unittest.TestCase):
    """子进程管道两端必须同一个编码：中文 Windows 缺省是 cp936，不是 UTF-8。

    只给“被我们读进管道的子进程”强制 UTF-8：整个进程的环境变量会影响
    它往终端写的方式，中文 PowerShell 会拿 cp936 去解 UTF-8，反而满屏乱码。
    例外是离线自检入口：它把每步输出都收进自己的管道，所以直接改了进程环境。
    """

    def _calls(self, text):
        """找出所有带管道的 subprocess 调用（括号配对，不靠行尾截断）。"""
        out = []
        for m in re.finditer(r'subprocess\.(Popen|run|check_output)\(', text):
            depth, i = 0, m.start() + len(m.group()) - 1
            while i < len(text):
                if text[i] == '(':
                    depth += 1
                elif text[i] == ')':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            head = re.findall(r'^def (\w+)', text[:m.start()], re.M)
            out.append((head[-1] if head else '<module>',
                        ' '.join(text[m.start():i + 1].split())))
        return out

    def test_pipe_calls_agree_on_encoding(self):
        # 选目录弹的是系统原生对话框，输出用宿主自己的控制台编码，不在这条规则里
        native = ('osascript', 'powershell', 'zenity', 'kdialog')
        for f in sorted(SCRIPTS.glob('*.py')):
            text = io.open(f, encoding='utf-8').read()
            pinned = "os.environ['PYTHONUTF8']" in text
            for func, call in self._calls(text):
                if 'PIPE' not in call and 'capture_output' not in call:
                    continue
                if func == 'pick_directory' or any(a in call for a in native):
                    continue
                where = '%s：%s' % (f.name, call[:80])
                self.assertIn('encoding', call, '读管道没写 encoding（' + where + '）')
                if pinned or "'-X', 'utf8'" in call:
                    continue          # 子进程那边已经按 UTF-8 写
                self.assertIn('env', call,
                              '子进程没被要求按 UTF-8 写（' + where + '）')

    def test_probe_runs_the_asked_interpreter_in_utf8(self):
        # py_select 不起常驻子进程，但问话的那个解释器也得按 UTF-8 写 stdout
        text = io.open(SCRIPTS / 'py_select.py', encoding='utf-8').read()
        self.assertRegex(text, r"'-X', 'utf8', '-c'")

    def test_requirements_are_ascii(self):
        # pip 按系统 locale 读 requirements（中文 Windows 是 cp936），
        # 非 ASCII 注释会让 pip 直接报错，而写文档的人自己机器上看不出来
        for name in ('requirements.txt', 'requirements-dev.txt'):
            raw = (SCRIPTS.parent / name).read_bytes()
            bad = [b for b in raw if b > 127]
            self.assertEqual(bad, [], '%s 含非 ASCII 字节' % name)


if __name__ == '__main__':
    unittest.main()
