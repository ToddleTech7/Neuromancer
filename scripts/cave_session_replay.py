"""CAVE 会话回放器：把 recorder 导出的会话包转成可读的时间线 + 静态 HTML 回放页。

用法（仓库根目录）：
    python scripts/cave_session_replay.py                  # 自动取最新会话包
    python scripts/cave_session_replay.py <session.json>   # 指定会话包
    python scripts/cave_session_replay.py <dir_or_glob>    # 目录/通配符批量
    python scripts/cave_session_replay.py <session.json> --join <join.jsonl>
        # 附服务端 op 对照（“看到→做了什么→提交了什么”单表回放）：
        # op 行按 join 的 client_time_ms 换算插入时间线

输出：
- stdout：会话摘要 + 文本时间线
- results/replay/<session_id>.html：静态回放页（视角轨迹 SVG + 事件表，
  无 JS 依赖，双击即开）

join 自动探测：未显式给 --join 时，依次找
results/joined/<session_id>.join.jsonl 与 <包>/derived/<session_id>.join.jsonl
（工作台默认把 join 写到后者）并自动加载。

如实边界：回放只呈现会话包/join 里"确实记录了什么"；没有记录的显示为空缺，
不做推断填补。位置轨迹以 voxel 坐标绘制（以 dims.scales 为单位）。
"""

import argparse
import glob as glob_mod
import html
import json
from pathlib import Path

try:
    from cave_session_pack import ctype as _ctype
except Exception:
    from scripts.cave_session_pack import ctype as _ctype

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GLOB = 'results/recorder_verify/session_*.json'
OUT_DIR = ROOT / 'results' / 'replay'
JOINED_DIR = ROOT / 'results' / 'joined'


def load_session(path):
    """读会话包：recorder 导出的 JSON，或 cave_session_pack 的目录包。"""
    from cave_session_pack import load_session as _load
    pack = _load(path)
    if 'events' not in pack:
        raise SystemExit(f'不是有效的会话包: {path}')
    return pack


def rel_s(t_wall, t0):
    if t_wall is None or t0 is None:
        return 0.0        # 缺时间的行（如宿主侧截图的旧记录）归到 0 点
    return round((t_wall - t0) / 1000.0, 2)


def fmt_num(v):
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


def fmt_vp(vp):
    if not vp:
        return ''
    parts = []
    pos = vp.get('position')
    if pos:
        parts.append('pos=(' + ','.join(fmt_num(v) for v in pos) + ')')
    if vp.get('crossSectionScale') is not None:
        parts.append('cross=' + fmt_num(vp['crossSectionScale']))
    if vp.get('projectionScale') is not None:
        parts.append('proj=' + fmt_num(vp['projectionScale']))
    ori = vp.get('projectionOrientation')
    if ori:
        parts.append('ori=(' + ','.join(
            f'{v:.3f}' if isinstance(v, float) else str(v)
            for v in ori) + ')')
    if vp.get('visibleLayers'):
        parts.append('layers=' + ','.join(vp['visibleLayers']))
    return ' '.join(parts)


def load_join(join_path):
    """加载 join.jsonl（cave_join.py 产出）与同名 summary（如有）。"""
    records = []
    for line in Path(join_path).read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    summary = None
    sp = Path(join_path).with_name(
        Path(join_path).name.replace('.join.jsonl', '.join_summary.json'))
    if sp.exists():
        summary = json.loads(sp.read_text(encoding='utf-8'))
    return {'records': records, 'summary': summary, 'path': str(join_path)}


def op_line(rec, t0):
    """join 记录 → 时间线伪事件行（客户端时钟换算的 op 行）。"""
    ct = rec.get('client_time_ms')
    t = round((ct - t0) / 1000.0, 2) if ct is not None else None
    ev = rec.get('evidence') or {}
    text = (f"op={rec.get('op_id')} {rec.get('op_type')} "
            f"[{rec.get('match_method')}] "
            f"off={rec.get('clock_offset_ms')}ms "
            f"root_seen={ev.get('root_id_seen_in_session')} "
            f"before={rec.get('before_root_ids')} → "
            f"after={rec.get('after_root_ids')}")
    return t, f"op:{rec.get('op_type')}", text


