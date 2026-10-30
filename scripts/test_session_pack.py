"""会话包目录契约 / join 目录输入 / 样本 process+provenance 的单元测试。

这些测试只碰离线逻辑（不访问网络、不启动浏览器）：
- pack_session 落盘结构与 manifest 健康摘要（心跳/缺口/seq 断档）
- load_session 目录往返（事件/备注/截图索引不丢）
- cave_join.load_session 接受目录包
- demo_export_samples.build_process：join 命中时填过程列、关键帧按 request_id 过滤
- 样本 schema 接受 process 新键与五段 provenance
"""

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp            # noqa: E402
import cave_join                            # noqa: E402
import demo_export_samples as des          # noqa: E402

PNG_1PX = base64.b64encode(base64.b64decode(
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
    b'z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==')).decode()


def _pack():
    t0 = 1_790_000_000_000
    events = [
        {'seq': 1, 't_wall': t0, 'type': 'session_start',
         'session_id': 'sess_test', 'collector_version': '0.6.0'},
        {'seq': 2, 't_wall': t0 + 500, 'type': 'state_sample',
         'viewpoint': {'state_id': 1, 'position': [10, 20, 30],
                       'position_nm': [40, 80, 1200],
                       'nm_per_px': {'x': 24, 'y': 24}}},
        {'seq': 3, 't_wall': t0 + 1000, 'type': 'heartbeat',
         'monotonic_ms': 1000, 'visible': True, 'focused': True,
         'online': True, 'not_for': 'thinking_time'},
        # 故意跳 seq，检查断档检测
        {'seq': 6, 't_wall': t0 + 2000, 'type': 'heartbeat',
         'monotonic_ms': 2000, 'visible': False, 'focused': False,
         'online': True},
        {'seq': 7, 't_wall': t0 + 2200, 'type': 'api_request',
         'kind': 'merge', 'request_id': 'req-abc', 'state_id': 1},
        {'seq': 8, 't_wall': t0 + 2400, 'type': 'api_operation_id',
         'kind': 'merge', 'op_id': 1956846, 'request_id': 'req-abc'},
        {'seq': 9, 't_wall': t0 + 2500, 'type': 'keyframe',
         'shot_id': 'shot_1', 'moment': 'pre_submit',
         'request_id': 'req-abc', 'capture_status': 'captured',
         'image_ref': 'shot_1.png'},
        {'seq': 10, 't_wall': t0 + 3000, 'type': 'session_stop'},
    ]
    return {
        'schema': 'cave_recorder.session.v0.2',
        'collector_version': '0.6.0',
        'session_id': 'sess_test',
        'page': {'url': 'http://localhost/v/1/'},
        'viewer': {'detected': True},
        'time': {'start_wall_ms': t0, 'end_wall_ms': t0 + 3000},
        'collector': {'version': '0.6.0',
                      'gaps': [{'t_wall': t0 + 1500,
                                'reason': 'page_hidden'}],
                      'pending_writes': [],
                      'capabilities': {'heartbeat': 'available（5s）'}},
        'counts': {'heartbeat': 2},
        # D10：采样配置必须随包走
        'config': {'move_sample_ms': 50, 'move_mode': 'sampled',
                   'wheel_burst_gap_ms': 400, 'endpoints': 'full'},
        'events': events,
        'notes': [{'note_id': 'note_1', 't_wall': t0 + 2100,
                   'text': '确认合并位置', 'state_id': 1}],
        'shots': [{'shot_id': 'shot_1', 't_wall': t0 + 2500, 'w': 1280,
                   'h': 800, 'ok': True, 'capture_status': 'captured',
                   'moment': 'pre_submit', 'request_id': 'req-abc',
                   'state_id': 1, 'dataURL': 'data:image/png;base64,' + PNG_1PX}],
        'errors': [],
    }


