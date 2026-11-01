"""离线派生：归一化时间线 + observation episode 切分 + no-op 分级。

对应需求清单 `12 在线与离线清洗`：
  PP06 归一化与单调时间线（离线）
  PP07 Observation episode（离线，按空间/根/工具上下文切分，允许重叠）
  PP10 No-op 分级（离线，no_edit 事实与 judgement_disposition 分开）

为什么必须离线：这三件事都要「跨时间推理」，规则还会变（D03 任务边界、
D14 空闲阈值都未决）。放进在线采集会污染原始事件，而且规则一改旧数据就废。
所以派生结果单独落 `derived/`，可重算、不覆盖原始，每条带 `policy_version`。

诚实边界：
- `no_edit` 是**事实**（这段没有提交编辑）；`disposition` 默认 `unknown`，
  只有出现显式书签且文本里明确声明时才改——**绝不从“没改”推出“检查过没问题”**（Q08/PP10）。
- `idle_segments` 是“没有输入的时间段”，**不是“在思考的时间”**（清单原话：
  长时间无操作不能被视为在思考）。

用法：
    python3 scripts/cave_episodes.py results/pipeline_demo/session_pack/<sid>
    python3 scripts/cave_episodes.py <会话.json> --out /tmp/episodes
"""

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from cave_session_pack import ctype, load_session
except Exception:                                   # 作为包导入时
    from scripts.cave_session_pack import ctype, load_session

# 默认策略（D03/D14 未决，先给可解释的默认值；改规则必须升 policy_version）
POLICY = {
    'version': 'v1.spatial-temporal',
    'gap_ms': 30000,        # 相邻事件间隔超过此值 → 切一个 episode
    'jump_nm': 50000,       # 相邻视角中心距离超过此值 → 切一个 episode（50 µm）
    'idle_ms': 2000,        # 输入静默超过此值 → 记一段 idle
    'overlap': False,       # 清单说“允许重叠”；v1 先不重叠，留字段位
    'min_events': 3,        # 少于这么多个事件的残片并入相邻段，不单列
    # 只允许把**空间抖动**切出来的小残片并回去。时间间隔是强信号
    # （人离开又回来 / 换了目标），把它吞掉会让 duration 和 idle 都失真，
    # 且事后无法解释边界。
    'mergeable_reasons': ('spatial_jump',),
}

INPUT_TYPES = ('input.pointer', 'input.wheel', 'input.key',
               'input.gesture', 'viewport.gesture')
EDIT_TYPES = ('edit.request',)


def _is_edit(e):
    """写操作判定。

    v0.7 起写请求由 recorder 直接命名成 edit.request；
    旧包（或别处直接发 api.request）靠 `write` 标志识别，
    否则“提交过没有”会被判反。
    """
    t = ctype(e)
    if t in EDIT_TYPES:
        return True
    return t == 'api.request' and bool(e.get('write'))


# ------------------------------------------------------------------ PP06
def normalize(events):
    """PP06：归一化与单调时间线。

    只做机械校验，不改写事件：seq 排序、缺口、单调钟与墙钟的漂移。
    """
    seqs = [e.get('seq') for e in events if isinstance(e.get('seq'), int)]
    by_seq = sorted((e for e in events if isinstance(e.get('seq'), int)),
                    key=lambda e: e['seq'])
    gaps = []
    prev = None
    for s in seqs:
        if prev is not None and s - prev > 1:
            gaps.append([prev + 1, s - 1])
        prev = s

    # 墙钟与单调钟的差：突变 = 系统改时间/休眠唤醒（R2 排序会错）
    drift = []
    for e in by_seq:
        tw, tm = e.get('t_wall'), e.get('t_mono')
        if isinstance(tw, (int, float)) and isinstance(tm, (int, float)):
            drift.append(tw - tm)
    drift_ms = None
    if len(drift) >= 2:
        med = statistics.median(drift)
        drift_ms = {
            'median': round(med, 3),
            'range': round(max(drift) - min(drift), 3),
            # 超过 1s 的跳变基本不是采样噪声
            'jump_detected': (max(drift) - min(drift)) > 1000,
        }

    # 用单调钟排序 vs 用 seq 排序是否一致（不一致 = 事件被乱序写入）
    order_ok = True
    if len(by_seq) >= 3:
        ts = [e.get('t_mono') for e in by_seq
              if isinstance(e.get('t_mono'), (int, float))]
        order_ok = all(b >= a for a, b in zip(ts, ts[1:]))

    return {
        'n_events': len(events),
        'seq_first': seqs[0] if seqs else None,
        'seq_last': seqs[-1] if seqs else None,
        'seq_gaps': gaps,
        'seq_ok': not gaps,
        'monotonic_order_ok': order_ok,
        'clock_drift_ms': drift_ms,
        'policy_version': POLICY['version'],
    }