def build_join_section(join_info, t0):
    """join 对照段：时钟估计 + op 表 + 客户端侧证据。"""
    s = join_info.get('summary') or {}
    recs = join_info.get('records') or []
    clock = s.get('clock') or {}
    head = (
        f'<div class="meta">join 文件：<code>'
        f'{html.escape(join_info.get("path", ""))}</code> ｜ '
        f'时钟 basis={html.escape(str(clock.get("basis")))} '
        f'offset={clock.get("offset_ms")}ms '
        f'±{clock.get("uncertainty_ms")}ms '
        f'({clock.get("n_samples")} 样本)</div>')

    rows = []
    for r in recs:
        ct = r.get('client_time_ms')
        t_txt = (f'{(ct - t0) / 1000.0:.2f}' if ct is not None else '?')
        ev = r.get('evidence') or {}
        ctx = r.get('context') or {}
        n_notes = len(ctx.get('notes') or [])
        n_mc = len(ctx.get('multicuts') or [])
        rows.append(
            f'<tr class="op-row"><td class="t">{t_txt}</td>'
            f'<td>{r.get("op_id")}</td>'
            f'<td>{html.escape(str(r.get("op_type")))}</td>'
            f'<td>{html.escape(str(r.get("match_method")))}</td>'
            f'<td>root={ev.get("root_id_seen_in_session")} '
            f'span={ev.get("in_session_span")}</td>'
            f'<td>备注{n_notes} multicut{n_mc}</td>'
            f'<td>{html.escape(str(r.get("server_time_utc")))}</td></tr>')
    tbl = ('<table><tr><th>t (s)</th><th>op_id</th><th>类型</th>'
           '<th>方式</th><th>证据</th><th>上下文</th><th>服务端时间</th></tr>'
           + (''.join(rows) or '<tr><td colspan="7" class="dim">'
                            '（无匹配 op）</td></tr>') + '</table>')

    ev_parts = []
    miss = s.get('anchors_not_on_server') or []
    if miss:
        ev_parts.append('<li>客户端看到锚点但服务端无此 op（可能被拒/超窗/'
                        '缓存，仅客户端侧证据）：'
                        + html.escape(json.dumps(miss, ensure_ascii=False))
                        + '</li>')
    for tag, label in (('rejected_writes', '客户端观察到的失败写请求'),
                       ('api_errors', 'api_error 事件')):
        items = s.get(tag) or []
        if items:
            ev_parts.append(f'<li>{label}：'
                            + html.escape(json.dumps(items,
                                                    ensure_ascii=False))
                            + '</li>')
    ev_html = (f'<ul>{"".join(ev_parts)}</ul>'
               if ev_parts else '<p class="dim">（无）</p>')

    return ('<h2>服务端 op 对照（join）</h2>' + head + tbl
            + '<h4>客户端侧证据（不冒充服务端事实）</h4>' + ev_html)


