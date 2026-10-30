"""回放视图指纹：把一帧视图归一成可比字段，供回放器与保真度自检共用。

为什么要有这个模块
------------------
之前"恢复成功"的判据只有 `position` 一个数：位置对上了就算 applied。于是
"图层被隐藏、朝向没复位、尺度沿用上一个点"这类错误**不会失败**，只会静默地
画出另一张图——而回放要的恰恰是"一模一样"。

这里把一帧视图归一成一组字段，并给出逐字段判定。两条不变量：

1. **缺省即默认**：recorder 与 Neuroglancer 的 state 里没有这个键 = 该字段是
   默认值。回放端必须把它**复位成默认**，不能沿用上一个回放点的值
   （对应 `cave_local_demo_server._apply_state` 的复位逻辑）。
2. **只比有记录或应为默认的字段**，不推断没记录的字段；字段缺失时如实记
   `absent`，不当成"一致"。

position 用 state 的坐标单位（= dims.scales）比较，容差 1 个体素。
"""

from __future__ import annotations

# 字段名与顺序（报告/对比页按这个顺序展示，便于人读）
FIELDS = (
    'position',
    'crossSectionScale',
    'projectionScale',
    'crossSectionOrientation',
    'projectionOrientation',
    'layout',
    'showSlices',
    'partialViewport',
    'crossSectionDepth',
    'layers',
    'dimensions',
)

# 语义默认值：state 里缺这个键就表示"等于这里"。只用于判据与文档，
# 真正复位由回放端（cave_local_demo_server）执行。
DEFAULTS = {
    'position': None,
    'crossSectionScale': None,
    'projectionScale': None,
    'crossSectionOrientation': [1.0, 0.0, 0.0, 0.0],
    'projectionOrientation': [1.0, 0.0, 0.0, 0.0],
    # 本地 NG viewer 创建时的默认布局；没有 layers = 没有图层
    'layout': '4panel',
    'showSlices': True,
    'partialViewport': [0.0, 0.0, 1.0, 1.0],
    'crossSectionDepth': None,
    'layers': [],
    'dimensions': None,
}