# ------------------------------------------------------------------ 工具
def _pos_nm(e):
    vp = e.get('viewpoint') or {}
    p = vp.get('position_nm')
    if (isinstance(p, list) and len(p) == 3
            and all(isinstance(x, (int, float)) for x in p)):
        return [float(x) for x in p]
    return None


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _state_positions(events):
    return [(_pos_nm(e), e.get('t_wall')) for e in events
            if ctype(e) == 'viewer.state-change' and _pos_nm(e)]


def _split_boundaries(events):
    """→ [(idx, reason, detail)]：在 events 的哪个位置切。"""
    cuts = []
    prev_t = None
    prev_pos = None
    for i, e in enumerate(events):
        t = e.get('t_wall')
        if isinstance(t, (int, float)) and isinstance(prev_t, (int, float)):
            if t - prev_t > POLICY['gap_ms']:
                cuts.append((i, 'time_gap', {'gap_ms': t - prev_t}))
        pos = _pos_nm(e)
        if pos and prev_pos:
            d = _dist(pos, prev_pos)
            if d > POLICY['jump_nm']:
                cuts.append((i, 'spatial_jump', {'jump_nm': round(d, 1)}))
        if pos:
            prev_pos = pos
        if isinstance(t, (int, float)):
            prev_t = t
    return cuts


def _counts(evs):
    out = {}
    for e in evs:
        t = ctype(e)
        out[t] = out.get(t, 0) + 1
    return out


def _all_idle_segments(events, t_end=None):
    """输入静默段（全段算一次，再按归属分给 episode）。

    只说“没输入”，不说“在想”。
    **归属规则**：静默归给“最后那一下输入所在的 episode”，而不是两边都算。
    否则跨边界的静默要么被丢（episode 内看不到），要么被双计。
    """
    ts = [e.get('t_wall') for e in events
          if ctype(e) in INPUT_TYPES
          and isinstance(e.get('t_wall'), (int, float))]
    ts.sort()
    segs = []
    for a, b in zip(ts, ts[1:]):
        if b - a > POLICY['idle_ms']:
            segs.append({'start_ms': a, 'end_ms': b,
                         'duration_ms': b - a})
    # 最后一次输入到会话结束也是静默；只算到 session.end，不把“关窗口之后”算进来。
    if (ts and isinstance(t_end, (int, float)) and t_end > ts[-1]
            and t_end - ts[-1] > POLICY['idle_ms']):
        segs.append({'start_ms': ts[-1], 'end_ms': t_end,
                     'duration_ms': t_end - ts[-1]})
    return segs


def _idle_for(segs, t_start, t_end):
    """取归属于 [t_start, t_end] 的静默段（按“开始时刻”归属）。"""
    return [s for s in segs if t_start <= s['start_ms'] <= t_end]


def _no_edit_disposition(evs):
    """PP10：事实与判断分开。默认 unknown，不从“没改”推“没问题”。"""
    has_edit = any(_is_edit(e) for e in evs)
    notes = [e for e in evs if ctype(e) == 'evidence.bookmark']
    disposition = 'unknown'
    reason = None
    if has_edit:
        disposition = 'edited'
        reason = 'episode 内有 edit.request'
    elif notes:
        # 只有当书签文本**明确**声明时才算判断；否则仍是 unknown
        for n in notes:
            txt = str(n.get('text') or '')
            if any(k in txt for k in ('无误', 'no error', '检查完', 'no_error')):
                disposition = 'inspected_no_error'
                reason = '书签文本显式声明无误（唯一合法来源）'
                break
    return {
        'no_edit': not has_edit,
        'disposition': disposition,
        'disposition_reason': reason,
    }