def event_line(e, t0):
    t = rel_s(e['t_wall'], t0)
    # 新包用点号事件名，旧包用下划线名；统一走 ctype，否则新包在 HTML 里
    # 只会显示一坨原始 JSON。这里显示规范名，旧包也能读。
    typ = _ctype(e) or e.get('type', '?')
    if typ in ('viewer.state-change', 'viewer.checkpoint'):
        return t, typ, fmt_vp(e.get('viewpoint'))
    if typ == 'input.pointer':
        return t, typ, (f"{e.get('phase')} @({e.get('x')},"
                        f"{e.get('y')}) btn={e.get('button')}")
    if typ == 'input.wheel':
        return t, typ, f"dy={e.get('dy')} @({e.get('x')},{e.get('y')})"
    if typ == 'input.key':
        mods = '+'.join(e.get('modifiers') or [])
        return t, typ, (f"{e.get('key')} ({e.get('code')})"
                        + (f' [{mods}]' if mods else ''))
    if typ == 'viewer.focus':
        pos = e.get('position') or []
        text = ('pos=(' + ','.join(fmt_num(v) for v in pos) + ')'
                if pos else 'pos=(空)')
        if e.get('picked'):
            text += ' picked=' + str(e['picked'])
        elif e.get('picked_miss'):
            text += ' picked=(未命中)'
        return t, typ, text
    if typ == 'selection.change':
        return t, typ, json.dumps(
            e.get('selection') or e.get('layers'),
            ensure_ascii=False)[:160]
    if typ == 'draft.update':
        return t, typ, (f"layer={e.get('layer')} "
                        f"sources={e.get('sources')} sinks={e.get('sinks')}")
    if typ == 'evidence.bookmark':
        return t, typ, e.get('text', '')
    if typ == 'session.start':
        return t, typ, f"page={(e.get('page') or {}).get('url', '')}"
    if typ == 'session.end':
        return t, typ, ''
    if typ == 'viewer.attach':
        return t, typ, e.get('url', '')
    if typ in ('api.request', 'edit.request'):
        return t, typ, (f"{e.get('kind')} {e.get('method')} "
                        f"{(e.get('url') or '')[:140]}")
    if typ in ('api.ack', 'edit.ack'):
        parts = [f"{e.get('kind')} status={e.get('status')}"]
        if e.get('ms') is not None:
            parts.append(f"{e.get('ms')}ms")
        if e.get('server_date_ms') is not None:
            parts.append(f"server_date={e.get('server_date_ms')}")
        if e.get('via'):
            parts.append(f"via={e.get('via')}")
        return t, typ, ' '.join(parts)
    if typ == 'edit.operation':
        return t, typ, f"{e.get('kind')} op_id={e.get('op_id')}"
    if typ == 'keyframe.capture':
        return t, typ, (f"{e.get('shot_id') or '?'} "
                        f"{e.get('moment') or '?'} "
                        f"{e.get('capture_status') or '?'}")
    if typ == 'viewer.dwell':
        return t, typ, f"停留 {e.get('duration_ms')}ms"
    rest = {k: v for k, v in e.items()
            if k not in ('seq', 't_wall', 't_mono', 'type', 'legacy_type')}
    return t, typ, json.dumps(rest, ensure_ascii=False)[:160]


def svg_mouse_heat(points, width=880, height=440, pad=48, bins=36):
    """鼠标落点热力图（页面像素坐标，横轴=屏幕左右）。

    只回答"注意力在屏幕上落在哪块区域"，不代表眼睛看哪。
    """
    pts = [(float(x), float(y)) for x, y in points
           if isinstance(x, (int, float)) and isinstance(y, (int, float))]
    if len(pts) < 5:
        return ('<p class="dim">（鼠标采样不足 5 个，未绘制热力图）</p>')
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx = (maxx - minx) or 1
    spany = (maxy - miny) or 1
    cw = (width - 2 * pad) / bins
    ch = (height - 2 * pad) / bins
    grid = [[0] * bins for _ in range(bins)]
    for x, y in pts:
        gx = min(bins - 1, int((x - minx) / spanx * (bins - 1)))
        gy = min(bins - 1, int((y - miny) / spany * (bins - 1)))
        grid[gy][gx] += 1
    peak = max(max(r) for r in grid) or 1
    cells = []
    for gy in range(bins):
        for gx in range(bins):
            n = grid[gy][gx]
            if not n:
                continue
            a = 0.12 + 0.78 * (n / peak)
            # 纵轴翻过来：屏幕 y 向下，SVG y 向下，保持一致
            cells.append(
                '<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                'fill="rgb(255,86,86)" fill-opacity="%.2f">'
                '<title>(%d, %d) 附近 %d 次</title></rect>'
                % (pad + gx * cw, pad + gy * ch, cw, ch, a,
                   int(minx + (gx + .5) / bins * spanx),
                   int(miny + (gy + .5) / bins * spany), n))
    return (
        '<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="0" y="0" width="%d" height="%d" class="bg"/>%s'
        '<text x="%d" y="%d" class="axis">屏幕 x %d–%d px</text>'
        '<text x="%d" y="%d" class="axis">屏幕 y %d–%d px ｜ 共 %d 个落点</text>'
        '</svg>'
        % (width, height, width, height, ''.join(cells),
           pad, height - 12, int(minx), int(maxx),
           pad, 20, int(miny), int(maxy), len(pts)))