class TestSessionPack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_pack_layout_and_health(self):
        d, man = csp.pack_session(_pack(), self.base)
        self.assertTrue((d / 'manifest.json').exists())
        self.assertTrue((d / 'client' / 'events.jsonl').exists())
        self.assertTrue((d / 'client' / 'notes.jsonl').exists())
        self.assertTrue((d / 'client' / 'shots' / 'shots.jsonl').exists())
        self.assertTrue((d / 'client' / 'shots' / 'shot_1.png').exists())
        self.assertTrue((d / 'derived').is_dir())

        h = man['health']
        self.assertEqual(h['n_events'], 8)
        self.assertEqual(h['seq_gaps'], [[4, 5]])
        # 心跳按 F26/RF14 保留，但只用于“发现节流/断采”，不推导思考时长
        self.assertEqual(h['n_heartbeats'], 2)
        self.assertEqual(h['heartbeat_median_ms'], 1000)
        self.assertEqual(len(h['gaps']), 1)
        self.assertEqual(man['integrity']['n_png'], 1)
        self.assertTrue(man['integrity']['events_sha256'])
        # D10：采样粒度随包走，下游不得靠默认值猜
        self.assertEqual(man['config']['move_sample_ms'], 50)
        self.assertEqual(man['config']['move_mode'], 'sampled')

    def test_health_reports_overflow_and_gap_duration(self):
        p = _pack()
        p['events'][-1] = {
            'seq': 10, 't_wall': 1_790_000_003_000, 'type': 'session_stop',
            'counts': {'dropped': 12, 'checkpoint_dropped': 2},
            'overflow': {'events': {'total': 12,
                                    'first_t_wall': 1_790_000_002_000,
                                    'limit': 200000}},
            'gaps': [
                {'t_wall': 1_790_000_001_000, 'reason': 'page_hidden',
                 't_end': 1_790_000_001_800},
                {'t_wall': 1_790_000_002_500, 'reason': 'network_offline'},
            ],
        }
        _, man = csp.pack_session(p, self.base)
        h = man['health']
        self.assertEqual(h['dropped'], 12)
        self.assertEqual(h['checkpoint_dropped'], 2)
        self.assertEqual(h['overflow']['events']['total'], 12)
        # 已闭合的缺口给时长，未闭合的单列（不假装知道它多久结束）
        self.assertEqual(h['gaps_total_ms'], 800)
        self.assertEqual(h['gaps_open'], 1)

    def test_provenance_defaults_unknown(self):
        _, man = csp.pack_session(_pack(), self.base)
        for leg in csp.LEGS:
            self.assertEqual(man['provenance'][leg]['status'], 'unknown')
            self.assertIsNone(man['provenance'][leg]['reason'])

    def test_provenance_explicit(self):
        legs = {'submit_execution': 'simulated', 'service_facts': 'real'}
        reasons = {'submit_execution': '公开栈只读'}
        _, man = csp.pack_session(_pack(), self.base, legs, reasons)
        self.assertEqual(man['provenance']['submit_execution']['status'],
                         'simulated')
        self.assertEqual(man['provenance']['service_facts']['status'], 'real')
        self.assertEqual(man['provenance']['join']['status'], 'unknown')

    def test_provenance_rejects_bad_value(self):
        with self.assertRaises(SystemExit):
            csp.pack_session(_pack(), self.base, {'join': 'maybe'})

    def test_same_session_overwrites(self):
        # 同一个会话存两次 = 覆盖那一份（否则一次会话会多出 __时间戳 的副本）
        d1, _ = csp.pack_session(_pack(), self.base)
        d2, _ = csp.pack_session(_pack(), self.base)
        self.assertEqual(d1, d2)
        self.assertTrue(d1.exists())
        # 旧包不直接丢：同 session 重存先备份到 .old_*
        self.assertTrue(list(self.base.glob('.old_*')))

    def test_failed_resave_keeps_old_pack(self):
        # 临时目录写完之前失败，正式目录必须原样保留（不能出现半包）
        d1, _ = csp.pack_session(_pack(), self.base)
        before = (d1 / 'manifest.json').read_bytes()
        with self.assertRaises(SystemExit):
            csp.pack_session(_pack(), self.base, {'join': 'not-a-valid-leg'})
        self.assertTrue(d1.exists())
        self.assertEqual((d1 / 'manifest.json').read_bytes(), before)
        self.assertFalse(list(self.base.glob('.tmp_*')))

    def test_failed_new_pack_leaves_no_final(self):
        # 第一次保存就失败：不能留下正式目录，也不能留下临时目录
        with self.assertRaises(SystemExit):
            csp.pack_session(_pack(), self.base, {'join': 'bad'})
        self.assertFalse((self.base / 'sess_test').exists())
        self.assertFalse(list(self.base.glob('.tmp_*')))

    def test_successful_resave_leaves_no_staging(self):
        csp.pack_session(_pack(), self.base)
        csp.pack_session(_pack(), self.base)
        self.assertFalse(list(self.base.glob('.tmp_*')))
        self.assertTrue(list(self.base.glob('.old_*')))

    def test_legacy_focus_event_is_flattened(self):
        events = [{'type': 'viewer.focus',
                   'position': {'position': [1, 2, 3], 'page': [10, 20],
                                'picked_miss': True}}]
        csp._normalize_focus_events(events)
        self.assertEqual(events[0]['position'], [1, 2, 3])
        self.assertEqual(events[0]['page'], [10, 20])
        self.assertTrue(events[0]['picked_miss'])

    def test_export_csv_has_three_tables(self):
        import csv
        import export_csv
        d, _ = csp.pack_session(_pack(), self.base, with_html=False)
        s = export_csv.export(d, self.base / 'csv')
        self.assertEqual(s['n_events'], 8)
        self.assertTrue((self.base / 'csv' / 'events.csv').exists())
        self.assertTrue((self.base / 'csv' / 'episodes.csv').exists())
        self.assertTrue((self.base / 'csv' / 'hover.csv').exists())
        rows = list(csv.DictReader(
            (self.base / 'csv' / 'events.csv').open(encoding='utf-8')))
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]['type'], 'session.start')

    def test_verify_pack_png_and_text_hashes(self):
        import inspect_pack as ip
        d, _ = csp.pack_session(_pack(), self.base, with_html=False)
        v = ip.verify_pack(d)
        self.assertTrue(v['ok'], v['errors'])
        # 改事件流：文本哈希必须失败
        with (d / 'client' / 'events.jsonl').open('a', encoding='utf-8') as f:
            f.write('{"seq":99,"type":"tamper"}\n')
        v2 = ip.verify_pack(d)
        self.assertFalse(v2['ok'])
        self.assertTrue(any('events.jsonl SHA256' in e for e in v2['errors']))

    def test_other_session_gets_suffix(self):
        # 不同会话占了同名目录 → 追加时间戳，不覆盖别人的
        d1, _ = csp.pack_session(_pack(), self.base)
        other = _pack()
        other['session_id'] = 'sess_other'
        d2, _ = csp.pack_session(other, self.base, extra_manifest={
            'session_id': 'sess_other'})
        self.assertNotEqual(d1, d2)
        self.assertTrue(d1.exists() and d2.exists())

    def test_dir_roundtrip(self):
        d, _ = csp.pack_session(_pack(), self.base)
        back = csp.load_session(d)
        self.assertEqual(len(back['events']), 8)
        self.assertEqual(back['events'][5]['op_id'], 1956846)
        self.assertEqual(back['notes'][0]['text'], '确认合并位置')
        self.assertEqual(back['shots'][0]['shot_id'], 'shot_1')
        self.assertEqual(back['collector_version'], '0.6.0')

    def test_join_accepts_dir(self):
        d, _ = csp.pack_session(_pack(), self.base)
        pack = cave_join.load_session(d)
        ops = [{'op_id': 1956846, 'type': 'merge',
                'timestamp': '2025-12-08T07:19:51+00:00',
                'before_root_ids': ['1', '2'], 'after_root_ids': ['3']}]
        out = cave_join.join_session(pack, ops)
        self.assertEqual(out['summary']['n_matched'], 1)
        rec = out['joined'][0]
        self.assertEqual(rec['match_method'], 'exact-anchor')
        self.assertEqual(rec['anchor']['request_id'], 'req-abc')
        self.assertEqual(out['summary']['client_request_ids'], ['req-abc'])
        self.assertEqual(out['summary']['health']['n_heartbeats'], 2)
        self.assertEqual(out['summary']['health']['seq_gaps'], [[4, 5]])

    def test_ctype_maps_legacy_names(self):
        """旧包（下划线名）必须能被当成新名读：否则改名就是破坏性变更。"""
        self.assertEqual(csp.ctype({'type': 'state_sample'}),
                         'viewer.state-change')
        self.assertEqual(csp.ctype({'type': 'api_operation_id'}),
                         'edit.operation')
        self.assertEqual(csp.ctype({'type': 'pointer'}), 'input.pointer')
        self.assertEqual(csp.ctype({'type': 'gesture'}), 'viewport.gesture')
        # 新名原样返回（含不在映射表里的工作台事件）
        self.assertEqual(csp.ctype({'type': 'viewer.checkpoint'}),
                         'viewer.checkpoint')
        self.assertEqual(csp.ctype({'type': 'candidate_decision'}),
                         'candidate_decision')


