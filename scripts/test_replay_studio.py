"""cave_replay_studio 时间轴与采集任务：录制视口要跟着鼠标轨迹一起带出；
采集窗口开不起来时任务要收尾（不能一直占着 busy）。"""
import tempfile
import unittest
from pathlib import Path

import cave_replay_studio as cs

T0 = 1_700_000_000_000


class TestTimeline(unittest.TestCase):
    def test_keeps_recorded_viewport_for_mouse_mapping(self):
        pack = {
            'session_id': 's1', 'time': {'start_wall_ms': T0},
            'notes': [], 'shots': [],
            'events': [
                {'type': 'session.start', 't_wall': T0,
                 'viewport': {'css_px': [1280, 740], 'dpr': 1}},
                {'type': 'viewer.state-change', 't_wall': T0 + 1000,
                 'seq': 1, 'state_sha': 'a',
                 'state': {'position': [1, 2, 3], 'layers': []}},
                {'type': 'input.pointer', 't_wall': T0 + 1100,
                 'phase': 'pointermove', 'x': 1200, 'y': 700},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tl = cs.build_timeline(pack, Path(tmp))
        self.assertEqual(tl['viewport'], [1280.0, 740.0])
        self.assertEqual(tl['mouse'][-1]['x'], 1200.0)


class TestCollectOpenFailure(unittest.TestCase):
    """子进程开不了浏览器时会自己挂着等 --cdp/油猴；工作台里得有人收尾。"""

    class _Proc:
        def __init__(self, lines):
            self.stdout = iter(lines)
            self.killed = False

        def kill(self):
            self.killed = True

        def poll(self):
            return None if not self.killed else 0

    def _run_pump(self, line):
        r = cs.Runner(None)
        r.kind = 'collect'
        r.proc = self._Proc([line, '[open] 服务继续运行\n'])
        r._pump()
        return r

    def test_missing_browser_closes_task_with_hint(self):
        r = self._run_pump('[open] 打开浏览器失败：BrowserType.launch: '
                           "Executable doesn't exist at "
                           'C:/x/ms-playwright/chromium-1243/chrome.exe\n')
        self.assertTrue(r.proc.killed)
        self.assertIn('打开浏览器失败', r.open_failed)
        self.assertIn('playwright install chromium', r.open_hint)
        self.assertFalse(r.status()['running'])

    def test_missing_module_closes_task_with_pip_hint(self):
        r = self._run_pump('[open] 打开浏览器失败：No module named playwright\n')
        self.assertTrue(r.proc.killed)
        self.assertIn('requirements.txt', r.open_hint)

    def test_normal_close_does_not_flag_failure(self):
        r = cs.Runner(None)
        r.kind = 'collect'
        r.proc = self._Proc(['[open] 浏览器已打开\n', '[open] 结束，服务退出。\n'])
        r._pump()
        self.assertEqual(r.open_failed, '')
        self.assertEqual(r.open_hint, '')
        self.assertFalse(r.proc.killed)


if __name__ == '__main__':
    unittest.main()