def _vec3(v):
    """把坐标统一成 [x, y, z]。实测 Neuroglancer 给过索引对象 {0,1,2}，
    既不是 list 也没有 length，直接切片会 TypeError。"""
    if not v:
        return None
    try:
        if isinstance(v, (list, tuple)):
            return [float(x) for x in v[:3]]
        return [float(v[i]) for i in range(3)]
    except Exception:
        return None


def _selection_summary(sel):
    """`selection` 的两种形态都收：{layer:{count,ids}} 或 {layer:[ids]}。"""
    if not isinstance(sel, dict):
        return None
    parts = []
    for name, v in sel.items():
        if isinstance(v, dict):
            n = v.get('count')
            if n is None:
                n = len(v.get('ids') or [])
        elif isinstance(v, (list, tuple)):
            n = len(v)
        else:
            n = v
        parts.append(f'{name}:{n}')
    return ','.join(parts) or None


def attention_rows(events, t0, limit=None):
    """把三类数据按时间揉成一张"他在看哪"的表。

    每行：时刻 / 相机中心 / 可见宽度 / 鼠标 3D 点 / 选中的段。
    只拼已经记录到的字段，缺的写 —，不推断。
    选中集是**随时间变化**的，不能拿会话最后一组值回填到每一行。
    """
    out = []
    sel = {}
    # 旧包没有 viewport.panel_px；ui.state.ui.panels 是面板布局的唯一证据。
    panel_w = None
    for e in events:
        if _ctype(e) != 'ui.state':
            continue
        ui = e.get('ui') or {}
        ps = ui.get('panels') or []
        vals = [p[2] for p in ps if isinstance(p, (list, tuple))
                and len(p) >= 3 and isinstance(p[2], (int, float)) and p[2] > 100]
        if vals:
            panel_w = max(vals)
    for e in events:
        typ = _ctype(e)
        if typ == 'selection.change':
            raw = e.get('selection')
            if raw is None:
                raw = e.get('layers')
            if isinstance(raw, dict):
                sel = raw
        if typ == 'viewer.state-change':
            vp = e.get('viewpoint') or {}
            pos = vp.get('position_nm') or []
            npp = vp.get('nm_per_px') or {}
            vpt = vp.get('viewport') or {}
            px = vpt.get('panel_px') or vpt.get('css_px') or []
            width_px = (vpt.get('panel_px') or [None])[0] or panel_w
            if not width_px and px:
                width_px = px[0]
            vieww = None
            if npp.get('x') and width_px:
                vieww = int(npp['x'] * width_px)
            out.append({
                't': rel_s(e['t_wall'], t0), 'kind': '相机',
                'center': _vec3(pos),
                'view_nm': vieww,
                'mouse': None, 'selected': _selection_summary(sel),
                'state_id': e.get('state_id'),
            })
        elif typ == 'viewer.focus':
            pos = e.get('position') or []
            if out:
                out[-1]['mouse'] = _vec3(pos)
            else:
                out.append({'t': rel_s(e['t_wall'], t0), 'kind': '鼠标',
                            'center': None, 'view_nm': None,
                            'mouse': _vec3(pos),
                            'selected': _selection_summary(sel),
                            'state_id': e.get('state_id')})
    if limit:
        out = out[:limit]
    return out