def _geometry(evs):
    pts = [p for p, _ in _state_positions(evs)]
    if not pts:
        return None
    n = len(pts)
    com = [round(sum(p[i] for p in pts) / n, 1) for i in range(3)]
    lo = [round(min(p[i] for p in pts), 1) for i in range(3)]
    hi = [round(max(p[i] for p in pts), 1) for i in range(3)]
    return {
        'n_points': n,
        'start_nm': pts[0], 'end_nm': pts[-1],
        'center_of_mass_nm': com,
        'bbox_nm': {'lower': lo, 'upper': hi,
                    'size': [round(h - l, 1) for l, h in zip(lo, hi)]},
        # 净位移与轨迹长度分开：跳一下 vs 慢慢挪过去 是两种行为
        'net_displacement_nm': round(_dist(pts[0], pts[-1]), 1),
        'path_length_nm': round(sum(_dist(a, b)
                                    for a, b in zip(pts, pts[1:])), 1),
    }


# ------------------------------------------------------------------ hover
# F09：指针悬停过的段。`selection.change` 只记“选中了什么”，
# 但人常常先把指针停在某个段上看一眼、再决定选不选——中间那步本来没人记。
SEG_ID_RE = re.compile(r'^!?\d{4,}$')


def _hover_summary(evs, t_end=None):
    """把 `viewer.focus` 的 `picked` 合并成悬停区间。

    口径（跟着产物走，不能只写在文档里）：

    - **事件是“变化才发”**，不是周期采样：指针停住不动就不会有新样本。
      所以区间结束时间取“下一个样本的时刻”；最后一次悬停只能到会话末尾，
      标 `open_spans`。
    - **hover ≠ 注意**：指针停着人也可能在走神。这里只回答“指针压在哪个段上
      多久”，**不推断**“看了/判断了/认为它有问题”。
    - **短划过会丢**：只在采样点上有值才算，次数与时长都是下界。
    - `picked` 不像段 id（灰度值/属性名）的样本单独计数，不混进 `segments`。
    """
    samples = []
    n_miss = 0
    for e in evs:
        if ctype(e) != 'viewer.focus':
            continue
        t = e.get('t_wall')
        if not isinstance(t, (int, float)):
            continue
        if e.get('picked_miss'):
            n_miss += 1
        samples.append((t, e.get('picked'), e.get('picked_layer')))
    if not samples:
        return {'n_samples': 0, 'status': 'no_samples',
                'status_why': '这一段没有 viewer.focus 事件（可能鼠标未进入画布）'}
    samples.sort(key=lambda x: x[0])
    # `t_end` 只在“确实晚于最后一个样本”时才算闭合；
    # 否则最后一段悬停会被算成已结束（实际只是没更多样本）。
    if t_end is not None and t_end <= samples[-1][0]:
        t_end = None

    n_value = sum(1 for _, p, _ in samples if p)
    # 关键区分：**“没人悬停”与“取不到命中值”是两件事**。
    # 实测本地 demo 与公开 minnie65 的 97 条 viewer.focus 里 picked 全为空
    # （mouseState.pickedValue=null），所以必须把这种情况单独标出来，
    # 否则会被读成“人从来不悬停”。
    status = 'ok' if n_value else 'no_picked'
    status_why = None
    if not n_value:
        if n_miss:
            status_why = (
                'viewer.focus 有 %d 条，其中 %d 条明确“在画布里但没压到段”'
                '（picked_miss）——pick 在跑，只是没有命中；'
                '把指针放到分割段上就会有值' % (len(samples), n_miss))
        else:
            status_why = (
                'viewer.focus 有 %d 条但 picked 全空且无 picked_miss：'
                '这个环境根本没填充 mouseState.pickedValue'
                '（没有可 pick 的分割层 / 数据没加载出来 / 构建未开 pick），'
                '**不是没人悬停**' % len(samples))

    n_odd = 0
    spans, cur = [], None
    for i, (t, picked, layer) in enumerate(samples):
        end = samples[i + 1][0] if i + 1 < len(samples) else t_end
        keep = bool(picked) and bool(SEG_ID_RE.match(str(picked)))
        if picked and not keep:
            n_odd += 1
        if not keep:
            if cur:
                spans.append(cur)
                cur = None
            continue
        if cur and cur['id'] == picked and cur['layer'] == layer:
            cur['last_t'] = t
            cur['n'] += 1
            if end is not None:
                cur['end_t'] = end
                cur['open'] = False
            continue
        if cur:
            spans.append(cur)
        cur = {'id': picked, 'layer': layer, 'first_t': t, 'last_t': t,
               'n': 1, 'end_t': end, 'open': end is None}
    if cur:
        spans.append(cur)

    agg = {}
    for s in spans:
        a = agg.setdefault(s['id'], {
            'id': s['id'], 'layers': set(), 'n_spans': 0,
            'total_ms': 0.0, 'max_span_ms': 0.0,
            'first_t_wall': s['first_t'], 'last_t_wall': s['last_t'],
        })
        if s.get('layer'):
            a['layers'].add(s['layer'])
        a['n_spans'] += 1
        if s.get('end_t') is not None:
            d = max(0.0, float(s['end_t']) - float(s['first_t']))
            a['total_ms'] += d
            a['max_span_ms'] = max(a['max_span_ms'], d)
        a['first_t_wall'] = min(a['first_t_wall'], s['first_t'])
        a['last_t_wall'] = max(a['last_t_wall'], s['last_t'])

    segs = sorted(agg.values(), key=lambda x: (-x['total_ms'], x['id']))
    for s in segs:
        s['layers'] = sorted(s['layers'])
        s['total_ms'] = round(s['total_ms'], 1)
        s['max_span_ms'] = round(s['max_span_ms'], 1)

    return {
        'status': status,
        'status_why': status_why,
        'n_samples': len(samples),
        'n_with_picked': n_value,
        'n_picked_miss': n_miss,
        'n_spans': len(spans),
        'n_not_segment_id': n_odd,
        'n_segments': len(segs),
        'open_spans': sum(1 for s in spans if s.get('open')),
        'segments': segs,
        'caveats': [
            'hover=指针压在哪个段；不推断看了/判断了',
            'viewer.focus 只在变化时发：停住不动没有新样本，时长=到下一次变化',
            '最后一次悬停到会话/片段结束，记在 open_spans',
            '短于采样点的划过会丢：次数与时长都是下界',
        ],
        'method': 'hover=viewer.focus.picked;span_end=next_sample',
    }


