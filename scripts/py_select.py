#!/usr/bin/env python3
"""解释器选择：挑一个“能起采集窗口”的 python，而不是只看它能不能 import。

为什么要单独一个模块：采集窗口、回放、工作台子进程都由 `sys.executable` 决定，
也就是“用哪个 python 起的”就用哪个。机器上常常同时有系统 python、Anaconda、
项目 `.venv`，它们各自有各自的包和各自的 Playwright 浏览器缓存（浏览器按版本
分目录，`chromium-1243` 与 `chromium-1248` 互不相认）。只看 `import` 成功不够：
装了 playwright 但没装对应版本浏览器的解释器，一样开不出窗口。

选择顺序（前面的命中即停，都可以用环境变量覆盖）：
    $NM_PYTHON → $PYTHON → 当前解释器 → 项目 .venv / venv → 上一级 .venv-* → python3 → python

用法：
    python3 scripts/nx.py doctor          # 入口已经自动做这件事
    from py_select import pick
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 一次子进程里问完三件事：能不能 import、能不能问到浏览器路径、浏览器文件在不在。
PROBE = """
import os, sys
try:
    import neuroglancer, playwright
except Exception as err:
    print('import:' + str(err).splitlines()[0]); sys.exit(2)
try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        p = pw.chromium.executable_path
except Exception as err:
    print('playwright:' + str(err).splitlines()[0][:120]); sys.exit(3)
if not p or not os.path.exists(p):
    print('browser:' + str(p)); sys.exit(4)
print('browser:' + str(p))
"""

# 只看依赖、不看浏览器：用于那些根本不开窗口的命令（看包 / 导出 CSV / doctor）。
PROBE_IMPORT = """
import sys
try:
    import neuroglancer, playwright
except Exception as err:
    print('import:' + str(err).splitlines()[0]); sys.exit(2)
print('imports:ok')
"""

# 探测失败原因 → 该解释器怎么修
FIX_PIP = '%s -m pip install -r requirements.txt'
FIX_BROWSER = '%s -m playwright install chromium'


def _exe_in(venv_dir):
    """虚拟环境里的 python 可执行文件（Windows 在 Scripts\\，其它系统在 bin/）。"""
    for rel in (('Scripts', 'python.exe'), ('Scripts', 'python'),
                ('bin', 'python'), ('bin', 'python3')):
        f = Path(venv_dir).joinpath(*rel)
        if f.exists():
            return str(f)
    return None


def candidates(root=None, env=None, current=None):
    """按优先级列出候选解释器绝对路径（去重，保持顺序）。"""
    env = os.environ if env is None else env
    root = Path(root) if root else ROOT
    out = []

    def push(v):
        if not v:
            return
        v = str(Path(os.path.expanduser(v))) if os.path.sep in v or ':' in v \
            else (shutil.which(v) or v)
        if v and v not in out and Path(v).exists():
            out.append(v)

    push(env.get('NM_PYTHON'))
    push(env.get('PYTHON'))
    push(current or sys.executable)
    for name in ('.venv', 'venv'):
        push(_exe_in(root / name))
    for d in sorted(root.parent.glob('.venv-*'))[:4]:
        push(_exe_in(d))
    for name in ('python3', 'python'):
        push(shutil.which(name))
    return out


def probe(exe, timeout=60, need_browser=True):
    """问一个解释器“你能不能起采集窗口”，返回 (能不能, 原因文本)。

    `-X utf8` 是给被问的那个解释器的：不指定的话它按本机 locale 写 stdout
    （中文 Windows 是 cp936），与下面按 UTF-8 读对不上，原因里的路径会变乱码。
    """
    code = PROBE if need_browser else PROBE_IMPORT
    try:
        r = subprocess.run([exe, '-X', 'utf8', '-c', code],
                           capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as err:
        return False, 'run:%s' % str(err)[:120]
    detail = (r.stdout or '').strip().splitlines()
    if r.returncode == 0:
        return True, (detail[-1] if detail else 'ok')
    return False, (detail[-1] if detail else 'exit %d' % r.returncode)


def fix_commands(exe, why=''):
    """针对某个解释器的缺失给出可直接执行的命令（原因文本决定给哪条）。

    `run:` 是连那个解释器都跑不起来（权限、进程保护），不是缺依赖，
    这时候给人装依赖的命令只会把人往错的方向带，所以不给建议。
    """
    if why.startswith('run:'):
        return []
    if why.startswith('browser:'):
        return [FIX_BROWSER % exe]
    if why.startswith('playwright:'):
        return [FIX_BROWSER % exe]
    return [FIX_PIP % exe, FIX_BROWSER % exe]


def pick(root=None, env=None, prober=None, current=None, need_browser=True):
    """选出第一个可用的解释器（need_browser=False 时只看依赖）。

    返回 {'exe': 路径或 None, 'why': 选中原因或第一个失败原因,
          'tried': [(exe, ok, why)], 'fixes': [建议命令]}
    """
    if prober is None:
        def prober(exe):
            return probe(exe, need_browser=need_browser)
    tried = []
    for exe in candidates(root=root, env=env, current=current):
        ok, why = prober(exe)
        tried.append((exe, ok, why))
        if ok:
            return {'exe': exe, 'why': why, 'tried': tried, 'fixes': []}
    first = tried[0] if tried else (None, False, 'no-candidate')
    return {'exe': None, 'why': first[2], 'tried': tried,
            'fixes': fix_commands(first[0], first[2]) if first[0] else
                    [FIX_PIP % 'python3', FIX_BROWSER % 'python3']}


def main(argv=None):
    """命令行看结果：`python3 scripts/py_select.py`（打印选择过程，不改任何东西）。"""
    chosen = pick()
    for exe, ok, why in chosen['tried']:
        print('[%s] %s  %s' % ('OK ' if ok else 'no', exe, why[:120]))
    if chosen['exe']:
        print('[python] 选中 %s' % chosen['exe'])
        return 0
    print('[python] 没有可用解释器，先装依赖：')
    for c in chosen['fixes']:
        print('   ' + c)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