def svg_track(positions, width=880, height=440, pad=48):
    pts = [(p[0], p[1]) for p in positions
           if p and len(p) >= 2
           and all(isinstance(v, (int, float)) for v in p[:2])]
    if len(pts) < 2:
        return '<p class="dim">（可用的视角点不足 2 个，未绘制轨迹）</p>'
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    spanx = (maxx - minx) or 1
    spany = (maxy - miny) or 1

    def tx(x):
        return pad + (x - minx) / spanx * (width - 2 * pad)

    def ty(y):
        return height - pad - (y - miny) / spany * (height - 2 * pad)

    poly = ' '.join(f'{tx(x):.1f},{ty(y):.1f}' for x, y in pts)
    dots = []
    for i, (x, y) in enumerate(pts, 1):
        cls = 'start' if i == 1 else ('end' if i == len(pts) else 'mid')
        dots.append(
            f'<circle class="{cls}" cx="{tx(x):.1f}" cy="{ty(y):.1f}" '
            f'r="{5 if cls != "mid" else 3}"><title>#{i} '
            f'({x:g}, {y:g})</title></circle>')
    return (f'<svg viewBox="0 0 {width} {height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            f'<rect x="0" y="0" width="{width}" height="{height}" '
            f'class="bg"/>'
            f'<polyline points="{poly}" class="track"/>'
            f'{"".join(dots)}'
            f'<text x="{pad}" y="{pad - 14}" class="axis">'
            f'x [{minx:g} … {maxx:g}]</text>'
            f'<text x="{pad}" y="{height - pad + 24}" class="axis">'
            f'y [{miny:g} … {maxy:g}]（上=+y）</text>'
            f'</svg>')


CSS = """
body{background:#15171c;color:#dfe3ea;font:14px/1.6 -apple-system,
'Segoe UI',Consolas,monospace;margin:0;padding:24px 32px;}
h1{font-size:20px;margin:0 0 6px;} h2{font-size:15px;margin:26px 0 8px;
color:#9fd0ff;}
.meta{color:#9aa3b2;font-size:12.5px;margin-bottom:8px;}
table{border-collapse:collapse;width:100%;font-size:12.5px;}
th,td{border-bottom:1px solid #2a2f3a;padding:4px 10px;text-align:left;
vertical-align:top;}
th{color:#9fd0ff;font-weight:600;position:sticky;top:0;background:#15171c;}
tr:hover td{background:#1d2129;}
.t{color:#8f98a8;white-space:nowrap;} .ty{color:#ffd479;white-space:nowrap;}
.note-row td{color:#8ce99a;}
.op-row td{background:#20263a;color:#ffd479;}
svg{max-width:100%;height:auto;border:1px solid #2a2f3a;border-radius:8px;}
.bg{fill:#12141a;} .track{fill:none;stroke:#4dabf7;stroke-width:2;
stroke-linejoin:round;}
circle.start{fill:#51cf66;} circle.end{fill:#ff6b6b;} circle.mid{fill:#748ffc;}
circle{opacity:.9;} .axis{fill:#8f98a8;font-size:12px;}
.dim{color:#8f98a8;} .err{color:#ff8787;} code{color:#e599f7;}
"""