def _gesture_summary(evs):
    g = [e for e in evs if ctype(e) == 'viewport.gesture']
    if not g:
        return {'n': 0}
    by_kind = {}
    for e in g:
        by_kind[e.get('kind') or 'unknown'] = \
            by_kind.get(e.get('kind') or 'unknown', 0) + 1
    durs = [e.get('duration_ms') for e in g
            if isinstance(e.get('duration_ms'), (int, float))]
    return {
        'n': len(g),
        'by_kind': by_kind,
        'total_duration_ms': round(sum(durs), 1) if durs else 0,
        'view_changed_n': sum(1 for e in g if e.get('view_changed')),
        'n_raw_inputs': sum(int(e.get('n_raw') or 0) for e in g),
    }


# ------------------------------------------------------------------ PP07
def _sampling(pack):
    """取输入的采样配置（目录包在 manifest，裸 JSON 在顶层）。

    缺失就返回 {}，**不拿默认值填**——否则等于谎报粒度。
    """
    cfg = pack.get('config')
    if not cfg:
        cfg = (pack.get('manifest') or {}).get('config')
    return dict(cfg or {})


def _selected_segments(evs):
    """从完整 state 里抽出选中集变化（不是 selection.change 的悬停）。"""
    out, prev = [], None
    for e in evs:
        if ctype(e) not in ('viewer.state-change', 'viewer.checkpoint'):
            continue
        st = e.get('state') or {}
        segs = {}
        for lay in st.get('layers') or []:
            if 'segmentation' not in str(lay.get('type') or '').lower():
                continue
            name = lay.get('name')
            ids = [re.sub(r'^!', '', str(x)) for x in (lay.get('segments') or [])]
            if name and ids:
                segs[str(name)] = ids
        sig = json.dumps(segs, sort_keys=True) if segs else '{}'
        if sig == prev:
            continue
        prev = sig
        out.append({'t_wall': e.get('t_wall'), 'state_id': e.get('state_id'),
                    'segments': segs})
    return out