class TestSampleProcess(unittest.TestCase):
    def _join_rec(self):
        return {
            1956846: {
                'op_id': 1956846, 'session_id': 'sess_test',
                'match_method': 'exact-anchor',
                'clock_offset_ms': -1234.5, 'clock_uncertainty_ms': 250.0,
                'anchor': {'request_id': 'req-abc'},
                'context': {
                    'window': {'pre_s': 15.0, 'post_s': 3.0, 'n_events': 9},
                    'notes': [{'text': '确认合并位置'}],
                    'multicuts': [{'sources': 1}],
                    'viewpoint_before': {'position': [10, 20, 30],
                                         'crossSectionScale': 6},
                },
            }
        }

    def test_process_filled_from_join(self):
        shots = [
            {'shot_id': 'shot_1', 'moment': 'pre_submit',
             'request_id': 'req-abc', 'image_ref': 'shots/shot_1.png',
             'capture_status': 'captured'},
            {'shot_id': 'shot_2', 'moment': 'post_submit',
             'request_id': 'other', 'image_ref': None,
             'capture_status': 'blank_readback'},
        ]
        prov = {'submit_execution': {'status': 'simulated'}}
        p = des.build_process(1956846, self._join_rec(), prov, shots)
        self.assertEqual(p['session_id'], 'sess_test')
        self.assertEqual(p['human_decision'], 'accept')
        self.assertEqual(p['human_note'], '确认合并位置')
        self.assertIn('仿真', p['note'])
        self.assertEqual(p['process_ref']['client_request_id'], 'req-abc')
        self.assertEqual(p['viewpoint_before']['crossSectionScale'], 6)
        self.assertEqual(len(p['keyframes']), 1)   # 只留同一 request_id 的
        self.assertEqual(p['keyframes'][0]['moment'], 'pre_submit')

    def test_process_null_placeholder(self):
        p = des.build_process(999, {}, {}, [])
        self.assertIsNone(p['session_id'])
        self.assertEqual(p['keyframes'], [])
        self.assertIn('不推断', p['note'])


