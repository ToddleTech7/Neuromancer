"""cave_join：会话包（客户端过程）↔ op log（服务端权威）关联器。

契约（见 docs/CAVE_PROCESS_COLLECTION_DESIGN.md §3.4）：
  键：session_id + user + root_id(s) + 时间戳；时钟以"提交锚点"估计偏移；
  join 结果标记方式（exact-anchor / anchor-estimated / window），
  不把估计当精确；无法匹配的如实列出，不推断填补。

输入：
- 会话包 JSON（recorder 导出，schema cave_recorder.session.v0.1/v0.2）
  或目录包（cave_session_pack.py 产出：manifest.json + client/events.jsonl）
- ops.jsonl（op_watcher 产出）

匹配规则（按强度降序）：
1. exact-anchor     —— 客户端 api_operation_id 事件与服务端 op_id 相同
                        （偏移样本 = 服务端时间 − 客户端锚点时间）
2. anchor-estimated —— 该 op 无 op_id 锚点，但会话有时钟估计
                        （来自其他 op 锚点或响应 Date 头），且 op 时间
                        落在会话跨度内 → 按估计偏移换算后关联
3. window           —— 无任何时钟估计（按零偏移），仅时间窗重叠
                        （不确定度 = 窗口大小，最弱等级）

客户端侧证据（不冒充服务端事实，单独列出）：
- anchors_not_on_server：客户端看到写回执/operation_id，但 ops.jsonl 里
  没有该 op（可能被拒、可能落在拉取窗口之外、可能回执是缓存——如实并列）
- rejected_writes：api_response 里写接口 status >= 400（客户端观察到的
  失败写请求，是 attempts 的客户端侧证据；服务端中间件日志仍为佳）
- api_errors：api_error 事件（网络层失败）

如实边界：selection_sample 只存了每层前 8 个 id（recorder 设计），
root_seen 证据因此是下界；root 匹配只作辅助证据，不单独决定 match_method。

用法（仓库根目录）：
    python scripts/cave_join.py <session.json> \
        --ops results/cave_watch/ops.jsonl
输出：
    results/joined/<session_id>.join.jsonl
    results/joined/<session_id>.join_summary.json
（拒绝覆盖：同名文件已存在时追加时间戳后缀）
"""

import argparse
import datetime as dt
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

# 事件名兼容：recorder v0.7 起对齐需求清单 RF01–RF15（点号命名），
# 旧包用下划线名。全部比较走 _ctype()，不直接比 e['type']。
try:
    from cave_session_pack import ctype as _ctype
    from cave_session_pack import relpath as _relpath
except Exception:                      # 作为 scripts.* 包导入时
    from scripts.cave_session_pack import ctype as _ctype
    from scripts.cave_session_pack import relpath as _relpath

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OPS = ROOT / 'results' / 'cave_watch' / 'ops.jsonl'
OUT_DIR = ROOT / 'results' / 'joined'

WRITE_KINDS = {'merge', 'split', 'undo', 'write'}


def load_session(path):
    """读会话包：JSON（recorder 导出）或目录包（cave_session_pack）。"""
    try:
        from cave_session_pack import load_session as _load
    except ImportError:                       # 以模块方式被导入时
        from scripts.cave_session_pack import load_session as _load
    pack = _load(path)
    if 'events' not in pack:
        raise SystemExit(f'不是有效的会话包: {path}')
    return pack


def load_ops(path):
    ops = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            ops.append(json.loads(line))
    return ops


