"""cave_session_replay 单元测试（离线；不落盘 HTML）。"""

import unittest

import cave_session_replay as cr

T0 = 1_700_000_000_000


def mini_pack(events=None):
    events = events or [
        {'type': 'session_start', 't_wall': T0,
         'page': {'url': 'http://x'}},
        {'type': 'state_sample', 't_wall': T0 + 1000,
         'viewpoint': {'position': [1, 2, 3], 'crossSectionScale': 2}},
        {'type': 'note', 't_wall': T0 + 2000, 'text': 'hello'},
        {'type': 'session_stop', 't_wall': T0 + 3000},
    ]
    return {'schema': 'cave_recorder.session.v0.1', 'session_id': 's1',
            'page': {'url': 'http://x'}, 'viewer': {'detected': True},
            'time': {'start_wall_ms': T0, 'end_wall_ms': T0 + 3000},
            'counts': {'state_sample': 1}, 'notes': [], 'shots': [],
            'errors': [], 'events': events}


class TestEventLine(unittest.TestCase):
    def test_api_response_fields(self):
        t, typ, text = cr.event_line(
            {'type': 'api_response', 't_wall': T0 + 10, 'kind': 'merge',
             'status': 200, 'ms': 42, 'server_date_ms': T0 + 9,
             'via': 'xhr'}, T0)
        self.assertIn('status=200', text)
        self.assertIn('42ms', text)
        self.assertIn('server_date=', text)
        self.assertIn('via=xhr', text)

    def test_api_response_minimal(self):
        t, typ, text = cr.event_line(
            {'type': 'api_response', 't_wall': T0, 'kind': 'read',
             'status': 200}, T0)
        self.assertEqual(text, 'read status=200')

    def test_note_and_unknown(self):
        t, typ, text = cr.event_line(
            {'type': 'note', 't_wall': T0, 'text': 'n'}, T0)
        self.assertEqual(text, 'n')
        t, typ, text = cr.event_line(
            {'type': 'weird', 't_wall': T0, 'x': 1}, T0)
        self.assertIn('x', text)

    def test_canonical_names_are_formatted(self):
        """新包用点号名；HTML 不能退回成一坨 JSON。"""
        _, typ, text = cr.event_line(
            {'type': 'viewer.state-change', 't_wall': T0 + 1000,
             'viewpoint': {'position': [1, 2, 3], 'crossSectionScale': 2}},
            T0)
        self.assertEqual(typ, 'viewer.state-change')
        self.assertIn('pos=(1,2,3)', text)
        self.assertIn('cross=2', text)

        _, typ, text = cr.event_line(
            {'type': 'selection.change', 't_wall': T0,
             'selection': {'autoseg': {'count': 1, 'ids': ['9']}}}, T0)
        self.assertEqual(typ, 'selection.change')
        self.assertIn('autoseg', text)

        _, typ, text = cr.event_line(
            {'type': 'input.pointer', 't_wall': T0, 'phase': 'pointermove',
             'x': 7, 'y': 8}, T0)
        self.assertIn('pointermove @(7,8)', text)


class TestAttentionRows(unittest.TestCase):
    def test_selection_is_time_aware(self):
        pack = mini_pack(events=[
            {'type': 'selection.change', 't_wall': T0,
             'selection': {'seg': {'count': 1, 'ids': ['1']}}},
            {'type': 'viewer.state-change', 't_wall': T0 + 1000,
             'viewpoint': {'position': [1, 2, 3]}},
            {'type': 'selection.change', 't_wall': T0 + 2000,
             'selection': {'seg': {'count': 2, 'ids': ['1', '2']}}},
            {'type': 'viewer.state-change', 't_wall': T0 + 3000,
             'viewpoint': {'position': [4, 5, 6]}},
        ])
        rows = cr.attention_rows(pack['events'], T0)
        self.assertEqual(rows[0]['selected'], 'seg:1')
        self.assertEqual(rows[1]['selected'], 'seg:2')


class TestOpLine(unittest.TestCase):
    def test_op_line(self):
        rec = {'op_id': 9, 'op_type': 'merge',
               'client_time_ms': T0 + 2500, 'match_method': 'exact-anchor',
               'clock_offset_ms': 5.0, 'before_root_ids': ['1'],
               'after_root_ids': ['2'], 'evidence':
               {'root_id_seen_in_session': True}}
        t, typ, text = cr.op_line(rec, T0)
        self.assertAlmostEqual(t, 2.5, delta=0.01)
        self.assertEqual(typ, 'op:merge')
        self.assertIn('op=9', text)
        self.assertIn('exact-anchor', text)