class TestSampleSchema(unittest.TestCase):
    @staticmethod
    def _valid_sample():
        return {
            'schema': 'neuromancer.sample.v0.1',
            'source': {'datastack': 'minnie65_public', 'op_id': 1,
                       'op_type': 'merge', 'user': 'u',
                       'timestamp_utc': None},
            'observation': {
                'render_params': {'S_nm': 3584.0, 'mip': 1},
                'images': {'em_xy': 'a', 'em_xz': 'b', 'em_yz': 'c',
                           'overlay_xz': 'd'},
                'interface_point_nm': [1, 2, 3],
                'viewpoint': {'position': [1, 2, 3]},
                'note': 'n'},
            'action': {'type': 'merge', 'before_root_ids': [1, 2],
                       'after_root_ids': [3], 'source_coords': None,
                       'sink_coords': None},
            'segments': {'segment1_id': 1, 'segment2_id': 2,
                         'semantics': 's'},
            'outcome': {'state': 'latest', 'survival_seconds': None,
                        'next_change': None, 'checked_at': None},
            'process': {
                'session_id': 's', 'human_decision': 'accept',
                'human_note': None, 'note': 'n',
                'process_ref': {'match_method': 'exact-anchor'},
                'viewpoint_before': {'position': [1, 2, 3]},
                'window': {'pre_s': 15.0, 'post_s': 3.0, 'n_events': 5},
                'n_notes': 0, 'n_multicuts': 0,
                'keyframes': [{'shot_id': 'shot_1',
                               'capture_status': 'captured'}]},
            'labels': {'action': 'merge', 'outcome_state': 'latest',
                       'usage_hint': 'h'},
            'provenance': {
                'service_facts': {'status': 'real', 'reason': 'r'},
                'visual_assets': {'status': 'real', 'reason': 'r'},
                'client_process': {'status': 'real', 'reason': 'r'},
                'submit_execution': {'status': 'simulated', 'reason': 'r'},
                'join': {'status': 'simulated', 'reason': 'r'}},
        }

    def test_schema_accepts_process_and_provenance(self):
        from jsonschema import Draft202012Validator
        schema = json.loads(
            (HERE.parent / 'schemas' / 'sample_v0.1.schema.json')
            .read_text(encoding='utf-8'))
        errs = list(Draft202012Validator(schema)
                    .iter_errors(self._valid_sample()))
        self.assertEqual(errs, [], [e.message for e in errs])

    def test_schema_rejects_bad_leg_status(self):
        from jsonschema import Draft202012Validator
        schema = json.loads(
            (HERE.parent / 'schemas' / 'sample_v0.1.schema.json')
            .read_text(encoding='utf-8'))
        bad = self._valid_sample()
        bad['provenance']['join']['status'] = 'probably'
        errs = list(Draft202012Validator(schema).iter_errors(bad))
        self.assertTrue(errs)

    def test_schema_rejects_unknown_process_key(self):
        from jsonschema import Draft202012Validator
        schema = json.loads(
            (HERE.parent / 'schemas' / 'sample_v0.1.schema.json')
            .read_text(encoding='utf-8'))
        bad = self._valid_sample()
        bad['process']['invented_key'] = 1
        errs = list(Draft202012Validator(schema).iter_errors(bad))
        self.assertTrue(errs)