def parse_server_ts(value):
    """op 记录的时间戳 → epoch ms（UTC）。支持 ISO 字符串与数值毫秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        s = str(value).replace('Z', '+00:00')
        t = dt.datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t.timestamp() * 1000.0
    except ValueError:
        return None


def extract_anchors(pack):
    """从会话包提取锚点与客户端侧证据。"""
    op_anchors, server_dates, rejected, api_errors = [], [], [], []
    for e in pack.get('events') or []:
        typ = _ctype(e)
        if typ == 'edit.operation':
            op_anchors.append({
                'op_id': e.get('op_id'), 'kind': e.get('kind'),
                't_wall': e.get('t_wall'), 'via': e.get('via'),
                'request_id': e.get('request_id'),
                'status': e.get('status'),
            })
        elif typ in ('api.ack', 'edit.ack'):
            sd = e.get('server_date_ms')
            if sd is not None and e.get('t_wall') is not None:
                server_dates.append({
                    't_wall': e.get('t_wall'),
                    'server_date_ms': sd, 'kind': e.get('kind'),
                })
            st = e.get('status')
            if (e.get('kind') in WRITE_KINDS
                    and isinstance(st, (int, float)) and st >= 400):
                rejected.append({
                    'kind': e.get('kind'), 'status': st,
                    't_wall': e.get('t_wall'), 'url': e.get('url'),
                })
        elif typ == 'api.error':
            api_errors.append({
                'kind': e.get('kind'), 'err': e.get('err'),
                't_wall': e.get('t_wall'), 'url': e.get('url'),
                'request_id': e.get('request_id'),
            })
    return {'op_anchors': op_anchors, 'server_dates': server_dates,
            'rejected_writes': rejected, 'api_errors': api_errors}


def estimate_clock(anchors, ops_by_id):
    """估计会话时钟偏移（服务端 − 客户端），带方法标记与不确定度。

    优先级：op_id 精确锚点 > 响应 Date 头（秒级、含单程时延偏差）。
    样本不足时如实降级；无任何样本时 offset=0、basis='none'。
    """
    exact, dates = [], []
    for a in anchors['op_anchors']:
        op = (ops_by_id.get(str(a.get('op_id')))
              or ops_by_id.get(a.get('op_id')))
        if op is None or a.get('t_wall') is None:
            continue
        st = parse_server_ts(op.get('timestamp'))
        if st is None:
            continue
        exact.append({'op_id': a['op_id'], 'offset_ms': st - a['t_wall'],
                      't_wall': a['t_wall']})
    for d in anchors['server_dates']:
        # 响应 Date 头为服务器生成时刻；客户端收到时刻 ≥ 生成时刻，
        # 故 offset 估计略偏小（网络时延），仅作粗锚点。
        dates.append({'offset_ms': d['server_date_ms'] - d['t_wall'],
                      't_wall': d['t_wall']})

    if exact:
        vals = [s['offset_ms'] for s in exact]
        off = statistics.median(vals)
        if len(vals) >= 2:
            mad = statistics.median(abs(v - off) for v in vals)
            unc = max(50.0, mad * 1.4826 + 50.0)
        else:
            unc = 250.0
        return {'offset_ms': round(off, 1), 'uncertainty_ms': round(unc, 1),
                'basis': 'op-anchor', 'samples': exact}
    if dates:
        vals = [s['offset_ms'] for s in dates]
        off = statistics.median(vals)
        unc = 600.0 + max(vals) - min(vals)   # 秒级量化 + 时延抖动
        return {'offset_ms': round(off, 1), 'uncertainty_ms': round(unc, 1),
                'basis': 'server-date', 'samples': dates}
    return {'offset_ms': 0.0, 'uncertainty_ms': None,
            'basis': 'none', 'samples': []}


def session_span(pack):
    t = pack.get('time') or {}
    events = pack.get('events') or []
    t0 = t.get('start_wall_ms')
    t1 = t.get('end_wall_ms')
    if t0 is None and events:
        t0 = events[0].get('t_wall')
    if t1 is None and events:
        t1 = events[-1].get('t_wall')
    return t0, t1


def collect_selection_ids(pack):
    """收集选择事件中的 id（每层前 8 个，见如实边界）。"""
    ids = set()
    for e in pack.get('events') or []:
        if _ctype(e) != 'selection.change':
            continue
        sel = e.get('selection') or {}
        for _, v in sel.items():
            for i in (v or {}).get('ids') or []:
                ids.add(str(i))
    return ids


def _viewpoint_summary(vp):
    if not vp:
        return None
    return {'position': vp.get('position'),
            'crossSectionScale': vp.get('crossSectionScale'),
            'projectionScale': vp.get('projectionScale')}


def build_context(events, center_ms, pre_s, post_s):
    """提交锚点附近的事件窗口：看到什么（视角）→做了什么→提交了什么。"""
    lo, hi = center_ms - pre_s * 1000, center_ms + post_s * 1000
    win = [e for e in events if lo <= (e.get('t_wall') or 0) <= hi]
    before = [e for e in win if _ctype(e) == 'viewer.state-change'
              and (e.get('t_wall') or 0) <= center_ms]
    after = [e for e in win if _ctype(e) == 'viewer.state-change'
             and (e.get('t_wall') or 0) > center_ms]
    ctx = {
        'window': {'pre_s': pre_s, 'post_s': post_s, 'n_events': len(win)},
        'state_samples': sum(1 for e in win
                             if _ctype(e) == 'viewer.state-change'),
        'selections': sum(1 for e in win
                          if _ctype(e) == 'selection.change'),
        'gestures': [{'t_wall': e.get('t_wall'), 'kind': e.get('kind'),
                      'duration_ms': e.get('duration_ms'),
                      'view_changed': e.get('view_changed')}
                     for e in win if _ctype(e) == 'viewport.gesture'],
        'multicuts': [{'t_wall': e.get('t_wall'),
                       'layer': e.get('layer'),
                       'sources': e.get('sources'),
                       'sinks': e.get('sinks')}
                      for e in win if _ctype(e) == 'draft.update'],
        'notes': [{'t_wall': e.get('t_wall'), 'text': e.get('text')}
                  for e in win if _ctype(e) == 'evidence.bookmark'],
        'api_calls': [{'t_wall': e.get('t_wall'), 'kind': e.get('kind'),
                       'type': e.get('type'), 'write': e.get('write')}
                      for e in win
                      if str(_ctype(e)).split('.')[0] in ('api', 'edit')],
        'viewpoint_before': _viewpoint_summary(
            (before[-1] or {}).get('viewpoint')) if before else None,
        'viewpoint_after': _viewpoint_summary(
            (after[0] or {}).get('viewpoint')) if after else None,
    }
    return ctx


def _health(pack):
    """会话健康：缺口/断档/状态变化/心跳（缺失不填，直接给 null）。

    心跳按 F26/RF14 保留，但它只用于“发现节流/断采”，**不得推导思考时长**
    （清单原文：长时间无操作不能被视为在思考）。
    """
    collector = pack.get('collector') or {}
    events = pack.get('events') or []
    status = [e for e in events if _ctype(e) == 'collector.status']
    hb = [e.get('t_wall') for e in events
          if _ctype(e) == 'collector.heartbeat'
          and isinstance(e.get('t_wall'), (int, float))]
    seqs = [e.get('seq') for e in events if isinstance(e.get('seq'), int)]
    gaps = []
    prev = None
    for s in seqs:
        if prev is not None and s - prev > 1:
            gaps.append([prev + 1, s - 1])
        prev = s
    out = {
        'n_events': len(events),
        'seq_gaps': gaps,
        'n_heartbeats': len(hb),
        'heartbeat_median_ms': None,
        'status_changes': len(status),
        'gaps': collector.get('gaps') or [],
        'capabilities': collector.get('capabilities') or None,
        'collector_version': pack.get('collector_version')
            or collector.get('version'),
    }
    if len(hb) >= 2:
        deltas = sorted(b - a for a, b in zip(hb, hb[1:]))
        out['heartbeat_median_ms'] = deltas[len(deltas) // 2]
    return out


def join_session(pack, ops, pre_s=15.0, post_s=3.0):
    ops_by_id = {}
    for op in ops:
        if op.get('op_id') is not None:
            ops_by_id[str(op.get('op_id'))] = op
    anchors = extract_anchors(pack)
    clock = estimate_clock(anchors, ops_by_id)
    t0, t1 = session_span(pack)
    events = pack.get('events') or []
    sel_ids = collect_selection_ids(pack)
    session_id = pack.get('session_id') or 'session'
    offset = clock['offset_ms']

    anchor_by_op = {}
    for a in anchors['op_anchors']:
        if a.get('op_id') is not None:
            anchor_by_op.setdefault(str(a['op_id']), a)

    joined, unmatched = [], []
    for op in ops:
        op_id = op.get('op_id')
        st = parse_server_ts(op.get('timestamp'))
        roots = {str(x) for x in (op.get('before_root_ids') or [])} | \
                {str(x) for x in (op.get('after_root_ids') or [])}
        root_seen = bool(roots & sel_ids)
        anchor = (anchor_by_op.get(str(op_id))
                  or anchor_by_op.get(op_id)) if op_id is not None else None

        if anchor is not None and st is not None:
            client_ms = anchor['t_wall']
            m_off, method = st - client_ms, 'exact-anchor'
        elif st is not None and t0 is not None and t1 is not None:
            client_ms = st - offset
            base_ok = (t0 - 2000) <= client_ms <= (t1 + 2000)
            win_ok = ((client_ms - pre_s * 1000) <= t1
                      and (client_ms + post_s * 1000) >= t0)
            if clock['basis'] in ('op-anchor', 'server-date') and base_ok:
                m_off, method = offset, 'anchor-estimated'
            elif clock['basis'] == 'none' and win_ok:
                m_off, method = 0.0, 'window'
            else:
                unmatched.append({'op_id': op_id, 'reason': '时间落在会话跨度外'
                                  if not base_ok else '无锚点且窗口不重叠'})
                continue
        else:
            unmatched.append({'op_id': op_id, 'reason': 'op 时间戳缺失'})
            continue

        rec = {
            'op_id': op_id,
            'op_type': op.get('type'),
            'server_time_utc': op.get('timestamp'),
            'user': op.get('user'),
            'before_root_ids': op.get('before_root_ids'),
            'after_root_ids': op.get('after_root_ids'),
            'session_id': session_id,
            'match_method': method,
            'client_time_ms': client_ms,
            'clock_offset_ms': round(m_off, 1),
            'clock_uncertainty_ms': clock['uncertainty_ms'],
            'evidence': {
                'root_id_seen_in_session': root_seen,
                'in_session_span': bool(t0 is not None and t1 is not None
                                        and t0 <= client_ms <= t1),
            },
            'anchor': ({'via': anchor.get('via'), 'kind': anchor.get('kind'),
                        't_wall': anchor.get('t_wall'),
                        'request_id': anchor.get('request_id')}
                       if anchor else None),
            'context': build_context(events, client_ms, pre_s, post_s),
        }
        joined.append(rec)

    server_ids = set(ops_by_id)
    anchors_missing = [a for a in anchors['op_anchors']
                       if a.get('op_id') is not None
                       and str(a.get('op_id')) not in server_ids]

    matched_counts = {}
    for r in joined:
        matched_counts[r['match_method']] = \
            matched_counts.get(r['match_method'], 0) + 1

    summary = {
        'session_id': session_id,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'session_span': {'start_wall_ms': t0, 'end_wall_ms': t1},
        'clock': {'offset_ms': clock['offset_ms'],
                  'uncertainty_ms': clock['uncertainty_ms'],
                  'basis': clock['basis'],
                  'n_samples': len(clock['samples'])},
        'n_ops_total': len(ops),
        'matched': matched_counts,
        'n_matched': len(joined),
        'unmatched_ops': unmatched,
        'anchors_not_on_server': anchors_missing,
        'rejected_writes': anchors['rejected_writes'],
        'api_errors': anchors['api_errors'],
        'client_request_ids': [a.get('request_id') for a in anchors['op_anchors']
                              if a.get('request_id')],
        'health': _health(pack),
        'notes': [
            'match_method: exact-anchor(最强，op_id 一致) > '
            'anchor-estimated(按估计时钟换算) > window(仅时间窗)',
            'anchors_not_on_server 可能被拒/超窗/缓存等多种原因，'
            '仅作客户端侧证据，不冒充服务端事实',
            'selection_sample ids 每层仅前 8 个，root 证据为下界',
        ],
    }
    return {'joined': joined, 'summary': summary}


def _unique_path(path):
    if not path.exists():
        return path
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return path.with_name(f'{path.stem}_{ts}{path.suffix}')


def main():
    ap = argparse.ArgumentParser(description='会话包 ↔ op log 关联器')
    ap.add_argument('session', help='会话包 JSON 路径')
    ap.add_argument('--ops', default=str(DEFAULT_OPS),
                    help='ops.jsonl 路径（默认 results/cave_watch/ops.jsonl）')
    ap.add_argument('--out', default=str(OUT_DIR), help='输出目录')
    ap.add_argument('--pre', type=float, default=15.0,
                    help='提交前上下文窗口（秒）')
    ap.add_argument('--post', type=float, default=3.0,
                    help='提交后上下文窗口（秒）')
    args = ap.parse_args()

    pack = load_session(args.session)
    ops = load_ops(args.ops)
    result = join_session(pack, ops, pre_s=args.pre, post_s=args.post)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = result['summary']['session_id']
    jl = _unique_path(out_dir / f'{sid}.join.jsonl')
    sm = _unique_path(out_dir / f'{sid}.join_summary.json')
    with jl.open('w', encoding='utf-8') as f:
        for rec in result['joined']:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + '\n')
    sm.write_text(json.dumps(result['summary'], ensure_ascii=False, indent=1,
                             default=str), encoding='utf-8')

    s = result['summary']
    print(f'会话 {sid}：op 总数 {s["n_ops_total"]}，matched {s["n_matched"]}'
          f' {s["matched"] or ""}')
    print(f'时钟：basis={s["clock"]["basis"]} '
          f'offset={s["clock"]["offset_ms"]}ms '
          f'±{s["clock"]["uncertainty_ms"]}ms '
          f'({s["clock"]["n_samples"]} 样本)')
    if s['unmatched_ops']:
        print(f'未匹配 op {len(s["unmatched_ops"])} 条')
    if s['anchors_not_on_server']:
        print(f'客户端锚点但服务端无此 op：'
              f'{[a.get("op_id") for a in s["anchors_not_on_server"]]}')
    if s['rejected_writes']:
        print(f'客户端观察到的失败写请求：{len(s["rejected_writes"])} 条')
    print('join:', jl)
    print('summary:', _relpath(sm))


if __name__ == '__main__':
    main()
