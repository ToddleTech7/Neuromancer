"""cave_derive 单元测试（纯函数 + 假 kg，无网络）。"""

import unittest

import cave_derive as cd

T0_MS = 1_700_000_000_000.0
ISO = '2023-11-14 22:13:20+00:00'


def iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


class FakeKG:
    """仅用于 derive_all 的调度测试（不触碰 derive_one 逻辑）。"""

    def __init__(self):
        self.latest_calls = []
        self.lineage_calls = []
        self.detail_calls = []

    def is_latest_roots(self, roots, timestamp=None):
        import numpy as np
        self.latest_calls.append(list(roots))
        return np.array([True] * len(roots))

    def get_lineage_graph(self, root_id, as_nx_graph=False):
        self.lineage_calls.append(root_id)
        return {'nodes': [], 'links': []}

    def get_operation_details(self, op_ids):
        self.detail_calls.append(list(op_ids))
        return {}


class TestDeriveOne(unittest.TestCase):
    def _op(self, after, ts_ms=T0_MS, op_id=1, typ='merge'):
        return {'op_id': op_id, 'type': typ, 'timestamp': iso(ts_ms),
                'user': 'u', 'after_root_ids': after}

    def test_latest_censored(self):
        op = self._op(['100'])
        rec = cd.derive_one(op, {'100': True}, {}, {}, T0_MS + 100_000)
        self.assertEqual(rec['state'], 'latest')
        self.assertTrue(rec['censored'])
        self.assertAlmostEqual(rec['survival_seconds'], 100.0, delta=0.1)
        self.assertIsNone(rec['undo_detected'])

    def test_later_split(self):
        op = self._op(['100'])
        succ = {'100': {'death_op': 999, 'birth_ts_ms': T0_MS,
                        'child_roots': ['101', '102'], 'error': None}}
        det = {999: {'ts_ms': T0_MS + 5000, 'kind': 'split',
                     'before': ['100'], 'after': ['101', '102']}}
        rec = cd.derive_one(op, {'100': False}, succ, det, T0_MS + 1e6)
        self.assertEqual(rec['state'], 'later_split')
        self.assertFalse(rec['censored'])
        self.assertAlmostEqual(rec['survival_seconds'], 5.0, delta=0.1)
        self.assertEqual(rec['next_change']['op_id'], 999)
        self.assertEqual(rec['n_successor_ops'], 1)

    def test_later_merged_first_wins(self):
        op = self._op(['100', '200'])
        succ = {'100': {'death_op': 999, 'birth_ts_ms': T0_MS,
                        'child_roots': [], 'error': None},
                '200': {'death_op': 888, 'birth_ts_ms': T0_MS,
                        'child_roots': [], 'error': None}}
        det = {999: {'ts_ms': T0_MS + 9000, 'kind': 'merge',
                     'before': None, 'after': None},
               888: {'ts_ms': T0_MS + 3000, 'kind': 'merge',
                     'before': None, 'after': None}}
        rec = cd.derive_one(op, {'100': False, '200': False}, succ, det,
                            T0_MS + 1e6)
        self.assertEqual(rec['state'], 'later_merged')
        self.assertEqual(rec['next_change']['op_id'], 888)
        self.assertAlmostEqual(rec['survival_seconds'], 3.0, delta=0.1)

    def test_semantic_drift_guard(self):
        # 消费操作时间早于诞生 → 丢弃，视为无后继
        op = self._op(['100'])
        succ = {'100': {'death_op': 999, 'birth_ts_ms': T0_MS + 1000,
                        'child_roots': [], 'error': None}}
        det = {999: {'ts_ms': T0_MS, 'kind': 'merge', 'before': None,
                     'after': None}}
        rec = cd.derive_one(op, {'100': False}, succ, det, T0_MS + 1e6)
        self.assertEqual(rec['state'], 'not_latest_no_successor')
        self.assertEqual(rec['n_successor_ops'], 0)

    def test_no_after_roots(self):
        op = {'op_id': 1, 'type': 'merge', 'timestamp': iso(T0_MS),
              'after_root_ids': None, 'roots': None}
        rec = cd.derive_one(op, {}, {}, {}, T0_MS)
        self.assertEqual(rec['state'], 'no_after_roots')

    def test_op_kind_from_edges(self):
        self.assertEqual(
            cd.op_kind_from_edges({'added_edges': [[1, 2]],
                                   'removed_edges': []}), 'merge')
        self.assertEqual(
            cd.op_kind_from_edges({'added_edges': [],
                                   'removed_edges': [[1, 2]]}), 'split')
        self.assertEqual(
            cd.op_kind_from_edges({'added_edges': [],
                                   'removed_edges': []}), 'unknown')


class TestDeriveAllScheduling(unittest.TestCase):
    def test_only_non_latest_get_lineage(self):
        ops = [
            {'op_id': 1, 'type': 'merge', 'timestamp': iso(T0_MS),
             'after_root_ids': ['100'], 'roots': None},
            {'op_id': 2, 'type': 'merge', 'timestamp': iso(T0_MS),
             'after_root_ids': ['200'], 'roots': None},
        ]

        class KG2(FakeKG):
            def is_latest_roots(self, roots, timestamp=None):
                import numpy as np
                return np.array([True, False])  # 100 latest, 200 非

        kg = KG2()
        recs, errors, _ = cd.derive_all(ops, kg, checked_at_ms=T0_MS,
                                        verbose=False)
        # 仅 200（非 latest）触发谱系调用
        self.assertEqual(len(kg.lineage_calls), 1)
        self.assertEqual(kg.lineage_calls[0], 200)
        self.assertEqual(recs[0]['state'], 'latest')
        self.assertEqual(recs[1]['state'], 'not_latest_no_successor')
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
