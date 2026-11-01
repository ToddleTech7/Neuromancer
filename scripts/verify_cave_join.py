"""cave_join 离线验证：夹具 A/B/C + 真实数据冒烟（如实边界）。

用法（仓库根目录）：
    python scripts/verify_cave_join.py

夹具（就地构造，不落盘数据；装置性质，不代表真实会话）：
- A 全链夹具：op_id 锚点（exact-anchor）+ 无锚点估计（anchor-estimated）
  + 超窗（unmatched）+ 失败写（rejected_writes）+ api_error + 上下文窗口
- B 日期估计夹具：只有响应 Date 头锚点（server-date basis）
- C 窗口回退夹具：无任何锚点（window 方法、不确定度如实为空）
- D 真实数据冒烟：最新 hooks 会话 × 真实 ops.jsonl
  （时间不相交 → 应 0 匹配；客户端锚点应列为 server 缺失）

输出：results/joined/verify_join_<ts>.json（checks 全量记录）
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

# 被检查的 CLI 子进程打中文：不给两端统一 UTF-8，
# 中文 Windows（PYTHONIOENCODING=cp936）下管道解码直接失败（夹具 E）。
os.environ['PYTHONUTF8'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'

import cave_join as cj  # noqa: E402

OUT = ROOT / 'results' / 'joined'
OUT.mkdir(parents=True, exist_ok=True)
TS = datetime.now().strftime('%Y%m%d_%H%M%S')

T0 = 1_700_000_000_000        # 夹具会话起点（ms，任意基准）
OFF = 7_200_000               # 夹具时钟偏移：服务端比客户端快 2 小时

checks = []


def check(name, cond, detail=''):
    checks.append({'name': name, 'ok': bool(cond), 'detail': str(detail)[:240]})
    print(f'  [{"PASS" if cond else "FAIL"}] {name}'
          + (f' — {detail}' if detail else ''))


def ev(typ, t, **kw):
    d = {'type': typ, 't_wall': t, 't_mono': t - T0, 'seq': 0}
    d.update(kw)
    return d


def pack_a():
    return {
        'schema': 'cave_recorder.session.v0.1',
        'session_id': 'fixture_A',
        'time': {'start_wall_ms': T0, 'end_wall_ms': T0 + 120_000},
        'events': [
            ev('session_start', T0),
            ev('state_sample', T0 + 5_000,
               viewpoint={'position': [10, 20, 30], 'crossSectionScale': 4}),
            ev('selection_sample', T0 + 20_000,
               selection={'em': {'count': 2, 'ids': ['100', '200']}}),
            ev('state_sample', T0 + 25_000,
               viewpoint={'position': [11, 21, 31], 'crossSectionScale': 3}),
            ev('api_request', T0 + 30_000, kind='merge', method='POST',
               url='/segmentation/api/v1/table/t/operation/do_merge'),
            ev('api_response', T0 + 30_500, kind='merge', status=200,
               server_date_ms=T0 + 30_500 + OFF),
            ev('api_operation_id', T0 + 30_600, kind='merge', op_id=111,
               via='fetch'),
            ev('state_sample', T0 + 31_000,
               viewpoint={'position': [12, 22, 32], 'crossSectionScale': 3}),
            ev('note', T0 + 31_500, text='fixture note'),
            ev('api_request', T0 + 60_000, kind='merge', method='POST'),
            ev('api_response', T0 + 60_500, kind='merge', status=409),
            ev('api_error', T0 + 80_000, kind='split', err='net fail'),
            ev('session_stop', T0 + 120_000),
        ],
    }


def ops_a():
    def op(op_id, ts, before, after, typ='merge'):
        return {'op_id': op_id, 'type': typ,
                'timestamp': ts, 'user': 'u1',
                'before_root_ids': before, 'after_root_ids': after}
    # 服务端时间 = 客户端 + OFF
    return [
        op(111, _iso(T0 + 30_600 + OFF), [100, 200], [300]),
        op(222, _iso(T0 + 40_000 + OFF), [400], [500]),
        op(333, _iso(T0 + 400_000 + OFF), [600], [700], typ='split'),
    ]


def _iso(ms):
    from datetime import datetime as _dt, timezone as _tz
    return _dt.fromtimestamp(ms / 1000, tz=_tz.utc).isoformat()


print('== 夹具 A：全链（exact + estimated + unmatched）==')
res_a = cj.join_session(pack_a(), ops_a())
sa = res_a['summary']
joined_a = {r['op_id']: r for r in res_a['joined']}

check('A: clock basis=op-anchor', sa['clock']['basis'] == 'op-anchor',
      sa['clock']['basis'])
check('A: 偏移=+7200000ms（单样本，±250）',
      sa['clock']['offset_ms'] == OFF and sa['clock']['uncertainty_ms'] == 250,
      sa['clock'])
check('A: op111 exact-anchor', joined_a.get(111, {}).get('match_method')
      == 'exact-anchor', joined_a.get(111, {}).get('match_method'))
check('A: op111 root 证据命中（100/200 在选集中）',
      joined_a.get(111, {}).get('evidence', {})
      .get('root_id_seen_in_session') is True)
r111 = joined_a.get(111, {}).get('context', {})
check('A: op111 上下文含备注与前后视角',
      r111.get('notes') and r111.get('viewpoint_before')
      and r111.get('viewpoint_after'),
      json.dumps({'notes': r111.get('notes'),
                  'vb': r111.get('viewpoint_before'),
                  'va': r111.get('viewpoint_after')}, ensure_ascii=False)[:200])
check('A: op222 anchor-estimated（无自身锚点）',
      joined_a.get(222, {}).get('match_method') == 'anchor-estimated',
      joined_a.get(222, {}).get('match_method'))
check('A: op333 unmatched（超窗）',
      any(u['op_id'] == 333 for u in sa['unmatched_ops']),
      sa['unmatched_ops'])
check('A: anchors_not_on_server 为空',
      sa['anchors_not_on_server'] == [], sa['anchors_not_on_server'])
check('A: rejected_writes 捕获 409 写请求',
      len(sa['rejected_writes']) == 1
      and sa['rejected_writes'][0]['status'] == 409, sa['rejected_writes'])
check('A: api_errors 捕获 1 条', len(sa['api_errors']) == 1)

print('== 夹具 B：日期估计（server-date basis）==')
pack_b = {
    'schema': 'cave_recorder.session.v0.1', 'session_id': 'fixture_B',
    'time': {'start_wall_ms': T0, 'end_wall_ms': T0 + 60_000},
    'events': [
        ev('session_start', T0),
        ev('state_sample', T0 + 1_000, viewpoint={'position': [1, 2, 3]}),
        ev('api_response', T0 + 10_200, kind='read', status=200,
           server_date_ms=T0 + 10_200 + 3_400),
        ev('api_response', T0 + 30_200, kind='read', status=200,
           server_date_ms=T0 + 30_200 + 3_400),
        ev('session_stop', T0 + 60_000),
    ],
}
ops_b = [{'op_id': 501, 'type': 'split', 'timestamp': _iso(T0 + 20_000 + 3_400),
          'user': 'u2', 'before_root_ids': [9], 'after_root_ids': [8, 7]}]
res_b = cj.join_session(pack_b, ops_b)
sb = res_b['summary']
check('B: basis=server-date', sb['clock']['basis'] == 'server-date',
      sb['clock']['basis'])
check('B: 偏移中位数≈3400ms',
      abs(sb['clock']['offset_ms'] - 3_400) < 1, sb['clock']['offset_ms'])
check('B: op 匹配 anchor-estimated',
      res_b['joined'] and res_b['joined'][0]['match_method']
      == 'anchor-estimated',
      res_b['joined'][0]['match_method'] if res_b['joined'] else None)

print('== 夹具 C：窗口回退（window 方法）==')
pack_c = {
    'schema': 'cave_recorder.session.v0.1', 'session_id': 'fixture_C',
    'time': {'start_wall_ms': T0, 'end_wall_ms': T0 + 60_000},
    'events': [
        ev('session_start', T0),
        ev('selection_sample', T0 + 30_000,
           selection={'em': {'count': 1, 'ids': ['888']}}),
        ev('session_stop', T0 + 60_000),
    ],
}
ops_c = [
    {'op_id': 601, 'type': 'merge', 'timestamp': _iso(T0 + 31_000),
     'user': 'u3', 'before_root_ids': [888], 'after_root_ids': [999]},
    {'op_id': 602, 'type': 'merge', 'timestamp': _iso(T0 + 300_000),
     'user': 'u3', 'before_root_ids': [1], 'after_root_ids': [2]},
]
res_c = cj.join_session(pack_c, ops_c)
sc = res_c['summary']
j601 = next((r for r in res_c['joined'] if r['op_id'] == 601), {})
check('C: 无锚点 → basis=none、不确定度为空',
      sc['clock']['basis'] == 'none' and sc['clock']['uncertainty_ms'] is None,
      sc['clock'])
check('C: op601 匹配 window 且 root 证据命中',
      j601.get('match_method') == 'window'
      and j601.get('evidence', {}).get('root_id_seen_in_session') is True,
      j601.get('match_method'))
check('C: op602 unmatched',
      any(u['op_id'] == 602 for u in sc['unmatched_ops']))

print('== 夹具 D：真实数据冒烟（hooks 会话 × 真实 ops）==')
hooks = sorted((ROOT / 'results' / 'recorder_verify').glob(
    'session_hooks_*.json'), key=lambda p: p.stat().st_mtime)
if not hooks:
    checks.append({'name': 'D: hooks 会话存在（跳过）', 'ok': True,
                   'detail': 'no session_hooks_*.json，跳过'})
    print('  [SKIP] 无 hooks 会话，跳过该节')
else:
    real_ops_path = ROOT / 'results' / 'cave_watch' / 'ops.jsonl'
    if not real_ops_path.exists():
        # 这份 ops 是早期从真实 CAVE 抓的样本（不可再生）。2026-10-29 整理
        # results/ 时被误删——不能拿合成 ops 冒充，所以这里**如实跳过**。
        checks.append({'name': 'D: 真实 ops 冒烟（跳过：ops.jsonl 不在）',
                       'ok': True,
                       'detail': 'results/cave_watch/ops.jsonl 缺失'})
        print('  [SKIP] results/cave_watch/ops.jsonl 不存在，跳过真实 ops 冒烟')
        print('         （要恢复只能从真实 CAVE 的 op log 再导一份）')
    else:
        pack_d = cj.load_session(hooks[-1])
        real_ops = cj.load_ops(real_ops_path)
        res_d = cj.join_session(pack_d, real_ops)
        sd = res_d['summary']
        check('D: 真实数据运行无异常', True, hooks[-1].name)
        check('D: 时间不相交 → 0 匹配', sd['n_matched'] == 0,
              sd['n_matched'])
        aids = {a.get('op_id') for a in sd['anchors_not_on_server']}
        check('D: 客户端锚点列为服务端缺失（424242/424243）',
              {424242, 424243} <= aids, sorted(aids))
        check('D: 报告字段完整',
              all(k in sd for k in ('clock', 'unmatched_ops', 'rejected_writes',
                                    'api_errors')))

print('== 夹具 E：CLI 集成（cave_join → cave_session_replay HTML）==')
import subprocess  # noqa: E402
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    tdp = Path(td)
    sess = tdp / 'session.json'
    sess.write_text(json.dumps(pack_a(), ensure_ascii=False), encoding='utf-8')
    opsf = tdp / 'ops.jsonl'
    opsf.write_text('\n'.join(json.dumps(o) for o in ops_a()),
                    encoding='utf-8')
    r1 = subprocess.run(
        [sys.executable, str(HERE / 'cave_join.py'), str(sess),
         '--ops', str(opsf), '--out', td],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120)
    jl = tdp / 'fixture_A.join.jsonl'
    sm = tdp / 'fixture_A.join_summary.json'
    check('E: join CLI exit 0 且产出 jsonl+summary',
          r1.returncode == 0 and jl.exists() and sm.exists(),
          ((r1.stdout or '')[-200:] + (r1.stderr or '')[-200:]).replace('\n', ' '))
    check('E: join CLI 输出 matched 2', 'matched 2' in (r1.stdout or ''),
          (r1.stdout or '')[-160:])

    r2 = subprocess.run(
        [sys.executable, str(HERE / 'cave_session_replay.py'),
         str(sess), '--join', str(jl)],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120)
    htmlp = ROOT / 'results' / 'replay' / 'fixture_A.html'
    html_txt = htmlp.read_text(encoding='utf-8') if htmlp.exists() else ''
    check('E: replay CLI 生成 HTML（含 join 段与 op 行）',
          r2.returncode == 0 and '服务端 op 对照' in html_txt
          and 'op:merge' in html_txt and 'exact-anchor' in html_txt,
          f'rc={r2.returncode} html_len={len(html_txt)}')
    check('E: replay CLI 时间线出现客户侧锚点事件',
          'edit.operation' in html_txt and 'op_id=111' in html_txt)

report = {'ts': TS, 'n_checks': len(checks),
          'n_pass': sum(1 for c in checks if c['ok']),
          'checks': checks, 'pass': all(c['ok'] for c in checks)}
fn = OUT / f'verify_join_{TS}.json'
fn.write_text(json.dumps(report, ensure_ascii=False, indent=1),
              encoding='utf-8')
print()
print(f'checks: {report["n_pass"]}/{report["n_checks"]}')
print('report:', fn)
print('RESULT:', 'PASS' if report['pass'] else 'FAIL')
sys.exit(0 if report['pass'] else 1)