def derive_episodes(pack, policy=None):
    """PP07：把整段会话切成 observation episode。

    切分依据（v1，全部机械可解释）：时间间隔、视角空间跳变。
    不用“语义猜测”（那属于 PP08 弱标签）。
    """
    if policy:
        POLICY.update(policy)
    events = [e for e in (pack.get('events') or [])
              if isinstance(e.get('seq'), int)]
    events.sort(key=lambda e: e['seq'])
    note_by_id = {n.get('note_id'): n for n in (pack.get('notes') or [])
                  if isinstance(n, dict) and n.get('note_id')}
    if not events:
        return {'policy_version': POLICY['version'], 'episodes': [],
                'sampling': _sampling(pack), 'normalized': normalize([])}

    cuts = _split_boundaries(events)
    cut_idx = sorted({i for i, _, _ in cuts})
    bounds = [0] + cut_idx + [len(events)]
    raw = []
    for a, b in zip(bounds, bounds[1:]):
        if b <= a:
            continue
        raw.append({
            'events': events[a:b],
            'trigger': next(({'reason': r, 'detail': d}
                             for i, r, d in cuts if i == a), None),
        })

    # 太碎、且紧邻的残片并入上一段（min_events）。
    # 被吞掉的切分理由不能丢——否则事后无法解释“这段边界到底因为什么”。
    # 只并“空间抖动”切出来的残片：时间间隔切出来的段哪怕只有 2 个事件，
    # 也是“人离开又回来”的真实断点，吞掉它等于篡改 duration。
    mergeable = tuple(POLICY['mergeable_reasons'])
    merged = []
    for seg in raw:
        reason = (seg['trigger'] or {}).get('reason')
        small = len(seg['events']) < POLICY['min_events']
        if merged and small and reason in mergeable:
            merged[-1]['events'].extend(seg['events'])
            if seg['trigger']:
                merged[-1]['extra_triggers'].append(seg['trigger'])
        else:
            merged.append({'events': seg['events'],
                           'trigger': seg['trigger'],
                           'extra_triggers': []})

    out = []
    t_end = next((e.get('t_wall') for e in reversed(events)
                  if ctype(e) == 'session.end'), None)
    idle_all = _all_idle_segments(events, t_end)
    for i, seg in enumerate(merged, 1):
        evs = seg['events']
        t0 = evs[0].get('t_wall') or 0
        t1 = evs[-1].get('t_wall') or t0
        out.append({
            'episode_id': f'ep_{i:04d}',
            't_start_ms': t0, 't_end_ms': t1,
            'duration_ms': t1 - t0,
            'trigger': seg['trigger'],
            'extra_triggers': seg.get('extra_triggers') or [],
            'n_events': len(evs),
            'event_counts': _counts(evs),
            'seq_range': [evs[0].get('seq'), evs[-1].get('seq')],
            'view': _geometry(evs),
            'gestures': _gesture_summary(evs),
            'hover': _hover_summary(evs, t1),
            'idle_segments': _idle_for(idle_all, t0, t1),
            # 这两个不是同一种东西：
            #   hover_selections = 鼠标悬停到的段（selection.change）
            #   selected_segments = 真正的选中集（完整 state.layers[].segments）
            'hover_selections': [e.get('selection') for e in evs
                                 if ctype(e) == 'selection.change'],
            # 兼容旧读者；新代码请读 hover_selections
            'selections': [e.get('selection') for e in evs
                           if ctype(e) == 'selection.change'],
            'selected_segments': _selected_segments(evs),
            'notes': [
                dict({'t_wall': e.get('t_wall'), 'text': e.get('text')},
                     **{k: note_by_id.get(e.get('note_id'), {}).get(k)
                        for k in ('note_id', 'state_id', 'viewpoint', 'shot_id')
                        if note_by_id.get(e.get('note_id'), {}).get(k) is not None})
                for e in evs if ctype(e) == 'evidence.bookmark'
            ],
            **_no_edit_disposition(evs),
            'policy_version': POLICY['version'],
            # 允许重叠：v1 false；下游不得据此假定互斥
            'overlap': POLICY['overlap'],
            'event_refs': [evs[0].get('seq'), evs[-1].get('seq')],
        })

    return {
        'policy_version': POLICY['version'],
        'policy': dict(POLICY),
        'source': {'session_id': pack.get('session_id'),
                   'n_events': len(pack.get('events') or [])},
        # D10：把输入的采样粒度写进派生结果。
        # 不写的话，下游会把 50ms 采样过的轨迹当成全量轨迹做速度/加速度分析。
        'sampling': _sampling(pack),
        'normalized': normalize(events),
        'n_episodes': len(out),
        'n_events': len(events),
        'hover': _hover_summary(
            events,
            next((e.get('t_wall') for e in reversed(events)
                  if ctype(e) == 'session.end'), None)),
        'episodes': out,
    }


