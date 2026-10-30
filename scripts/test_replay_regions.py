"""回放分区：有 ui.state 时按实际面板；旧包退回 4panel 三块 2D。"""
import unittest

import replay_manual_session as rms

T0 = 1_700_000_000_000


def _pack(panels, layout='4panel'):
    return {
        'events': [
            {'type': 'session.start', 't_wall': T0,
             'viewport': {'css_px': [1280, 740]}},
            {'type': 'ui.state', 't_wall': T0 + 500,
             'ui': {'panels': panels}},
        ],
    }


def _spec(pack, layout='4panel', compare_all=False):
    return rms._region_specs(pack, {'t': T0 + 1000,
                                   'state': {'layout': layout}},
                             compare_all=compare_all)


class TestRegions(unittest.TestCase):
    PANELS = [[0, 45, 640, 348], [640, 45, 640, 348],
              [0, 392, 640, 348], [640, 392, 640, 348]]

    def test_four_panel_excludes_bottom_left_3d(self):
        specs = _spec(_pack(self.PANELS))
        self.assertEqual([s['kind'] for s in specs], ['2d', '2d', '2d'])
        self.assertEqual(len(specs), 3)

    def test_compare_all_keeps_every_panel(self):
        specs = _spec(_pack(self.PANELS), compare_all=True)
        self.assertEqual(len(specs), 4)

    def test_closed_panel_changes_regions(self):
        # 关掉左下 3D 后只剩三块 2D
        specs = _spec(_pack(self.PANELS[:2] + self.PANELS[3:]))
        self.assertEqual(len(specs), 3)
        # 关掉左上 2D 后，左下 3D 仍被排除，只剩两块 2D
        specs = _spec(_pack(self.PANELS[1:]))
        self.assertEqual(len(specs), 2)

    def test_single_2d_layout_uses_its_panel(self):
        specs = _spec(_pack([[0, 0, 1280, 740]]), layout='xy')
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]['kind'], '2d')

    def test_old_pack_falls_back_to_three_2d_regions(self):
        specs = _spec({'events': []})
        self.assertEqual(len(specs), 3)
        self.assertEqual([s['label'] for s in specs],
                         ['2d_tl', '2d_tr', '2d_br'])


if __name__ == '__main__':
    unittest.main()