class TestSessionDirname(unittest.TestCase):
    """会话目录名：保留中文，挡住会出事的字符。"""

    def test_keeps_chinese(self):
        from cave_session_pack import _safe_dirname
        self.assertEqual(_safe_dirname('sess_演示_测试_1'), 'sess_演示_测试_1')

    def test_blocks_path_separators(self):
        from cave_session_pack import _safe_dirname
        self.assertEqual(_safe_dirname('a/../b'), 'a_.._b')
        self.assertEqual(_safe_dirname('x\\y'), 'x_y')

    def test_blocks_url_and_shell_unfriendly(self):
        from cave_session_pack import _safe_dirname
        for bad in ('sess_a#b', 'sess_a?b', 'sess_a:b', 'sess_a*b'):
            out = _safe_dirname(bad)
            self.assertNotIn('#', out)
            self.assertNotIn('?', out)
            self.assertNotIn(':', out)
            self.assertNotIn('*', out)

    def test_whitespace_to_underscore(self):
        from cave_session_pack import _safe_dirname
        self.assertEqual(_safe_dirname(' sess_ a  b '), 'sess__a_b')

    def test_empty_fallback(self):
        from cave_session_pack import _safe_dirname
        self.assertEqual(_safe_dirname(''), 'session')
        self.assertEqual(_safe_dirname('...'), 'session')

    def test_shot_names_stay_ascii(self):
        """shot 文件名仍走 _safe_name：中文会变下划线（图片名不用看懂）。"""
        from cave_session_pack import _safe_name
        self.assertEqual(_safe_name('sess_演示', 'shot'), 'sess__')