def build_html(pack, lines, join_info=None):
    sid = pack.get('session_id') or 'session'
    page = pack.get('page') or {}
    time_info = pack.get('time') or {}
    counts = pack.get('counts') or {}
    t0 = pack['events'][0]['t_wall'] if pack.get('events') else 0
    dur = round(((time_info.get('end_wall_ms') or t0) - t0) / 1000.0, 1)

    positions = [e['viewpoint'].get('position')
                 for e in pack['events']
                 if _ctype(e) == 'viewer.state-change'
                 and (e.get('viewpoint') or {}).get('position')]

    # 鼠标落点（页面像素）：逐条 pointer + 周期采样的 page 坐标。
    # 只表示"指针在哪"，不表示"眼睛看哪"——页面上写清楚。
    mouse_pts = []
    for e in pack['events']:
        t = _ctype(e)
        if t == 'input.pointer' and isinstance(e.get('x'), (int, float)):
            mouse_pts.append((e['x'], e['y']))
        elif t == 'viewer.focus':
            pg = e.get('page') or []
            if len(pg) >= 2 and all(isinstance(v, (int, float)) for v in pg[:2]):
                mouse_pts.append((pg[0], pg[1]))

    # 不截断：早期版本只取前 200 行，前 200 行恰好都在同一缩放档，
    # 看起来会像“可见宽永远不变”。
    att = attention_rows(pack['events'], t0)
    att_rows = ''.join(
        '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
        % (r['t'],
           '—' if not r['center'] else '(' + ', '.join('%g' % v for v in r['center']) + ')',
           '—' if not r['view_nm'] else '%d nm' % r['view_nm'],
           '—' if not r['mouse'] else '(' + ', '.join('%g' % v for v in r['mouse']) + ')',
           '—' if not r['selected'] else html.escape(str(r['selected'])))
        for r in att)

    rows = []
    for t, typ, text in lines:
        cls = ' class="note-row"' if typ == 'note' else ''
        if str(typ).startswith('op:'):
            cls = ' class="op-row"'
        rows.append(
            f'<tr{cls}><td class="t">{t:.2f}</td>'
            f'<td class="ty">{html.escape(typ)}</td>'
            f'<td>{html.escape(text)}</td></tr>')

    notes = pack.get('notes') or []
    notes_html = ''.join(
        f'<li>[{rel_s(n.get("t_wall", t0), t0):.2f}s] '
        f'{html.escape(n.get("text", ""))}</li>' for n in notes) or \
        '<li class="dim">（无）</li>'

    shots = pack.get('shots') or []
    shots_rows = ''.join(
        f'<tr><td class="t">{rel_s(s.get("t_wall", t0), t0):.2f}</td>'
        f'<td>{s.get("w")}x{s.get("h")}</td>'
        f'<td>{"OK" if s.get("ok") else "空"}</td>'
        f'<td>{html.escape(s.get("note", ""))}</td></tr>'
        for s in shots) or '<tr><td colspan="4" class="dim">（无）</td></tr>'

    errors = pack.get('errors') or []
    errors_html = ''.join(f'<li class="err">{html.escape(str(x))}</li>'
                          for x in errors) or '<li class="dim">（无）</li>'

    counts_str = ' · '.join(f'{k}: {v}' for k, v in sorted(counts.items()))
    join_html = (build_join_section(join_info, t0)
                 if join_info else '')

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>CAVE 会话回放 — {html.escape(sid)}</title>
<style>{CSS}</style></head><body>
<h1>CAVE 会话回放</h1>
<div class="meta">会话 <code>{html.escape(sid)}</code> ｜ 时长 {dur}s ｜
schema {html.escape(str(pack.get('schema', '?')))}</div>
<div class="meta">页面：{html.escape(str(page.get('url', '?')))}</div>
<div class="meta">viewer 检测：
{'是' if (pack.get('viewer') or {}).get('detected') else '否'} ｜
事件计数：{html.escape(counts_str)}</div>

<h2>视角轨迹（position 的 x-y 俯视，voxel 坐标）</h2>
{svg_track(positions)}

<h2>鼠标落点热力图（屏幕像素）</h2>
<p class="dim">只表示<strong>指针</strong>停在哪里，不表示眼睛看哪里；
颜色越亮 = 该区域停留次数越多。</p>
{svg_mouse_heat(mouse_pts)}

<h2>注意点时间线（相机中心 → 鼠标 3D 点 → 选中段）</h2>
<p class="dim">每一行是一次视角变化：相机中心在哪、可见宽度多大、
那一刻鼠标指向哪个 3D 坐标、当前选中了多少段。缺的写 —，不做推断。</p>
<table><tr><th>t (s)</th><th>相机中心 (nm)</th><th>可见宽</th>
<th>鼠标 3D 点 (nm)</th><th>选中</th></tr>
{att_rows}</table>

<h2>时间线（{len(lines)} 条事件）</h2>
<table><tr><th>t (s)</th><th>类型</th><th>内容</th></tr>
{''.join(rows)}</table>

{join_html}

<h2>备注（人工显式表达）</h2><ul>{notes_html}</ul>

<h2>截图元数据</h2>
<table><tr><th>t (s)</th><th>尺寸</th><th>状态</th><th>说明</th></tr>
{shots_rows}</table>

