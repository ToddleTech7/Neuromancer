"""cave_op_watcher 单元测试（离线；纯函数 + 假 kg 的窗口采集）。"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import cave_op_watcher as cw

UTC = dt.timezone.utc
T0 = dt.datetime(2025, 12, 8, 7, 10, tzinfo=UTC)
T1 = T0 + dt.timedelta(minutes=10)


class TestParseTime(unittest.TestCase):
    def test_naive_becomes_utc(self):
        t = cw.parse_time('2025-12-08T07:15:00')
        self.assertEqual(t.tzinfo, UTC)
        self.assertEqual(t.hour, 7)

    def test_offset_converted(self):
        t = cw.parse_time('2025-12-08T07:15:00+08:00')
        self.assertEqual(t.hour, 23)
        self.assertEqual(t.day, 7)


class TestLoadSeen(unittest.TestCase):
    def test_reads_valid_ids_only(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / 'ops.jsonl'
            p.write_text('\n'.join([
                json.dumps({'op_id': 11}),
                'junk',
                '',
                json.dumps({'x': 1}),
                json.dumps({'op_id': '12'}),
            ]) + '\n', encoding='utf-8')
            self.assertEqual(cw.load_seen(p), {11, 12})

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(cw.load_seen(Path(td) / 'no.jsonl'), set())


class TestToUtc(unittest.TestCase):
    def test_none_and_nan(self):
        self.assertIsNone(cw._to_utc(None))
        self.assertIsNone(cw._to_utc(float('nan')))

    def test_iso_strings(self):
        t = cw._to_utc('2025-12-08T07:15:00+08:00')
        self.assertEqual(t.hour, 23)
        z = cw._to_utc('2025-12-08T07:15:00Z')
        self.assertEqual(z, dt.datetime(2025, 12, 8, 7, 15, tzinfo=UTC))

    def test_epoch_ms(self):
        t = cw._to_utc(1765178100000.0)
        self.assertEqual(t, dt.datetime.fromtimestamp(1765178100, tz=UTC))

    def test_naive_and_pandas(self):
        naive = dt.datetime(2025, 12, 8, 7, 15)
        self.assertEqual(cw._to_utc(naive).tzinfo, UTC)
        self.assertEqual(cw._to_utc(pd.Timestamp(naive)),
                         dt.datetime(2025, 12, 8, 7, 15, tzinfo=UTC))

    def test_bad_values(self):
        self.assertIsNone(cw._to_utc('not-a-time'))
        self.assertIsNone(cw._to_utc([1, 2]))


class TestDeriveType(unittest.TestCase):
    def test_split_priority(self):
        both = {'added_edges': [[1, 2]], 'removed_edges': [[3, 4]]}
        self.assertEqual(cw.derive_type(both), 'split')

    def test_merge_and_unknown(self):
        self.assertEqual(cw.derive_type({'added_edges': [[1, 2]]}), 'merge')
        self.assertEqual(cw.derive_type({}), 'unknown')
        self.assertEqual(cw.derive_type({'added_edges': None,
                                         'removed_edges': None}), 'unknown')


class TestNormalize(unittest.TestCase):
    DETAIL = {
        'user': 42,
        'timestamp': '2025-12-08T07:20:00+00:00',
        'added_edges': [[1, 2]],
        'removed_edges': [],
        'roots': [9],
    }

    def test_is_merge_priority(self):
        rec = cw.normalize(11, self.DETAIL, 'now', {'is_merge': False})
        self.assertEqual(rec['type'], 'split')  # 覆盖 added_edges 推导

    def test_derive_when_row_missing_is_merge(self):
        rec = cw.normalize(11, self.DETAIL, 'now', {})
        self.assertEqual(rec['type'], 'merge')
        rec2 = cw.normalize(11, self.DETAIL, 'now')
        self.assertEqual(rec2['type'], 'merge')
        self.assertIsNone(rec2['change_log_row'])

    def test_field_mapping(self):
        row = {'is_merge': True, 'user_name': 'alice',
               'user_affiliation': 'lab', 'before_root_ids': [1, 2],
               'after_root_ids': [3]}
        rec = cw.normalize(11, self.DETAIL, 'now', row)
        self.assertEqual(rec['op_id'], 11)
        self.assertEqual(rec['user'], 42)
        self.assertEqual(rec['user_name'], 'alice')
        self.assertEqual(rec['before_root_ids'], [1, 2])
        self.assertEqual(rec['timestamp'], '2025-12-08T07:20:00+00:00')
        self.assertEqual(rec['raw'], self.DETAIL)

    def test_timestamp_fallback(self):
        d = dict(self.DETAIL)
        d.pop('timestamp')
        d['operation_ts'] = 1234567890000
        rec = cw.normalize(11, d, 'now')
        self.assertEqual(rec['timestamp'], 1234567890000)
        self.assertIsNone(cw.normalize(11, {}, 'now')['timestamp'])


class TestFindCol(unittest.TestCase):
    def test_found(self):
        df = pd.DataFrame(columns=['Timestamp', 'operation_id', 'user_name'])
        self.assertEqual(cw.find_col(df, 'time'), 'Timestamp')
        self.assertEqual(cw.find_col(df, 'operation', 'id'), 'operation_id')

    def test_not_found(self):
        df = pd.DataFrame(columns=['a', 'b'])
        self.assertIsNone(cw.find_col(df, 'time'))


class FakeKG:
    def __init__(self, past=(), future=(), logs=None, details=None,
                 delta_err=None):
        self._past, self._future = list(past), list(future)
        self._logs = logs or {}
        self._details = details or {}
        self._delta_err = delta_err
        self.detail_calls = []

    def get_delta_roots(self, t0, t1):
        if self._delta_err is not None:
            raise self._delta_err
        return self._past, self._future

    def get_tabular_change_log(self, roots, filtered=False):
        return {r: self._logs.get(r) for r in roots}

    def get_operation_details(self, ids):
        self.detail_calls.append(list(ids))
        return {str(i): self._details[i] for i in ids if i in self._details}


class FakeCL:
    def __init__(self, kg):
        self.chunkedgraph = kg


def log_df(rows):
    return pd.DataFrame(rows)


class TestCollectWindow(unittest.TestCase):
    def test_full_path_and_dedup(self):
        df1 = log_df([
            {'timestamp': T0 + dt.timedelta(seconds=60), 'operation_id': 11,
             'is_merge': True, 'user_name': 'alice'},
            {'timestamp': T0 + dt.timedelta(seconds=120), 'operation_id': 12,
             'is_merge': False, 'user_name': 'bob'},
        ])
        df2 = log_df([
            {'timestamp': T0 + dt.timedelta(seconds=180), 'operation_id': 13,
             'is_merge': None, 'user_name': 'carol'},
        ])
        kg = FakeKG(past=[100], future=[200],
                    logs={100: df1, 200: df2},
                    details={11: {'user': 'u1', 'added_edges': [[1, 2]],
                                  'removed_edges': []},
                             13: {'user': 'u3', 'added_edges': [[5, 6]],
                                  'removed_edges': []}})
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen={12})
        self.assertEqual(res['delta_roots_found'], 2)
        self.assertEqual(res['op_ids_in_window'], 3)
        self.assertEqual(res['new_ops'], 2)
        self.assertEqual(kg.detail_calls, [[11, 13]])
        by_id = {r['op_id']: r for r in recs}
        self.assertEqual(by_id[11]['type'], 'merge')
        self.assertEqual(by_id[11]['user_name'], 'alice')
        self.assertEqual(by_id[13]['type'], 'merge')  # is_merge 缺失 → 边推导
        self.assertEqual(res['errors'], [])

    def test_window_filtering(self):
        df = log_df([
            {'timestamp': T0 - dt.timedelta(seconds=1), 'operation_id': 21},
            {'timestamp': T0 + dt.timedelta(seconds=1), 'operation_id': 22},
            {'timestamp': T1, 'operation_id': 23},  # 右开区间
        ])
        kg = FakeKG(past=[100], logs={100: df},
                    details={22: {'user': 'u'}})
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(res['op_ids_in_window'], 1)
        self.assertEqual([r['op_id'] for r in recs], [22])

    def test_inverted_window_error(self):
        kg = FakeKG(delta_err=RuntimeError(
            'Inverted or empty timestamp range'))
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(recs, [])
        self.assertIn('窗口晚于数据栈最新数据时间', res['errors'][0])

    def test_generic_delta_error(self):
        kg = FakeKG(delta_err=ValueError('boom'))
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertIn('delta_roots: ValueError', res['errors'][0])

    def test_missing_columns(self):
        df = log_df([{'foo': 1, 'bar': 2}])
        kg = FakeKG(past=[100], logs={100: df})
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(recs, [])
        self.assertIn('缺列', res['errors'][0])

    def test_missing_detail(self):
        df = log_df([{'timestamp': T0 + dt.timedelta(seconds=1),
                      'operation_id': 31}])
        kg = FakeKG(past=[100], logs={100: df}, details={})
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(res['new_ops'], 0)
        self.assertEqual(res['op_ids_in_window'], 1)
        self.assertIn('op 31: 详情缺失', res['errors'])

    def test_no_delta_roots(self):
        kg = FakeKG()
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(recs, [])
        self.assertEqual(res['delta_roots_found'], 0)
        self.assertEqual(res['errors'], [])

    def test_first_root_wins_for_same_op(self):
        df1 = log_df([{'timestamp': T0 + dt.timedelta(seconds=1),
                       'operation_id': 41, 'user_name': 'first'}])
        df2 = log_df([{'timestamp': T0 + dt.timedelta(seconds=2),
                       'operation_id': 41, 'user_name': 'second'}])
        # roots 排序 → 100 先于 200 → df1 胜
        kg = FakeKG(past=[200], future=[100], logs={100: df1, 200: df2},
                    details={41: {'user': 'u'}})
        res, recs = cw.collect_window(FakeCL(kg), T0, T1, seen=set())
        self.assertEqual(recs[0]['user_name'], 'first')


if __name__ == '__main__':
    unittest.main()