class TestBuildHtml(unittest.TestCase):
    def test_without_join(self):
        pack = mini_pack()
        lines = [cr.event_line(e, T0) for e in pack['events']]
        html = cr.build_html(pack, lines)
        self.assertIn('CAVE 会话回放', html)
        self.assertNotIn('服务端 op 对照', html)

    def test_with_join_section(self):
        pack = mini_pack()
        lines = [cr.event_line(e, T0) for e in pack['events']]
        join_info = {
            'path': 'x.join.jsonl',
            'records': [{'op_id': 9, 'op_type': 'merge',
                         'client_time_ms': T0 + 2500,
                         'match_method': 'exact-anchor',
                         'clock_offset_ms': 0, 'server_time_utc': 't',
                         'before_root_ids': ['1'], 'after_root_ids': ['2'],
                         'evidence': {'root_id_seen_in_session': True,
                                      'in_session_span': True},
                         'context': {'notes': [], 'multicuts': []}}],
            'summary': {'clock': {'basis': 'op-anchor', 'offset_ms': 0,
                                  'uncertainty_ms': 250, 'n_samples': 1},
                        'anchors_not_on_server': [],
                        'rejected_writes': []},
        }
        lines = lines + [cr.op_line(join_info['records'][0], T0)]
        html = cr.build_html(pack, lines, join_info)
        self.assertIn('服务端 op 对照', html)
        self.assertIn('op:merge', html)
        self.assertIn('exact-anchor', html)

    def test_svg_track_insufficient(self):
        out = cr.svg_track([])
        self.assertIn('不足', out)


class TestCollectStates(unittest.TestCase):
    """复刻单位 = 每个状态变化点，而不是 30s checkpoint。

    这里锁住三件容易默默变坏的事：① 优先用完整 state；② 残缺 state 不能用；
    ③ 旧包要退回 checkpoint 并标 source，不能假装是完整 state。
    """

    def _ev(self, t, sid, sha, partial=None):
        e = {'type': 'viewer.state-change', 't_wall': t, 'state_id': sid,
             'state': {'position': [1, 2, 3], 'layers': [],
                       'dimensions': {}, 'layout': '4panel'},
             'state_sha': sha}
        if partial:
            e['state_partial'] = partial
        return e

    def test_prefers_state_changes(self):
        import cave_replay_viewer as rv
        pack = {'events': [
            self._ev(T0, 1, 'a'), self._ev(T0 + 1000, 2, 'b')]}
        pts = rv.collect_states(pack)
        self.assertEqual(len(pts), 2)
        self.assertTrue(all(p['source'] == 'state' for p in pts))
        self.assertEqual([p['state_id'] for p in pts], [1, 2])

    def test_skips_partial_state(self):
        import cave_replay_viewer as rv
        pack = {'events': [self._ev(T0, 1, 'a', partial='layers'),
                           self._ev(T0 + 1000, 2, 'b')]}
        pts = rv.collect_states(pack)
        self.assertEqual([p['state_id'] for p in pts], [2])

    def test_dedupes_same_sha(self):
        import cave_replay_viewer as rv
        pack = {'events': [self._ev(T0, 1, 'same'),
                           self._ev(T0 + 1000, 2, 'same')]}
        self.assertEqual(len(rv.collect_states(pack)), 1)

    def test_state_id_falls_back_to_viewpoint(self):
        import cave_replay_viewer as rv
        e = self._ev(T0, None, 'a')
        e.pop('state_id')
        e['viewpoint'] = {'state_id': 7}
        pts = rv.collect_states({'events': [e]})
        self.assertEqual(pts[0]['state_id'], 7)

    def test_old_pack_falls_back_to_checkpoint(self):
        import cave_replay_viewer as rv
        pack = {'events': [
            {'type': 'state_sample', 't_wall': T0, 'viewpoint': {}},
            {'type': 'state_checkpoint', 't_wall': T0 + 500, 'state_id': 3,
             'reason': 'session_start',
             'state': {'position': [1, 2, 3], 'layers': []}}]}
        pts = rv.collect_states(pack)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0]['source'], 'checkpoint')

    def test_limit_applies(self):
        import cave_replay_viewer as rv
        pack = {'events': [self._ev(T0 + i, i, str(i)) for i in range(5)]}
        self.assertEqual(len(rv.collect_states(pack, limit=3)), 3)


if __name__ == '__main__':
    unittest.main()
