"""cave_replay_studio 时间轴：录制视口要跟着鼠标轨迹一起带出。"""
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


if __name__ == '__main__':
    unittest.main()