<h2>会话错误</h2><ul>{errors_html}</ul>

<div class="meta" style="margin-top:24px">
如实说明：本页只呈现会话包中确实记录的内容；未记录项不推断填补。</div>
</body></html>
"""


def replay_one(path, join_path=None, auto_join=True, out_path=None,
               quiet=False):
    _say = (lambda *a, **k: None) if quiet else print
    pack = load_session(path)
    events = pack['events']
    t0 = events[0]['t_wall'] if events else 0
    lines = [event_line(e, t0) for e in events]

    sid = pack.get('session_id') or Path(path).stem
    page = pack.get('page') or {}
    counts = pack.get('counts') or {}
    dur = round((events[-1]['t_wall'] - t0) / 1000.0, 1) if events else 0

    # join（显式 --join 优先；否则自动探测 results/joined/<sid>.join.jsonl）
    join_info = None
    jp = Path(join_path) if join_path else None
    if jp is None and auto_join:
        cands = [JOINED_DIR / f'{sid}.join.jsonl']
        pack_dir = pack.get('pack_dir')
        if pack_dir:
            cands.append(Path(pack_dir) / 'derived' / f'{sid}.join.jsonl')
        for cand in cands:
            if cand.exists():
                jp = cand
                break
    if jp is not None and jp.exists():
        join_info = load_join(jp)
        op_lines = [op_line(r, t0) for r in join_info['records']]
        lines = sorted(lines + [x for x in op_lines if x[0] is not None],
                       key=lambda x: x[0])

    _say('=' * 68)
    _say(f'会话回放：{sid}')
    _say(f'  页面    : {page.get("url", "?")}')
    _say(f'  时长    : {dur}s ｜ 事件 {len(events)} ｜ '
          f'viewer 检测 {(pack.get("viewer") or {}).get("detected")}')
    _say('  计数    : ' + ' · '.join(
        f'{k}: {v}' for k, v in sorted(counts.items())))
    _say(f'  备注    : {len(pack.get("notes") or [])} ｜ '
          f'截图 {len(pack.get("shots") or [])} ｜ '
          f'错误 {len(pack.get("errors") or [])}')
    if join_info:
        s = join_info.get('summary') or {}
        clk = s.get('clock') or {}
        n_m = s.get('n_matched', len(join_info['records']))
        _say(f'  join    : {join_info["path"]} ｜ 匹配 {n_m} op ｜ '
              f'时钟 {clk.get("basis")} '
              f'{clk.get("offset_ms")}ms')
    _say('-' * 68)
    for t, typ, text in lines:
        _say(f'  [{t:7.2f}s] {typ:18s} {text[:110]}')

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(out_path) if out_path else (OUT_DIR / f'{sid}.html')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(pack, lines, join_info), encoding='utf-8')
    _say('-' * 68)
    _say(f'HTML 回放页: {out}')
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('target', nargs='?',
                    help='会话包路径 / 目录 / 通配符；缺省取最新')
    ap.add_argument('--join', default=None,
                    help='join.jsonl 路径（cave_join.py 产出）；缺省自动探测')
    ap.add_argument('--out', default=None,
                    help='HTML 输出路径（默认 results/replay/<sid>.html）')
    args = ap.parse_args()

    targets = []
    if args.target:
        p = Path(args.target)
        if p.is_dir():
            if (p / 'manifest.json').exists():
                targets = [p]          # cave_session_pack 目录包
            else:
                targets = sorted(p.glob('session_*.json'))
        elif any(c in args.target for c in '*?['):
            targets = [Path(x) for x in sorted(glob_mod.glob(args.target))]
        else:
            targets = [p]
    else:
        cand = sorted((ROOT / 'results' / 'recorder_verify').glob(
            'session_*.json'), key=lambda x: x.stat().st_mtime)
        targets = cand[-1:]
    if not targets:
        raise SystemExit('未找到会话包')

    for t in targets:
        replay_one(t, join_path=args.join, out_path=args.out)


if __name__ == '__main__':
    main()
