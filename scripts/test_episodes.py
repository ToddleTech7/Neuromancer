"""离线派生（PP06/PP07/PP10）的单元测试。

守的是几条不能妥协的语义：
  - `no_edit` 是事实，`disposition` 是判断，二者不可互推（Q08/PP10）；
  - 只有**显式**文本声明才算“检查过没问题”，没写就只能是 unknown；
  - idle 段只表示“没有输入”，不是“在思考”；
  - 残片合并后，被吞掉的切分理由必须留在 extra_triggers 里。
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cave_episodes as cep      # noqa: E402

T0 = 1_790_000_000_000


def ev(seq, dt_ms, typ, **kw):
    d = {'seq': seq, 't_wall': T0 + dt_ms, 't_mono': dt_ms, 'type': typ}
    d.update(kw)
    return d


def state(seq, dt_ms, pos):
    return ev(seq, dt_ms, 'state_sample',
              viewpoint={'state_id': seq, 'position_nm': pos})


class TestNormalize(unittest.TestCase):

    def test_seq_gap_detected(self):
        n = cep.normalize([ev(1, 0, 'session_start'),
                           ev(4, 10, 'session_stop')])
        self.assertEqual(n['seq_gaps'], [[2, 3]])
        self.assertFalse(n['seq_ok'])

    def test_monotonic_order(self):
        ok = cep.normalize([ev(1, 0, 'a'), ev(2, 5, 'a'), ev(3, 9, 'a')])
        self.assertTrue(ok['monotonic_order_ok'])

    def test_clock_drift_jump(self):
        evs = [ev(1, 0, 'a'), ev(2, 10, 'a')]
        evs[1]['t_wall'] = evs[1]['t_mono'] + 5000   # 墙钟被改了 5s
        n = cep.normalize(evs)
        self.assertTrue(n['clock_drift_ms']['jump_detected'])


class TestSplit(unittest.TestCase):

    def _pack(self, events):
        return {'events': events}

    @staticmethod
    def _reasons(res):
        """切分理由可能在 trigger，也可能在“被吞掉”的 extra_triggers 里。"""
        out = []
        for e in res['episodes']:
            if e.get('trigger'):
                out.append(e['trigger']['reason'])
            out.extend(t['reason'] for t in e.get('extra_triggers') or [])
        return out

    def test_time_gap_splits(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 100, 'input.pointer'),
               state(3, 200, [0, 0, 0]),
               # 超过默认 30s
               ev(4, 40000, 'input.pointer'),
               state(5, 40100, [0, 0, 0])]
        res = cep.derive_episodes(self._pack(evs))
        self.assertGreaterEqual(res['n_episodes'], 2)
        self.assertIn('time_gap', self._reasons(res))

    def test_time_gap_is_never_merged_away(self):
        """时间间隔切出的尾段哪怕只有 2 个事件，也不得被并进上一段。"""
        evs = [ev(1, 0, 'session_start'),
               ev(2, 100, 'input.pointer'),
               state(3, 200, [0, 0, 0]),
               ev(4, 40000, 'input.pointer'),
               state(5, 40100, [0, 0, 0])]
        res = cep.derive_episodes(self._pack(evs))
        self.assertEqual(res['n_episodes'], 2)
        self.assertEqual(res['episodes'][1]['trigger']['reason'], 'time_gap')
        # 40s 的间隔不得被算进第一个 episode 的时长
        self.assertLess(res['episodes'][0]['duration_ms'], 40000)

    def test_spatial_jump_splits(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 10, 'input.pointer'),
               state(3, 100, [0, 0, 0]),
               # 默认阈值 50µm
               state(4, 500, [200000, 0, 0]),
               ev(5, 600, 'input.pointer')]
        res = cep.derive_episodes(self._pack(evs))
        self.assertIn('spatial_jump', self._reasons(res))

    def test_merged_reason_is_kept(self):
        """小残片被合并时，它的切分理由不能丢（否则边界无法解释）。"""
        evs = [ev(1, 0, 'session_start')] + \
              [ev(2 + i, 10 + i, 'input.pointer') for i in range(5)] + \
              [state(7, 100, [0, 0, 0]),
               ev(8, 40000, 'input.pointer')] + \
              [state(9, 40100, [0, 0, 0])] + \
              [ev(10 + i, 40200 + i, 'input.pointer') for i in range(6)]
        res = cep.derive_episodes(self._pack(evs))
        all_reasons = []
        for e in res['episodes']:
            if e.get('trigger'):
                all_reasons.append(e['trigger']['reason'])
            all_reasons.extend(t['reason'] for t in e.get('extra_triggers') or [])
        self.assertIn('time_gap', all_reasons)


class TestIdleAndNoEdit(unittest.TestCase):

    def _pack(self, events):
        return {'events': events}

    def test_idle_only_means_no_input(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 0, 'input.pointer'),
               # 10s 没有输入
               ev(3, 10000, 'input.pointer'),
               ev(4, 10100, 'input.pointer'),
               ev(5, 10200, 'input.pointer')]
        res = cep.derive_episodes(self._pack(evs))
        segs = res['episodes'][0]['idle_segments']
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]['duration_ms'], 10000)
        # 不得出现“思考时长”这类派生字段
        self.assertNotIn('thinking', str(segs).lower())

    def test_trailing_idle_to_session_end_is_counted(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 1000, 'input.pointer'),
               ev(3, 2000, 'input.pointer'),
               ev(4, 20000, 'session.end')]
        res = cep.derive_episodes(self._pack(evs))
        segs = res['episodes'][0]['idle_segments']
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0]['start_ms'], evs[2]['t_wall'])
        self.assertEqual(segs[0]['end_ms'], evs[3]['t_wall'])

    def test_idle_at_boundary_is_attributed_once(self):
        """跨切分边界的静默：既不能丢，也不能两边都算。

        归属规则：归给“最后那一下输入所在的段”（这里是第一段）。
        """
        evs = [ev(1, 0, 'session_start'),
               ev(2, 100, 'input.pointer'),
               ev(3, 40100, 'input.pointer'),
               ev(4, 40200, 'input.pointer')]
        res = cep.derive_episodes(self._pack(evs))
        self.assertEqual(res['n_episodes'], 2)
        segs0 = res['episodes'][0]['idle_segments']
        self.assertEqual(len(segs0), 1)
        self.assertEqual(segs0[0]['duration_ms'], 40000)
        self.assertEqual(res['episodes'][1]['idle_segments'], [])
        self.assertEqual(sum(len(e['idle_segments']) for e in res['episodes']), 1)

    def test_no_edit_is_fact_not_judgement(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 10, 'input.pointer'),
               ev(3, 20, 'input.pointer'),
               ev(4, 30, 'input.pointer')]
        res = cep.derive_episodes(self._pack(evs))
        e = res['episodes'][0]
        self.assertTrue(e['no_edit'])
        # 没提交 ≠ 检查过没问题
        self.assertEqual(e['disposition'], 'unknown')
        self.assertIsNone(e['disposition_reason'])

    def test_explicit_declaration_upgrades_disposition(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 10, 'input.pointer'),
               ev(3, 20, 'input.pointer'),
               ev(4, 30, 'note', text='这段已检查完，无误')]
        res = cep.derive_episodes(self._pack(evs))
        e = res['episodes'][0]
        self.assertTrue(e['no_edit'])
        self.assertEqual(e['disposition'], 'inspected_no_error')

    def test_note_without_declaration_stays_unknown(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 10, 'input.pointer'),
               ev(3, 20, 'input.pointer'),
               ev(4, 30, 'note', text='看起来有点怪，再看看')]
        res = cep.derive_episodes(self._pack(evs))
        self.assertEqual(res['episodes'][0]['disposition'], 'unknown')

    def test_edit_marks_edited(self):
        evs = [ev(1, 0, 'session_start'),
               ev(2, 10, 'input.pointer'),
               ev(3, 20, 'input.pointer'),
               ev(4, 30, 'api_request', kind='merge', write=True)]
        res = cep.derive_episodes(self._pack(evs))
        e = res['episodes'][0]
        self.assertFalse(e['no_edit'])
        self.assertEqual(e['disposition'], 'edited')

    def test_read_request_does_not_count_as_edit(self):
        """读请求（GET）不是编辑——写标志缺失时不得当成提交过。"""
        for kw in ({'kind': 'root', 'write': False}, {'kind': 'root'}):
            evs = [ev(1, 0, 'session_start'),
                   ev(2, 10, 'input.pointer'),
                   ev(3, 20, 'input.pointer'),
                   ev(4, 30, 'api_request', **kw)]
            e = cep.derive_episodes(self._pack(evs))['episodes'][0]
            self.assertTrue(e['no_edit'], kw)
            self.assertEqual(e['disposition'], 'unknown')


class TestLegacyNames(unittest.TestCase):
    """旧包（下划线事件名）也必须能派生——否则改名就是破坏性变更。"""

    def test_legacy_state_sample(self):
        evs = [{'seq': 1, 't_wall': T0, 'type': 'session_start'},
               {'seq': 2, 't_wall': T0 + 10, 'type': 'pointer'},
               {'seq': 3, 't_wall': T0 + 20, 'type': 'pointer'},
               {'seq': 4, 't_wall': T0 + 30, 'type': 'state_sample',
                'viewpoint': {'position_nm': [1, 2, 3]}}]
        res = cep.derive_episodes({'events': evs})
        self.assertEqual(res['episodes'][0]['view']['n_points'], 1)


class TestHover(unittest.TestCase):
    """指针悬停派生（F09）。

    守三条：
      - “没人悬停”与“取不到命中值”必须分开（后者最常见，实测 97 条全空）；
      - 悬停时长是下界，区间结束取“下一个样本”；
      - 非段 id 的 picked（灰度值等）不能混进结果，但要计数。
    """

    @staticmethod
    def _pack(evs):
        return {'events': evs}

    @staticmethod
    def _focus(seq, dt, picked=None, layer=None, pos=(0, 0, 0)):
        d = ev(seq, dt, 'mouse_sample', position=list(pos))
        if picked is not None:
            d['picked'] = picked
        if layer is not None:
            d['picked_layer'] = layer
        return d

    def test_no_samples(self):
        res = cep.derive_episodes(self._pack(
            [ev(1, 0, 'session_start'), ev(2, 10, 'pointer')]))
        self.assertEqual(res['hover']['status'], 'no_samples')

    def test_all_picked_empty_is_not_no_hover(self):
        """有样本但 picked 全空：不能报成“没人悬停”。"""
        evs = [ev(1, 0, 'session_start'),
               self._focus(2, 100), self._focus(3, 300),
               ev(4, 900, 'session_stop')]
        h = cep.derive_episodes(self._pack(evs))['hover']
        self.assertEqual(h['status'], 'no_picked')
        self.assertEqual(h['n_samples'], 2)
        self.assertEqual(h['n_with_picked'], 0)
        self.assertIn('不是没人悬停', h['status_why'])
        self.assertEqual(h['segments'], [])

    def test_spans_merge_and_duration(self):
        """相邻同段合并；时长 = 下一个样本 - 本段第一个样本。"""
        evs = [ev(1, 0, 'session_start'),
               self._focus(2, 100, '1956846', 'seg'),
               self._focus(3, 600, '1956846', 'seg'),
               self._focus(4, 1200, '1956847', 'seg'),
               ev(5, 1600, 'session_stop')]
        h = cep.derive_episodes(self._pack(evs))['hover']
        self.assertEqual(h['status'], 'ok')
        self.assertEqual(h['n_spans'], 2)
        by_id = {s['id']: s for s in h['segments']}
        # 1956846：100 → 1200（下一样本时刻）= 1100ms
        self.assertAlmostEqual(by_id['1956846']['total_ms'], 1100.0, places=1)
        self.assertEqual(by_id['1956846']['n_spans'], 1)
        # 1956847：1200 → 1600（t_end）= 400ms，且已闭合
        self.assertAlmostEqual(by_id['1956847']['total_ms'], 400.0, places=1)
        self.assertEqual(h['open_spans'], 0)
        # 按总时长降序
        self.assertEqual(h['segments'][0]['id'], '1956846')

    def test_open_span_counted(self):
        evs = [ev(1, 0, 'session_start'),
               self._focus(2, 100, '1956846', 'seg')]
        h = cep.derive_episodes(self._pack(evs))['hover']
        self.assertEqual(h['open_spans'], 1)
        self.assertAlmostEqual(h['segments'][0]['total_ms'], 0.0, places=1)

    def test_non_segment_picked_counted_not_mixed(self):
        """灰度值/属性名不算段，但要计数（不静默丢）。"""
        evs = [ev(1, 0, 'session_start'),
               self._focus(2, 100, '128'),
               self._focus(3, 400, '1956846', 'seg'),
               ev(4, 800, 'session_stop')]
        h = cep.derive_episodes(self._pack(evs))['hover']
        self.assertEqual(h['n_not_segment_id'], 1)
        self.assertEqual([s['id'] for s in h['segments']], ['1956846'])

    def test_caveats_always_there(self):
        evs = [ev(1, 0, 'session_start'),
               self._focus(2, 100, '1956846', 'seg'),
               ev(3, 400, 'session_stop')]
        h = cep.derive_episodes(self._pack(evs))['hover']
        self.assertTrue(any('不推断' in c for c in h['caveats']))



class TestDerivedFields(unittest.TestCase):
    def test_hover_and_selected_are_separate(self):
        evs = [ev(1, 0, 'session_start'),
               state(2, 100, [0, 0, 0]),
               ev(3, 200, 'selection_sample',
                  selection={'autoseg': {'count': 1, 'ids': ['111']}}),
               ev(4, 300, 'state_sample',
                  state={'position': [0, 0, 0],
                         'layers': [{'name': 'autoseg', 'type': 'segmentation',
                                     'segments': ['!222', '333']}]}),
               ev(5, 400, 'session_stop')]
        res = cep.derive_episodes({'events': evs})
        ep = res['episodes'][0]
        self.assertEqual(ep['hover_selections'],
                         [{'autoseg': {'count': 1, 'ids': ['111']}}])
        self.assertEqual(ep['selections'], ep['hover_selections'])
        self.assertEqual(ep['selected_segments'][-1]['segments'],
                         {'autoseg': ['222', '333']})

    def test_derived_manifest_has_source_hash(self):
        import json
        import tempfile
        from pathlib import Path
        evs = [ev(1, 0, 'session_start'), ev(2, 100, 'session_stop')]
        res = cep.derive_episodes({'events': evs, 'session_id': 's1'})
        with tempfile.TemporaryDirectory() as tmp:
            cep.write_derived(res, Path(tmp))
            man = json.loads((Path(tmp) / 'manifest.json').read_text('utf-8'))
            self.assertEqual(man['source']['session_id'], 's1')
            self.assertEqual(len(man['source']['events_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