class TestPackOps(unittest.TestCase):
    """工作台首页的"改名 / 删除"：只动会话目录，删除是真删。"""

    def setUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        for n in ('sess_a', 'sess_b'):
            d = self.root / n
            d.mkdir()
            (d / 'manifest.json').write_text('{"session_id": "%s"}' % n)
        self._old = os.environ.get('NM_SESSIONS')
        os.environ['NM_SESSIONS'] = str(self.root)

    def tearDown(self):
        import os
        if self._old is None:
            os.environ.pop('NM_SESSIONS', None)
        else:
            os.environ['NM_SESSIONS'] = self._old
        self._tmp.cleanup()

    def test_rename_keeps_chinese(self):
        import cave_replay_studio as cs
        r = cs.pack_op('rename', str(self.root / 'sess_a'), '小鼠皮层_第3轮')
        self.assertTrue(r.get('ok'))
        self.assertEqual(r['name'], '小鼠皮层_第3轮')
        self.assertTrue((self.root / '小鼠皮层_第3轮' / 'manifest.json').exists())
        self.assertFalse((self.root / 'sess_a').exists())

    def test_rename_refuses_existing(self):
        import cave_replay_studio as cs
        r = cs.pack_op('rename', str(self.root / 'sess_a'), 'sess_b')
        self.assertIn('同名已存在', r.get('err', ''))
        self.assertTrue((self.root / 'sess_a').exists())

    def test_rename_refuses_bad_name(self):
        import cave_replay_studio as cs
        r = cs.pack_op('rename', str(self.root / 'sess_a'), '///')
        self.assertIn('名字不合法', r.get('err', ''))

    def test_refuses_path_outside_root(self):
        import cave_replay_studio as cs
        r = cs.pack_op('rename', '/etc', 'x')
        self.assertIn('只能操作', r.get('err', ''))

    def test_delete_moves_to_trash_and_purge_removes(self):
        import cave_replay_studio as cs
        r = cs.pack_op('delete', str(self.root / 'sess_b'))
        self.assertTrue(r.get('trashed'))
        self.assertFalse((self.root / 'sess_b').exists())
        trashed = list((self.root / '_trash').iterdir())
        self.assertEqual(len(trashed), 1)
        r2 = cs.pack_op('purge', str(trashed[0]))
        self.assertTrue(r2.get('deleted'))
        self.assertEqual(list((self.root / '_trash').iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()
                                if p.name != '_trash'), ['sess_a'])

    def test_list_dirs_skips_hidden(self):
        import cave_replay_studio as cs
        (self.root / '.DS_Store').write_text('x')
        (self.root / '.hidden').mkdir()
        (self.root / '_draft_sess_x').mkdir()
        (self.root / '_trash').mkdir()
        names = sorted(d.name for d in cs.list_pack_dirs(self.root))
        self.assertEqual(names, ['sess_a', 'sess_b'])


class TestNxArgs(unittest.TestCase):
    def test_option_values_are_not_positional_packs(self):
        import nx
        self.assertFalse(nx._has_positional(['--seek-t', '140']))
        self.assertFalse(nx._has_positional(['--out', '/tmp/x']))
        self.assertTrue(nx._has_positional(['--seek-t', '140', 'sess_x']))
        self.assertTrue(nx._has_positional(['sess_x', '--headed']))


class TestDraftCleanup(unittest.TestCase):
    """正式保存后必须真的删掉草稿（草稿里有 shots/ 子目录，容易删不干净）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_drop_draft_removes_shots_subdir(self):
        import cave_manual_server as cms
        pack = {
            'session_id': 'sess_drop',
            'events': [{'seq': 1, 'type': 'session.start'}],
            'notes': [],
            'shots': [{'shot_id': 'shot_1',
                       'dataURL': 'data:image/png;base64,' + PNG_1PX}],
        }
        cms._write_draft(pack, self.root)
        d = cms._draft_dir('sess_drop', self.root)
        self.assertTrue((d / 'shots' / 'shot_1.png').is_file())
        cms._drop_draft('sess_drop', self.root)
        self.assertFalse(d.exists())

    def test_latest_pack_skips_draft_and_trash(self):
        import cave_manual_server as cms
        (self.root / 'sess_a').mkdir()
        (self.root / 'sess_a' / 'manifest.json').write_text('{}')
        (self.root / '_draft_sess_b').mkdir()
        (self.root / '_trash').mkdir()
        self.assertEqual(cms._latest_pack(self.root).name, 'sess_a')


if __name__ == '__main__':
    unittest.main()
