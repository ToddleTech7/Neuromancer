"""replay_state 单元测试（纯离线，不碰浏览器）。"""

import unittest

import replay_state as rs


def st(**kw):
    base = {
        'position': [190587, 114102, 21773],
        'crossSectionScale': 6,
        'projectionScale': 524288,
        'layout': '4panel',
        'dimensions': {'x': [4e-09, 'm'], 'y': [4e-09, 'm'],
                       'z': [4e-08, 'm']},
        'layers': [
            {'name': 'em', 'type': 'image',
             'source': 'precomputed://em'},
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': []},
        ],
    }
    base.update(kw)
    return base


class TestFingerprint(unittest.TestCase):
    def test_state_fields(self):
        fp = rs.fingerprint(st())
        self.assertEqual(fp['position'], [190587.0, 114102.0, 21773.0])
        self.assertEqual(fp['crossSectionScale'], 6.0)
        self.assertEqual(fp['layout'], '4panel')
        self.assertEqual(fp['dimensions']['x'], 4e-09)
        self.assertEqual([l['name'] for l in fp['layers']], ['autoseg', 'em'])
        # 缺省即默认：state 里没有 visible，就等于 True
        self.assertTrue(all(l['visible'] for l in fp['layers']))
        self.assertEqual([l['opacity'] for l in fp['layers']], [1.0, 1.0])

    def test_absent_fields_are_none(self):
        fp = rs.fingerprint({'position': [1, 2, 3]})
        self.assertIsNone(fp['crossSectionScale'])
        self.assertIsNone(fp['crossSectionOrientation'])
        self.assertIsNone(fp['dimensions'])

    def test_viewpoint_fills_visibility(self):
        fp = rs.fingerprint(
            st(),
            {'layers': [{'name': 'em', 'type': 'image',
                         'source': 'precomputed://em', 'visible': False}]})
        by = {l['name']: l for l in fp['layers']}
        self.assertFalse(by['em']['visible'])
        self.assertTrue(by['autoseg']['visible'])

    def test_segments_sorted_unique(self):
        fp = rs.fingerprint(st(layers=[
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': [3, 1, 3, '2']}]))
        self.assertEqual(fp['layers'][0]['segments'], [1, 2, 3])

    def test_uint64_segments_keep_exact_id(self):
        """minnie65 的 root id > 2^53；不能经 JS Number 后丢精度。"""
        big = '864691135684174263'
        fp = rs.fingerprint(st(layers=[
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': ['!' + big]}]))
        self.assertEqual(fp['layers'][0]['segments'], [int(big)])

    def test_slice_fields_default_to_clean(self):
        fp = rs.fingerprint(st())
        self.assertIsNone(fp['showSlices'])
        self.assertIsNone(fp['partialViewport'])
        # 缺省 = 默认：干净状态与显式默认状态必须一致
        explicit = rs.fingerprint(st(showSlices=True,
                                     partialViewport=[0, 0, 1, 1]))
        self.assertTrue(rs.compare(fp, explicit)['fields']['showSlices']['ok'])
        self.assertTrue(rs.compare(fp, explicit)['fields']
                        ['partialViewport']['ok'])

    def test_layer_style_is_restored_and_compared(self):
        a = rs.fingerprint(st(layers=[
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': [],
             'selectedAlpha': 0.8, 'baseSegmentColoring': True}]))
        b = rs.fingerprint(st(layers=[
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': [],
             'selectedAlpha': 0.8, 'baseSegmentColoring': True}]))
        self.assertEqual(a['layers'][0]['style']['selectedAlpha'], 0.8)
        self.assertTrue(rs.compare(a, b)['ok'])
        c = rs.fingerprint(st(layers=[
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': [],
             'selectedAlpha': 0.2, 'baseSegmentColoring': True}]))
        r = rs.compare(a, c)
        self.assertFalse(r['ok'])
        self.assertEqual(r['fields']['layers']['broken'], {'autoseg': 'style'})

    def test_source_list_takes_first(self):
        fp = rs.fingerprint(st(layers=[
            {'name': 'em', 'type': 'image',
             'source': ['precomputed://a', 'precomputed://b']}]))
        self.assertEqual(fp['layers'][0]['source'], 'precomputed://a')


class TestCompare(unittest.TestCase):
    def setUp(self):
        self.exp = rs.fingerprint(st())

    def test_identical_passes(self):
        got = rs.fingerprint(st())
        r = rs.compare(self.exp, got)
        self.assertTrue(r['ok'], r['failed'])
        self.assertEqual(r['failed'], [])

    def test_position_within_one_voxel(self):
        got = rs.fingerprint(st(position=[190587.4, 114102.0, 21773.9]))
        self.assertTrue(rs.compare(self.exp, got)['fields']['position']['ok'])

    def test_position_off_fails(self):
        got = rs.fingerprint(st(position=[190589, 114102, 21773]))
        r = rs.compare(self.exp, got)
        self.assertFalse(r['ok'])
        self.assertIn('position', r['failed'])

    def test_orientation_leak_fails(self):
        """记录里没有朝向（=默认），回放端却带着上一个点的旋转 → 必须失败。"""
        got = dict(self.exp)
        got['crossSectionOrientation'] = [0.7071, 0.0, 0.0, 0.7071]
        r = rs.compare(self.exp, got)
        self.assertFalse(r['fields']['crossSectionOrientation']['ok'])
        self.assertIn('crossSectionOrientation', r['failed'])

    def test_quaternion_sign_flip_passes(self):
        exp = dict(self.exp)
        exp['projectionOrientation'] = [0.7071, 0.0, 0.0, 0.7071]
        got = dict(exp)
        got['projectionOrientation'] = [-0.7071, 0.0, 0.0, -0.7071]
        self.assertTrue(rs.compare(exp, got)['fields']
                        ['projectionOrientation']['ok'])

    def test_scale_leak_fails(self):
        got = dict(self.exp)
        got['crossSectionScale'] = 7.0
        self.assertFalse(rs.compare(self.exp, got)['fields']
                         ['crossSectionScale']['ok'])

    def test_hidden_layer_fails_with_detail(self):
        got = rs.fingerprint(st())
        for l in got['layers']:
            if l['name'] == 'em':
                l['visible'] = False
        r = rs.compare(self.exp, got)
        self.assertFalse(r['fields']['layers']['ok'])
        self.assertEqual(r['fields']['layers']['broken'], {'em': 'visible'})

    def test_missing_layer_fails(self):
        got = rs.fingerprint(st(layers=[
            {'name': 'em', 'type': 'image', 'source': 'precomputed://em'}]))
        r = rs.compare(self.exp, got)
        self.assertEqual(r['fields']['layers']['broken'], {'autoseg': 'missing'})

    def test_source_change_fails(self):
        got = rs.fingerprint(st(layers=[
            {'name': 'em', 'type': 'image', 'source': 'precomputed://other'},
            {'name': 'autoseg', 'type': 'segmentation',
             'source': 'precomputed://seg', 'segments': []}]))
        r = rs.compare(self.exp, got)
        self.assertEqual(r['fields']['layers']['broken'], {'em': 'source'})

    def test_dimension_scale_fails(self):
        got = rs.fingerprint(st(dimensions={'x': [8e-09, 'm'],
                                           'y': [4e-09, 'm'],
                                           'z': [4e-08, 'm']}))
        self.assertFalse(rs.compare(self.exp, got)['fields']
                         ['dimensions']['ok'])

    def test_layout_change_fails(self):
        got = rs.fingerprint(st(layout='xy'))
        self.assertFalse(rs.compare(self.exp, got)['fields']['layout']['ok'])

    def test_summarize(self):
        a = rs.compare(self.exp, self.exp)
        b = rs.compare(self.exp, rs.fingerprint(st(layout='xy')))
        s = rs.summarize([a, b])
        self.assertEqual(s['n_rows'], 2)
        self.assertEqual(s['n_ok'], 1)
        self.assertEqual(s['per_field_ok']['layout'], 1)
        self.assertEqual(s['failed_fields'], ['layout'])


if __name__ == '__main__':
    unittest.main()