def write_derived(result, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ep_path = out_dir / 'episodes.jsonl'
    with open(ep_path, 'w', encoding='utf-8') as f:
        for e in result['episodes']:
            f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + '\n')
    norm = dict(result['normalized'])
    norm['policy_version'] = result['policy_version']
    norm['sampling'] = result.get('sampling') or {}
    (out_dir / 'normalized.json').write_text(
        json.dumps(norm, ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'hover.json').write_text(
        json.dumps({'policy_version': result['policy_version'],
                    'hover': result.get('hover') or {}},
                   ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'episode_policy.json').write_text(
        json.dumps({'policy_version': result['policy_version'],
                    'policy': result['policy']},
                   ensure_ascii=False, indent=1), encoding='utf-8')
    (out_dir / 'manifest.json').write_text(
        json.dumps({
            'policy_version': result['policy_version'],
            'source': result.get('source') or {},
            'generated_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'files': ['normalized.json', 'episodes.jsonl', 'hover.json',
                      'episode_policy.json'],
        }, ensure_ascii=False, indent=1), encoding='utf-8')
    return ep_path


def main(argv=None):
    ap = argparse.ArgumentParser(description='离线派生：归一化 + episode + no-op')
    ap.add_argument('session', help='会话目录包或 recorder JSON')
    ap.add_argument('--out', default=None,
                    help='派生输出目录（默认写进会话包的 derived/）')
    ap.add_argument('--gap-ms', type=int, default=None)
    ap.add_argument('--jump-nm', type=float, default=None)
    ap.add_argument('--idle-ms', type=int, default=None)
    args = ap.parse_args(argv)

    policy = {}
    if args.gap_ms is not None:
        policy['gap_ms'] = args.gap_ms
    if args.jump_nm is not None:
        policy['jump_nm'] = args.jump_nm
    if args.idle_ms is not None:
        policy['idle_ms'] = args.idle_ms
    if policy:
        policy['version'] = 'v1.spatial-temporal+custom'

    pack = load_session(args.session)
    res = derive_episodes(pack, policy or None)

    out = Path(args.out).expanduser() if args.out else (
        Path(pack.get('pack_dir') or '.') / 'derived')
    ep = write_derived(res, out)

    n = res['normalized']
    smp = res.get('sampling') or {}
    print(f"采样: move {smp.get('move_sample_ms', '未记录')}ms"
          f"（{smp.get('move_mode', '未知')}）/ 端点 {smp.get('endpoints', '?')}")
    print(f"归一化: {n['n_events']} 事件 / seq {n['seq_first']}–{n['seq_last']}"
          f" / 断档 {n['seq_gaps'] or '无'} / 单调序 "
          f"{'OK' if n['monotonic_order_ok'] else '异常'}"
          f" / 时钟漂移 {n['clock_drift_ms'] or '无数据'}")
    print(f"episode: {res['n_episodes']} 个（policy {res['policy_version']}）")
    for e in res['episodes']:
        idle = sum(s['duration_ms'] for s in e['idle_segments'])
        print(f"  {e['episode_id']} {e['duration_ms']/1000:6.1f}s "
              f"ev={e['n_events']:<3} gestures={e['gestures'].get('n', 0)} "
              f"idle={idle/1000:4.1f}s "
              f"no_edit={e['no_edit']} disp={e['disposition']}"
              f" 触发={((e['trigger'] or {}).get('reason')) or '-'}")
    print('派生:', ep)
    return 0


if __name__ == '__main__':
    sys.exit(main())
