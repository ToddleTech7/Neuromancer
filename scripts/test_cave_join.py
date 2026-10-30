"""cave_join 单元测试（离线，无网络）。"""

import tempfile
import unittest
from pathlib import Path

import cave_join as cj

T0 = 1_700_000_000_000


def ev(typ, t, **kw):
    d = {'type': typ, 't_wall': t}
    d.update(kw)
    return d


class TestParseServerTs(unittest.TestCase):
    def test_iso_with_space(self):
        # op_watcher 实际格式（空格分隔 + 微秒 + UTC）
        ms = cj.parse_server_ts('2025-12-08 07:15:10.528000+00:00')
        self.assertAlmostEqual(ms, 1765178110528.0, delta=0.5)

    def test_numeric_ms(self):
        self.assertEqual(cj.parse_server_ts(123456.0), 123456.0)

    def test_none_and_bad(self):
        self.assertIsNone(cj.parse_server_ts(None))
        self.assertIsNone(cj.parse_server_ts('not-a-date'))


class TestExtractAnchors(unittest.TestCase):
    def test_extract(self):
        pack = {'events': [
            ev('api_operation_id', T0, op_id=7, kind='merge', via='fetch'),
            ev('api_response', T0 + 10, kind='merge', status=200,
               server_date_ms=T0 + 10),
            ev('api_response', T0 + 20, kind='split', status=409),
            ev('api_error', T0 + 30, kind='split', err='x'),
            ev('state_sample', T0 + 40),
        ]}
        a = cj.extract_anchors(pack)
        self.assertEqual(len(a['op_anchors']), 1)
        self.assertEqual(len(a['server_dates']), 1)
        self.assertEqual(a['rejected_writes'][0]['status'], 409)
        self.assertEqual(len(a['api_errors']), 1)


class TestEstimateClock(unittest.TestCase):
    def test_op_anchor_priority(self):
        anchors = {'op_anchors': [
            {'op_id': 1, 't_wall': T0}], 'server_dates': [
            {'t_wall': T0, 'server_date_ms': T0 + 999}], 'rejected_writes': [],
            'api_errors': []}
        ops = {1: {'timestamp': _iso(T0 + 5000)}}
        c = cj.estimate_clock(anchors, ops)
        self.assertEqual(c['basis'], 'op-anchor')
        self.assertAlmostEqual(c['offset_ms'], 5000, delta=1)

    def test_none_basis(self):
        c = cj.estimate_clock({'op_anchors': [], 'server_dates': [],
                               'rejected_writes': [], 'api_errors': []}, {})
        self.assertEqual(c['basis'], 'none')
        self.assertEqual(c['offset_ms'], 0.0)
        self.assertIsNone(c['uncertainty_ms'])


class TestJoinSession(unittest.TestCase):
    def _pack(self, with_anchor=True):
        events = [
            ev('session_start', T0),
            ev('selection_sample', T0 + 1000,
               selection={'em': {'count': 1, 'ids': ['9']}}),
            ev('state_sample', T0 + 2000,
               viewpoint={'position': [1, 2, 3]}),
        ]
        if with_anchor:
            events.append(ev('api_operation_id', T0 + 3000, op_id=42,
                             kind='merge'))
        events.append(ev('session_stop', T0 + 10_000))
        return {'session_id': 's1', 'events': events,
                'time': {'start_wall_ms': T0, 'end_wall_ms': T0 + 10_000}}

    def test_exact_and_estimated_and_unmatched(self):
        pack = self._pack()
        ops = [
            {'op_id': 42, 'type': 'merge', 'timestamp': _iso(T0 + 3000),
             'before_root_ids': ['9'], 'after_root_ids': ['10']},
            {'op_id': 43, 'type': 'merge', 'timestamp': _iso(T0 + 5000),
             'before_root_ids': ['1'], 'after_root_ids': ['2']},
            {'op_id': 44, 'type': 'merge', 'timestamp': _iso(T0 + 999_000),
             'before_root_ids': ['1'], 'after_root_ids': ['2']},
        ]
        res = cj.join_session(pack, ops)
        j = {r['op_id']: r for r in res['joined']}
        self.assertEqual(j[42]['match_method'], 'exact-anchor')
        self.assertTrue(j[42]['evidence']['root_id_seen_in_session'])
        self.assertEqual(j[43]['match_method'], 'anchor-estimated')
        self.assertEqual(len(res['summary']['unmatched_ops']), 1)
        self.assertEqual(res['summary']['clock']['basis'], 'op-anchor')
        self.assertEqual(len(res['summary']['source']['events_sha256']), 64)
        # 未匹配 op 的原因字段存在
        self.assertIn('reason', res['summary']['unmatched_ops'][0])

    def test_window_fallback(self):
        pack = self._pack(with_anchor=False)
        ops = [{'op_id': 1, 'type': 'split',
                'timestamp': _iso(T0 + 2000),
                'before_root_ids': ['5'], 'after_root_ids': ['6']}]
        res = cj.join_session(pack, ops)
        self.assertEqual(res['joined'][0]['match_method'], 'window')
        self.assertEqual(res['summary']['clock']['uncertainty_ms'], None)

    def test_anchors_not_on_server(self):
        pack = self._pack()
        ops = []
        res = cj.join_session(pack, ops)
        self.assertEqual(res['summary']['anchors_not_on_server'][0]['op_id'],
                         42)


class TestOutputPaths(unittest.TestCase):
    def test_unique_path(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'x.jsonl'
            self.assertEqual(cj._unique_path(p), p)
            p.write_text('x')
            q = cj._unique_path(p)
            self.assertNotEqual(q, p)
            self.assertTrue(q.name.startswith('x_'))


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


if __name__ == '__main__':
    unittest.main()