POS_TOL = 1.0          # 1 个体素（state 的坐标单位）
SCALE_RTOL = 1e-3      # float32 往返
ORIENT_TOL = 1e-3      # 单位四元数分量最大差
OPACITY_TOL = 1e-3


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _source(v):
    """source 可能是 str 或 [str, ...]；只取第一个，便于比较数据身份。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)) and v:
        first = v[0]
        return first if isinstance(first, str) else None
    return None


def _dims(v):
    """dimensions → {轴名: 尺度}。recorder 是 {name: [scale, unit]}；NG 亦然。"""
    if not isinstance(v, dict):
        return None
    out = {}
    for name, e in v.items():
        if isinstance(e, (list, tuple)) and e:
            s = _num(e[0])
        else:
            s = _num(e)
        if s is not None:
            out[str(name)] = s
    return out or None


def _segments(v):
    if not isinstance(v, (list, tuple)):
        return []
    out = []
    for x in v:
        try:
            # state 里可能带 NG 的 `!`（visible 标记）或大整数 ID 字符串；
            # 统一成十进制整数，避免 8.6e17 这种 uint64 在 JS Number 里丢精度。
            out.append(int(str(x).lstrip('!')))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


STYLE_KEYS = ('baseSegmentColoring', 'baseSegmentHighlighting',
              'selectedAlpha', 'notSelectedAlpha', 'objectAlpha',
              'colorSeed', 'segmentDefaultColor')


def _style(l):
    """图层样式：state 顶层 camelCase，viewpoint 里可能在 style 子对象。"""
    out = {}
    raw = l.get('style')
    if isinstance(raw, dict):
        out.update(raw)
    for k in STYLE_KEYS:
        if l.get(k) is not None:
            out[k] = l.get(k)
    return out


def _layer(name, src, typ, visible, opacity, segments, style=None):
    return {
        'name': name,
        'source': _source(src),
        'type': (str(typ).lower() if typ else None),
        'visible': True if visible is None else bool(visible),
        'opacity': 1.0 if opacity is None else float(opacity),
        'segments': _segments(segments),
        'style': style if isinstance(style, dict) else {},
    }


def _layers_from_state(v):
    out = []
    for l in (v or []):
        if not isinstance(l, dict):
            continue
        out.append(_layer(l.get('name'), l.get('source'), l.get('type'),
                          l.get('visible'), l.get('opacity'),
                          l.get('segments'), _style(l)))
    return sorted(out, key=lambda x: str(x['name']))


def _layers_from_viewpoint(v):
    """viewpoint.layers 里有 visible/opacity（没有 segments 列表）。"""
    out = []
    for l in (v or []):
        if not isinstance(l, dict):
            continue
        out.append(_layer(l.get('name'), l.get('source'), l.get('type'),
                          l.get('visible'), l.get('opacity'), None, _style(l)))
    return sorted(out, key=lambda x: str(x['name']))


def _orientation(*candidates):
    for c in candidates:
        if isinstance(c, (list, tuple)) and len(c) == 4:
            vals = [_num(x) for x in c]
            if all(v is not None for v in vals):
                return vals
    return None


def fingerprint(state=None, viewpoint=None):
    """从 recorder 的 state（+事件里的 viewpoint）提出可比字段。

    返回 dict，键 = `FIELDS`。缺席的字段返回 None（= 默认值），不猜值。
    """
    st = state if isinstance(state, dict) else {}
    vp = viewpoint if isinstance(viewpoint, dict) else {}

    pos = st.get('position')
    if not (isinstance(pos, (list, tuple)) and len(pos) >= 3):
        pos = vp.get('position')
    if isinstance(pos, (list, tuple)) and len(pos) >= 3:
        pos = [_num(x) for x in pos][:3]
        if any(x is None for x in pos):
            pos = None
    else:
        pos = None

    # 图层：state 里有 segments 但没有 visible；viewpoint 里有 visible 但没有
    # segments。两处都有时以 state 为骨架、viewpoint 补 visible。
    layers = _layers_from_state(st.get('layers'))
    if not layers:
        layers = _layers_from_viewpoint(vp.get('layers'))
    elif vp.get('layers'):
        vis = {str(x['name']): x for x in _layers_from_viewpoint(vp['layers'])}
        for l in layers:
            v = vis.get(str(l['name']))
            if v is not None:
                l['visible'] = v['visible']

    return {
        'position': pos,
        'crossSectionScale': _num(st.get('crossSectionScale')),
        'projectionScale': _num(st.get('projectionScale')),
        'crossSectionOrientation': _orientation(
            st.get('crossSectionOrientation'),
            vp.get('crossSectionOrientation')),
        'projectionOrientation': _orientation(
            st.get('projectionOrientation'),
            vp.get('projectionOrientation')),
        'layout': st.get('layout') or vp.get('layout'),
        'showSlices': (st.get('showSlices')
                       if isinstance(st.get('showSlices'), bool) else None),
        'partialViewport': (list(st.get('partialViewport'))
                            if isinstance(st.get('partialViewport'),
                                          (list, tuple))
                            and len(st['partialViewport']) == 4 else None),
        'crossSectionDepth': _num(st.get('crossSectionDepth')),
        'layers': layers or None,
        'dimensions': _dims(st.get('dimensions')) or _dims(vp.get('dimensions')),
    }


def _effective(field, v):
    """把 None 解读成该字段的默认值（None = 记录里没这个键 = 默认）。

    只对“默认值有确定含义”的字段生效；尺度/位置的默认值不可知，仍保留 None，
    因为“回放端有值、记录端没有”就是泄漏，必须判不一致。
    """
    if v is None and field in ('layout', 'layers', 'showSlices',
                               'partialViewport'):
        return DEFAULTS[field]
    return v


def _close(a, b, tol):
    return a is not None and b is not None and abs(a - b) <= tol


def _rel_close(a, b, rtol):
    """相对误差比较。注意不能给 max() 加 1.0 的下限：
    dimensions 的尺度是 4e-09 这种量级，加了 1.0 会把 2 倍差异判成相等。"""
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= rtol * max(abs(a), abs(b)) + 1e-12


def _quat_equal(a, b, tol=ORIENT_TOL):
    """四元数按 ±q 等价比较（NG 可能给出相反符号）；None = 单位四元数。"""
    qa = [1.0, 0.0, 0.0, 0.0] if a is None else a
    qb = [1.0, 0.0, 0.0, 0.0] if b is None else b
    if len(qa) != 4 or len(qb) != 4:
        return False
    direct = max(abs(x - y) for x, y in zip(qa, qb))
    flipped = max(abs(x + y) for x, y in zip(qa, qb))
    return min(direct, flipped) <= tol


def _style_equal(a, b):
    if not a and not b:
        return True
    if not isinstance(a, dict) or not isinstance(b, dict):
        return False
    if set(a) != set(b):
        return False
    for k in a:
        x, y = a[k], b[k]
        if isinstance(x, (int, float)) and not isinstance(x, bool) \
                and isinstance(y, (int, float)) and not isinstance(y, bool):
            if not _close(float(x), float(y), 1e-3):
                return False
        elif x != y:
            return False
    return True


def compare(expected, got, pos_tol=POS_TOL, scale_rtol=SCALE_RTOL):
    """逐字段比对。

    返回 {'fields': {名: {...}}, 'ok': bool, 'failed': [...]}。
    规则：
      - 标量：容差内相等；两侧都缺席（None）视为一致；
      - 朝向：None = 单位四元数，按 ±q 等价；
      - 图层：名字集合必须相同；source/visible/opacity/segments 逐项比；
      - dimensions：轴集合相同且尺度相对误差在容差内。
    """
    exp = expected or {}
    now = got or {}
    out = {}

    out['position'] = {
        'ok': bool(exp.get('position') and now.get('position')
                   and len(exp['position']) == len(now['position'])
                   and all(_close(a, b, pos_tol)
                           for a, b in zip(exp['position'], now['position']))),
        'expected': exp.get('position'), 'got': now.get('position'),
    }
    for k in ('crossSectionScale', 'projectionScale'):
        e, g = exp.get(k), now.get(k)
        ok = (e is None and g is None) or _rel_close(e, g, scale_rtol)
        out[k] = {'ok': ok, 'expected': e, 'got': g}
    for k in ('crossSectionOrientation', 'projectionOrientation'):
        out[k] = {'ok': _quat_equal(exp.get(k), now.get(k)),
                  'expected': exp.get(k), 'got': now.get(k)}
    out['layout'] = {'ok': _effective('layout', exp.get('layout'))
                     == _effective('layout', now.get('layout')),
                     'expected': exp.get('layout'), 'got': now.get('layout')}
    es = _effective('showSlices', exp.get('showSlices'))
    gs = _effective('showSlices', now.get('showSlices'))
    out['showSlices'] = {'ok': bool(es) == bool(gs),
                         'expected': exp.get('showSlices'),
                         'got': now.get('showSlices')}
    ep = _effective('partialViewport', exp.get('partialViewport'))
    gp = _effective('partialViewport', now.get('partialViewport'))
    def _viewport_close(a, b):
        try:
            return (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple))
                    and len(a) == len(b)
                    and all(_close(float(x), float(y), 1e-3)
                            for x, y in zip(a, b)))
        except (TypeError, ValueError):
            return False

    out['partialViewport'] = {
        'ok': _viewport_close(ep, gp),
        'expected': exp.get('partialViewport'),
        'got': now.get('partialViewport'),
    }
    ed = exp.get('crossSectionDepth')
    gd = now.get('crossSectionDepth')
    out['crossSectionDepth'] = {
        'ok': (ed is None and gd is None) or _close(ed, gd, 1e-3),
        'expected': ed, 'got': gd,
    }

    el = _effective('layers', exp.get('layers'))
    nl = _effective('layers', now.get('layers'))
    by_e = {str(x.get('name')): x for x in el}
    by_n = {str(x.get('name')): x for x in nl}
    detail = {}
    for name in sorted(set(by_e) | set(by_n)):
        a, b = by_e.get(name), by_n.get(name)
        if a is None or b is None:
            detail[name] = 'missing'
            continue
        bad = []
        if a.get('source') != b.get('source'):
            bad.append('source')
        if bool(a.get('visible')) != bool(b.get('visible')):
            bad.append('visible')
        if not _close(a.get('opacity'), b.get('opacity'), OPACITY_TOL):
            bad.append('opacity')
        if _segments(a.get('segments')) != _segments(b.get('segments')):
            bad.append('segments')
        if not _style_equal(a.get('style'), b.get('style')):
            bad.append('style')
        if bad:
            detail[name] = '+'.join(bad)
    out['layers'] = {'ok': not detail, 'expected': el, 'got': nl,
                     'broken': detail}

    ed = exp.get('dimensions')
    nd = now.get('dimensions')
    if ed is None or nd is None:
        out['dimensions'] = {'ok': ed is None and nd is None,
                             'expected': ed, 'got': nd}
    else:
        same_axes = set(ed) == set(nd)
        close = same_axes and all(_rel_close(ed[k], nd[k], scale_rtol)
                                  for k in ed)
        out['dimensions'] = {'ok': bool(close), 'expected': ed, 'got': nd}

    failed = [k for k in FIELDS if not out.get(k, {}).get('ok')]
    return {'fields': out, 'ok': not failed, 'failed': failed}


def summarize(results):
    """把多行 compare 结果压成可写进报告的计数。"""
    n = len(results)
    per_field = {k: 0 for k in FIELDS}
    for r in results:
        for k in FIELDS:
            if (r.get('fields') or {}).get(k, {}).get('ok'):
                per_field[k] += 1
    return {
        'n_rows': n,
        'n_ok': sum(1 for r in results if r.get('ok')),
        'per_field_ok': per_field,
        'failed_fields': sorted({k for r in results
                                 for k in (r.get('failed') or [])}),
    }


def collect_points(events, shots=None, pair_tol_ms=3000):
    """可恢复点 + "图 → 点"配对（回放器与保真度自检共用）。

    点的来源按可信度排：
      'state'      事件流里 viewer.state-change 自带的完整 state（sha 去重）
      'shot_state' 快门自带完整 state（L1：拍时钉的，不是事后按时间猜）
      'checkpoint' 旧包兜底：30s 剖点

    配对优先级：shot_state（精确）> state_id（流里同 id）> nearest_time（≤3s）。
    返回 (points, specs, stats)。
    """
    from cave_session_pack import ctype

    points, by_sha = [], {}
    n_partial = 0
    for e in (events or []):
        if ctype(e) != 'viewer.state-change':
            continue
        st = e.get('state')
        if not isinstance(st, dict):
            continue
        if e.get('state_partial'):
            n_partial += 1
            continue
        sha = e.get('state_sha')
        if sha and sha in by_sha:
            continue
        sid = e.get('state_id')
        if sid is None:
            sid = (e.get('viewpoint') or {}).get('state_id')
        pt = {'state_id': sid, 't_wall': e.get('t_wall'), 'state': st,
              'viewpoint': e.get('viewpoint'), 'source': 'state',
              'state_truncated': e.get('state_truncated')}
        points.append(pt)
        if sha:
            by_sha[sha] = pt
    n_from_state = len(points)

    n_from_shot = 0
    for s in (shots or []):
        st = s.get('state')
        if not isinstance(st, dict):
            continue
        sha = s.get('state_sha')
        if sha and sha in by_sha:
            continue
        pt = {'state_id': s.get('state_id'), 't_wall': s.get('t_wall'),
              'state': st, 'viewpoint': None, 'source': 'shot_state',
              'state_truncated': s.get('state_truncated')}
        points.append(pt)
        if sha:
            by_sha[sha] = pt
        n_from_shot += 1

    if not points:
        for e in (events or []):
            if ctype(e) != 'viewer.checkpoint':
                continue
            st = e.get('state')
            if isinstance(st, dict):
                points.append({'state_id': e.get('state_id'),
                               't_wall': e.get('t_wall'), 'state': st,
                               'viewpoint': e.get('viewpoint'),
                               'source': 'checkpoint'})
    points.sort(key=lambda p: (p.get('t_wall') or 0))

    by_state = {}
    for pt in points:
        by_state.setdefault(pt.get('state_id'), pt)

    specs = []
    n_match = {'shot_state': 0, 'state_id': 0, 'nearest_time': 0}
    for s in (shots or []):
        if not s.get('image_ref'):
            continue
        st = s.get('state')
        if isinstance(st, dict):
            pt = by_sha.get(s.get('state_sha')) or {
                'state_id': s.get('state_id'), 't_wall': s.get('t_wall'),
                'state': st, 'viewpoint': None, 'source': 'shot_state'}
            match = 'shot_state'
        else:
            pt = by_state.get(s.get('state_id'))
            match = 'state_id'
            if pt is None:
                t = s.get('t_wall') or 0
                best = None
                for p2 in points:
                    d = abs((p2.get('t_wall') or 0) - t)
                    if d <= pair_tol_ms and (best is None or d < best[0]):
                        best = (d, p2)
                if best is None:
                    continue
                pt, match = best[1], 'nearest_time'
        n_match[match] = n_match.get(match, 0) + 1
        specs.append({'shot': s, 'point': pt, 'pair_match': match})
    specs.sort(key=lambda x: (x['shot'].get('t_wall') or 0))
    stats = {'n_partial': n_partial, 'n_from_state': n_from_state,
             'n_from_shot_state': n_from_shot, 'pair_match': n_match}
    return points, specs, stats


# 在回放端读回"当前视图指纹"的 JS。字段名与 fingerprint() 对齐；
# 语义也一致：visible 缺省 = true，opacity 缺省 = 1。
LIVE_FP_JS = r"""
() => {
  const v = window.viewer;
  if (!v) return null;
  let js;
  try { js = v.state.toJSON(); } catch (e) { return null; }
  const dims = {};
  try {
    for (const [k, e] of Object.entries(js.dimensions || {})) {
      dims[k] = Array.isArray(e) ? Number(e[0]) : Number(e);
    }
  } catch (e) { /* keep empty */ }
  const layers = (js.layers || []).map(l => {
    let src = l.source;
    if (Array.isArray(src)) src = src.length ? src[0] : null;
    const segRaw = (l.segments && typeof l.segments.length === 'number')
      ? Array.from(l.segments) : [];
    // 不要 Number()：minnie65 的 root id 是 8.6e17，超过 JS 安全整数，
    // 转成数字会悄悄改 id 并让 segments 比较失真。保留十进制字符串。
    const segs = segRaw.map(x => String(x)).filter(x => /^!?\d+$/.test(x))
      .map(x => x.replace(/^!/, ''));
    const style = {};
    for (const k of ['baseSegmentColoring', 'baseSegmentHighlighting',
                     'selectedAlpha', 'notSelectedAlpha', 'objectAlpha',
                     'colorSeed', 'segmentDefaultColor']) {
      if (l[k] !== undefined && l[k] !== null) style[k] = l[k];
    }
    if (l.style && typeof l.style === 'object') {
      for (const k of Object.keys(l.style)) style[k] = l.style[k];
    }
    return {
      name: l.name || null,
      source: (typeof src === 'string') ? src : null,
      type: l.type || null,
      visible: (l.visible !== false),
      opacity: (typeof l.opacity === 'number') ? l.opacity : 1.0,
      segments: segs,
      style: style,
    };
  });
  return {
    position: Array.isArray(js.position) ? js.position.slice(0, 3) : null,
    crossSectionScale: (typeof js.crossSectionScale === 'number') ? js.crossSectionScale : null,
    projectionScale: (typeof js.projectionScale === 'number') ? js.projectionScale : null,
    crossSectionOrientation: Array.isArray(js.crossSectionOrientation) ? js.crossSectionOrientation : null,
    projectionOrientation: Array.isArray(js.projectionOrientation) ? js.projectionOrientation : null,
    layout: js.layout || null,
    showSlices: (typeof js.showSlices === 'boolean') ? js.showSlices : null,
    partialViewport: (js.partialViewport
      && typeof js.partialViewport.length === 'number')
      ? Array.from(js.partialViewport).slice(0, 4).map(Number) : null,
    crossSectionDepth: (typeof js.crossSectionDepth === 'number')
      ? js.crossSectionDepth : null,
    layers: layers,
    dimensions: dims,
  };
}
"""
