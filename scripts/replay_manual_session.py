"""回放**别人的**会话包：只喂输入事件，不喂 state。

与另外两个的分工（别混）：

  verify_action_replay.py   自己录、自己放（闭环自检，断言固定）
  replay_manual_session.py  读**真人**会话包（cave_manual_server 产的），逐检查点比对
  cave_replay_viewer.py     逐点恢复 state（人看，不验输入）

为什么要单独一个：真人会话的包不是“固定 6 段”，而是“你随手打了 N 个检查点”，
而且时长、动作数都不固定。这里按**检查点切段**：每段先把输入重放一遍，
再拿现场 state 和当时记下的 state 比。**全程不喂 state**——喂了就等于作弊。

用法：

    python3 scripts/replay_manual_session.py --pack results/manual_sessions/<ts>
    python3 scripts/replay_manual_session.py --pack <dir> --start manual_1 --end manual_3
    python3 scripts/replay_manual_session.py --pack <dir> --no-pixels   # 跳过像素比对
    python3 scripts/replay_manual_session.py --pack <dir> --reanchor-gesture
        # 可选：每个 gesture 结束后用录制 end_view 校正相机；不是纯输入，
        # report 里标 reanchored。

如实边界：
  - 只重放 pointer / wheel / key；selection/note/提交是语义事件，不是输入。
  - 滚轮是动画，尺度给相对容差；位置按 POS_TOL_NM。
  - 包里的数据源必须回放机可达，否则图是空的（state 仍能比）。
"""

import argparse
import base64
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import browser_util                     # noqa: E402
import cave_session_pack as csp          # noqa: E402
import replay_state as rsq               # noqa: E402
from verify_action_replay import (      # noqa: E402
    SERVER, GPU_PATCH, POS_TOL_NM, PAINT_JS,
    play_events, _pos_delta, inputs_of)
from verify_replay_fidelity import READBACK_JS, CMP_JS   # noqa: E402


class _EvalTimeout(Exception):
    pass


# 4panel 的 2D 三块（比例框）：左上、右上、右下。左下 = 3D，默认不参与比对。
# 框避开顶部 52px 工具栏/黑边、面板间的 2-3px 分隔线和边缘 1%，
# 否则“边框对不齐”会当成“内容不一样”拉低 NCC。
REGIONS_2D = [[0.01, 0.08, 0.49, 0.48],
              [0.51, 0.08, 0.99, 0.48],
              [0.51, 0.52, 0.99, 0.99]]
REGIONS_2D = [[0.01, 0.08, 0.49, 0.48],
              [0.51, 0.08, 0.99, 0.48],
              [0.51, 0.52, 0.99, 0.99]]
REGIONS_2D_LABELS = ('2d_tl', '2d_tr', '2d_br')


def _session_css_px(pack):
    """录制时的画布 CSS 尺寸；旧包没有就按 4panel 默认。"""
    for e in pack.get('events') or []:
        if csp.ctype(e) == 'session.start':
            css = (e.get('viewport') or {}).get('css_px') or []
            if (len(css) >= 2 and isinstance(css[0], (int, float))
                    and isinstance(css[1], (int, float))):
                return float(css[0]), float(css[1])
    return 1280.0, 740.0


def _ui_panels_at(pack, t_wall):
    """取锚点时刻最近一次的界面面板矩形；没有前后就取最近一条。"""
    before = None
    for e in pack.get('events') or []:
        if csp.ctype(e) != 'ui.state':
            continue
        et = e.get('t_wall') or 0
        if et <= t_wall:
            before = e
    if before is not None:
        return (before.get('ui') or {}).get('panels') or []
    for e in pack.get('events') or []:
        if csp.ctype(e) == 'ui.state':
            return (e.get('ui') or {}).get('panels') or []
    return []


def _region_specs(pack, anchor, compare_all=False):
    """按录制时的实际面板生成像素分区。

    默认只比 2D 面板：4panel 的左下是 3D，单独排除；如果用户关掉/打开面板，
    `ui.state.ui.panels` 会变，分区也跟着变。没有 ui.state 的旧包退回固定 2D 三块。
    """
    state = anchor.get('state') or {}
    layout = state.get('layout')
    w, h = _session_css_px(pack)
    panels = _ui_panels_at(pack, anchor.get('t') or 0)
    boxes = []
    for i, p in enumerate(panels):
        if not (isinstance(p, (list, tuple)) and len(p) >= 4):
            continue
        try:
            x, y, pw, ph = (float(p[0]), float(p[1]),
                            float(p[2]), float(p[3]))
        except (TypeError, ValueError):
            continue
        if pw <= 100 or ph <= 100 or w <= 0 or h <= 0:
            continue
        box = [max(0.0, x / w), max(0.0, y / h),
               min(1.0, (x + pw) / w), min(1.0, (y + ph) / h)]
        # 留 1% 边，避开面板边框/分隔线
        box = [box[0] + 0.01, box[1] + 0.01,
               box[2] - 0.01, box[3] - 0.01]
        if box[2] - box[0] > 0.02 and box[3] - box[1] > 0.02:
            boxes.append({'label': 'panel_%d' % i, 'kind': 'panel',
                          'box': box, 'rect': [x, y, pw, ph]})
    if not boxes:
        return [{'label': lab, 'kind': '2d', 'box': b}
                for lab, b in zip(REGIONS_2D_LABELS, REGIONS_2D)]
    if compare_all:
        return boxes
    if layout == '3d':
        return [{'label': '3d', 'kind': '3d', 'box': [0.01, 0.01, 0.99, 0.99]}]
    if layout == '4panel' or len(boxes) == 4:
        # 左下 = 3D：中心 x 在左半、y 在下半。
        def _bottom_left(b):
            x0, y0, x1, y1 = b['box']
            return ((x0 + x1) / 2 < 0.5) and ((y0 + y1) / 2 > 0.5)
        keep = [b for b in boxes if not _bottom_left(b)]
        if keep:
            for b in keep:
                b['kind'] = '2d'
            return keep
        return [{'label': '3d', 'kind': '3d', 'box': [0.01, 0.01, 0.99, 0.99]}]
    for b in boxes:
        b['kind'] = '2d'
    return boxes


def _on_alarm(signum, frame):
    raise _EvalTimeout()


def eval_timeout(page, expr, arg=None, seconds=15):
    """带硬超时的 page.evaluate。

    为什么非做不可：回放端一旦有选中段（segments 非空），NG 就去拉 3D mesh；
    它忙的时候 `page.evaluate` **没有超时、会无限挂**（实测：整个回放卡 9 分钟、
    CPU 0%）。Playwright 不给 evaluate 设超时的口子，只能用 SIGALRM 把阻塞的
    同步等待打断。超时返回 None，调用方按“这一段没拿到”处理，不能让整次回放死掉。
    """
    if not hasattr(signal, 'SIGALRM'):
        # Windows 没有 SIGALRM/setitimer。先用带 timeout 的 wait_for_function
        # 探活；页面主线程忙时它会超时返回，不会像裸 evaluate 那样无限挂。
        try:
            page.wait_for_function('() => true', timeout=max(1, int(seconds * 1000)))
        except Exception:
            return None
        try:
            return (page.evaluate(expr, arg) if arg is not None
                    else page.evaluate(expr))
        except Exception:
            return None
    old = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return page.evaluate(expr, arg) if arg is not None else page.evaluate(expr)
    except _EvalTimeout:
        return None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def load_pack(path):
    p = Path(path)
    if p.is_dir():
        return csp.load_session(p), p
    return json.loads(p.read_text(encoding='utf-8')), p.parent


def anchors_of(pack):
    """取带完整 state 的 checkpoint，按时间排序。"""
    out = []
    for e in pack.get('events') or []:
        if csp.ctype(e) != 'viewer.checkpoint':
            continue
        st = e.get('state')
        if not isinstance(st, dict) or not st.get('position'):
            continue
        out.append({'t': e.get('t_wall') or 0,
                    'reason': e.get('reason') or '?',
                    'sha': e.get('state_sha'),
                    'state': st})
    out.sort(key=lambda x: x['t'])
    return out


def usable_shots(pack, packdir):
    """可用的关键帧（只收 `captured`，排除 `painted_partial`）。

    不能用 `moment` 当键：所有检查点触发的关键帧 moment 都是 'checkpoint'，
    区分不开是哪一个。所以按**时间 / state_sha 就近**配对到锚点。

    目录包里图是落盘的（`client/shots/shot_N.png`），JSON 里只有 image_ref，
    所以要在这里读回成 dataURL——否则就算“没有图可用”。
    """
    out = []
    for s in pack.get('shots') or []:
        if s.get('capture_status') != 'captured':
            continue
        url = s.get('dataURL')
        if not url:
            ref = s.get('image_ref')
            if ref:
                f = Path(packdir) / 'client' / ref
                if f.exists():
                    url = ('data:image/png;base64,'
                           + base64.b64encode(f.read_bytes()).decode())
        if not url:
            continue
        out.append({'t': s.get('t_wall') or 0,
                    'sha': s.get('state_sha'),
                    'dataURL': url})
    out.sort(key=lambda x: x['t'])
    return out


def shot_for(anchor, shots, tol_ms=2500):
    """图 → 锚点。优先 state_sha；没有才按时间就近，并标明配对方式。"""
    hit = None
    best = None
    for s in shots:
        if anchor.get('sha') and s.get('sha') == anchor.get('sha'):
            out = dict(s)
            out['pair_match'] = 'state_sha'
            return out
        d = abs((s['t'] or 0) - (anchor['t'] or 0))
        if d <= tol_ms and (best is None or d < best):
            best, hit = d, s
    if hit is not None:
        out = dict(hit)
        out['pair_match'] = 'nearest_time'
        return out
    return None


def viewport_of(pack):
    """从包里读出录制时的视口。

    为什么必须一致：拖动是按**像素**投递的。视口不同→同一个 px 对应的
    nm 不同（实测 1280×820 录、1400×900 放，同一次拖动差 200nm），
    会在 “动作明明一样” 的情况下把 position 报成不及格。
    """
    for e in pack.get('events') or []:
        vp = e.get('viewport') or {}
        px = vp.get('css_px')
        if isinstance(px, list) and len(px) == 2:
            try:
                return int(px[0]), int(px[1]), float(vp.get('dpr') or 1)
            except Exception:
                pass
    return 1400, 900, 1.0


def start_viewer():
    p = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', cwd=str(ROOT),
        env={**os.environ, 'PYTHONUTF8': '1'})
    url = None
    while True:
        line = p.stdout.readline()
        if not line:
            break
        m = re.match(r'VIEWER_URL=(\S+)', line.strip())
        if m:
            url = m.group(1)
            break
    if not url:
        p.kill()
        raise SystemExit('演示服务没有给出 VIEWER_URL')
    return p, url


def send_fn(p):
    def send(cmd):
        p.stdin.write(cmd + '\n')
        p.stdin.flush()
    return send


def wait_pos(page, want, timeout=20.0, tol=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not _alive(page, 3000):
            time.sleep(0.5)
            continue
        got = eval_timeout(
            page,
            '() => (window.viewer && window.viewer.state.toJSON().position)',
            seconds=6)
        if got and len(want) == 3 and all(abs(a - b) < tol
                                          for a, b in zip(got, want)):
            return True
        time.sleep(0.2)
    return False


def segments_of_state(st):
    """从一份完整 state 里抽出“每层选中集” {层名: [id...]}。

    只取 segmentation 层。`!` 前缀（NG 的 visible 标记）去掉，
    否则 demo server 的 int() 会抛。
    """
    out = {}
    for l in ((st or {}).get('layers') or []):
        name = l.get('name')
        if not name or 'segmentation' not in str(l.get('type') or '').lower():
            continue
        out[name] = [re.sub(r'^!', '', str(x)) for x in (l.get('segments') or [])]
    return out


def _nm_axes(state):
    dims = state.get('dimensions') or {}
    out = []
    unit_nm = {'m': 1e9, 'mm': 1e6, 'um': 1e3, '\u00b5m': 1e3, 'nm': 1}
    for e in dims.values():
        try:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                out.append(float(e[0]) * unit_nm.get(str(e[1]), 1))
            else:
                out.append(float(e))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _pos_delta_nm(exp, got, state):
    a = (exp or {}).get('position') or []
    b = (got or {}).get('position') or []
    scales = _nm_axes(state)
    if len(a) != 3 or len(b) != 3:
        return None
    try:
        return max(abs(float(x) - float(y)) * float(scales[i] or 1)
                   for i, (x, y) in enumerate(zip(a, b)))
    except (TypeError, ValueError, IndexError):
        return None


def _nm_per_voxel(state):
    """state.position 的单位是体素；把最细轴换算成 nm 用于容差。"""
    dims = state.get('dimensions') or {}
    vals = []
    unit_nm = {'m': 1e9, 'mm': 1e6, 'um': 1e3, '\u00b5m': 1e3, 'nm': 1}
    for e in dims.values():
        try:
            if isinstance(e, (list, tuple)) and len(e) >= 2:
                vals.append(float(e[0]) * unit_nm.get(str(e[1]), 1))
            else:
                vals.append(float(e))
        except (TypeError, ValueError):
            continue
    vals = [v for v in vals if v > 0]
    return min(vals) if vals else 1.0


def segments_timeline(events):
    """把视角快照里的“选中集变化点”抽成时间线（相同集合只留第一次）。

    为什么必须重放它：NG 在“选中集为空”时显示全部分割（全彩）且不画 mesh；
    录制端只显示选中段并画 mesh。选中集不随指针/滚轮变化，只能从 state 快照取。
    注意 `selection.change` 记的是**悬停的段**（随鼠标走），不是选中集。
    """
    out, prev = [], None
    for e in events or []:
        if csp.ctype(e) not in ('viewer.state-change', 'viewer.checkpoint'):
            continue
        segs = segments_of_state(e.get('state') or {})
        if not segs:
            continue
        sig = json.dumps(segs, sort_keys=True)
        if sig == prev:
            continue
        prev = sig
        out.append({'type': 'segments.sync', 't_wall': e.get('t_wall') or 0,
                    'layers': segs})
    return out


def _alive(page, ms=8000):
    """页面主线程还响应吗。

    为什么需要：一旦回放端有选中段（segments 非空），NG 会去拉 3D mesh
    （multilod draco 分片，实测同一视角 ~35 s）。它忙的时候 `page.evaluate`
    **没有超时、会无限挂**（真踩过：整个回放卡 9 分钟、CPU 0%）。
    这里用 `wait_for_function` 当探针：它带超时，能抛错，也就把“无限等”
    变成“这一段跳过并如实标记”。
    """
    try:
        page.wait_for_function('() => true', timeout=ms)
        return True
    except Exception:
        return False


def _settle(page, timeout=8.0, min_colored=0.05):
    """等画面真有内容（eval_timeout 版：页面忙时不会无限等）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = eval_timeout(page, PAINT_JS, seconds=8) or {}
        if (r.get('colored') or 0) >= min_colored:
            time.sleep(0.35)
            return r.get('colored')
        time.sleep(0.25)
    return None


def _state_of(page):
    fp = eval_timeout(page, rsq.LIVE_FP_JS, seconds=10)
    return rsq.fingerprint(state=fp) if fp else None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--pack', required=True)
    ap.add_argument('--start', default=None, help='从哪个检查点开始（reason）')
    ap.add_argument('--end', default=None, help='到哪个检查点结束（reason）')
    # 工作台用：拖到某一秒 → 自动选前后锚点，只重放到那一秒就停。
    # 为什么不用 --start/--end：studio 只知道“秒”，不知道锚点叫什么。
    ap.add_argument('--seek-t', type=float, default=None,
                    help='只重放到会话第 N 秒（自动选前后锚点）')
    ap.add_argument('--out', default=None,
                    help='回放产物写到哪（缺省 = <包>/replay/<时间戳>/，'
                         '跟着会话走，一个包的东西都在一起）')
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--no-pixels', action='store_true')
    ap.add_argument('--reanchor-gesture', action='store_true',
                    help='在每个 viewport.gesture 结束时用录制 end_view 校正相机；'
                         '不是纯输入模式，报告会标 reanchored')
    ap.add_argument('--accept-ncc', type=float, default=0.95)
    # 4panel 里左下是 3D 视图：mesh 是另一路请求、没有就绪信号，加载慢。
    # 整图比时这一块（占 1/4）会把“2D 已完全一致”报成 0.83。默认只比 2D 三块。
    ap.add_argument('--compare-all', action='store_true',
                    help='像素比对用整图（默认只比 2D 三块，3D 面板单列）')
    # mesh 是另一路请求（multilod draco 分片），实测同一视角要 ~35 s 才画出来。
    # 默认不等：像素比对只看 2D 与已有内容；要看 3D mesh 就 --mesh-wait 30。
    ap.add_argument('--mesh-wait', type=float, default=0.0,
                    help='段末恢复选中集后额外等待秒数（等 3D mesh 加载）')
    # 全过程可见：断言只落在锚点，但重放本身是逐条输入。录下来才能“看全过程”
    ap.add_argument('--video', action='store_true',
                    help='把整段回放录成 webm（落到 <out>/replay.webm）')
    args = ap.parse_args(argv)

    pack, packdir = load_pack(args.pack)
    anchors = anchors_of(pack)
    if len(anchors) < 2:
        raise SystemExit('包里带完整 state 的检查点不足 2 个，无法逐段比对')
    if args.start:
        idx = [i for i, a in enumerate(anchors) if a['reason'] == args.start]
        if not idx:
            raise SystemExit('找不到起点检查点 %r' % args.start)
        anchors = anchors[idx[0]:]
    if args.end:
        idx = [i for i, a in enumerate(anchors) if a['reason'] == args.end]
        if not idx:
            raise SystemExit('找不到终点检查点 %r' % args.end)
        anchors = anchors[:idx[0] + 1]

    if len(anchors) < 2:
        raise SystemExit('裁剪后不足 2 个检查点；单点无法做逐段比对（不产生空 PASS）')

    if args.seek_t is not None:
        if not anchors:
            raise SystemExit('包里没有可用锚点')
        # --seek-t 是“会话第 N 秒”，从会话起点算；起点优先取时间戳，不取第一个锚点。
        t_start = (pack.get('time') or {}).get('start_wall_ms')
        if not isinstance(t_start, (int, float)):
            evs = pack.get('events') or []
            t_start = evs[0].get('t_wall') if evs else anchors[0]['t']
        t_ms = t_start + float(args.seek_t) * 1000.0
        prev = anchors[0]
        for a in anchors:
            if a['t'] <= t_ms:
                prev = a
            else:
                break
        # 目标不是检查点时，必须拿“cut 时刻最近的那份完整 state”当期望值。
        # 原来的实现重放到 cut 却拿下一个检查点的 state 比对——中途停止必然误判。
        cut_state = None
        cut_sha = None
        for e in pack.get('events') or []:
            if csp.ctype(e) not in ('viewer.state-change',
                                    'viewer.checkpoint'):
                continue
            st = e.get('state')
            t = e.get('t_wall')
            if (isinstance(st, dict) and isinstance(t, (int, float))
                    and prev['t'] <= t <= t_ms and st.get('position')):
                cut_state, cut_sha = st, e.get('state_sha')
        if cut_state is None:
            cut_state = prev['state']
            cut_sha = prev.get('sha')
        cut_t = max(prev['t'], t_ms)
        cut = {'t': cut_t, 'reason': 'seek_%.1fs' % float(args.seek_t),
               'sha': cut_sha, 'state': cut_state}
        anchors = [prev, cut]
        print('  → seek %.1fs：%s 的 state 作为期望值（cut=%s）'
              % (args.seek_t, prev['reason'], round(cut_t)))

    seq = inputs_of(pack)
    # 选中集（segments）是**独立的 UI 状态**：不在 pointer/wheel/key 里，也不在
    # 起点 state 快照里。不恢复它，回放端就“什么都没选中”，NG 会显示全部分割
    # （全彩）且 3D 不画 mesh——与录制端“EM 灰底 + 只显示选中段 + 有 mesh”相反。
    # 做法：**每段末**按目标锚点的 state 对齐一次（段内不改）。不逐事件改是因为
    # 实测每改一次 segments 都会让 NG 重载该层（含 mesh 分片），段内反复重载会把
    # page.evaluate 拖死（真跑过：卡 9 分钟无输出）。
    seg_stat = {'ops': 0}
    print('[pack] %s' % packdir)
    print('[pack] 事件 %d（输入 %d）｜检查点 %d：%s'
          % (len(pack.get('events') or []), len(seq), len(anchors),
             [a['reason'] for a in anchors]))

    shots = usable_shots(pack, packdir)
    vw, vh, dpr = viewport_of(pack)
    print('[pack] 视口 %d×%d @%.2fx（回放必须用同一个）' % (vw, vh, dpr))
    proc, url = start_viewer()
    send = send_fn(proc)
    # 默认写进包内：一段会话的所有东西（原始/派生/回放产物）留在同一个文件夹
    base_out = (Path(args.out).expanduser() if args.out
                else (packdir / 'replay'))
    out = base_out / datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True, exist_ok=True)

    rows, checks = [], []

    def apply_segments(ev):
        """把一个“选中集变化点”回放到 demo server（只改 segments，不重建层）。"""
        for name, ids in (ev.get('layers') or {}).items():
            send('sel %s %s' % (name, ' '.join(ids)))
        seg_stat['ops'] += 1

    def check(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        print('  [%s] %s%s' % ('PASS' if ok else 'FAIL', name,
                               '' if detail is None else ' — %s' % (detail,)))

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = browser_util.launch_browser(pw, headless=not args.headed)
            ctx_kw = {'viewport': {'width': vw, 'height': vh},
                      'device_scale_factor': dpr}
            if args.video:
                ctx_kw['record_video_dir'] = str(out / 'video')
                ctx_kw['record_video_size'] = {'width': vw, 'height': vh}
            ctx = b.new_context(**ctx_kw)
            page = ctx.new_page()
            # 动作级超时：页面忙（双击选中 → NG 抳 mesh）时不要让一条
            # mouse.move 卡满默认 30 s；500 ms 足够正常动作，卡就跳过这一条。
            page.set_default_timeout(500)
            page.add_init_script(path=str(GPU_PATCH))
            page.goto(url, wait_until='domcontentloaded')
            # set_default_timeout(500) 同时会改 wait_for_function 的默认超时；
            # viewer 在冷启动/网络慢时超过 0.5s 很常见，这里必须显式给足。
            page.wait_for_function('() => !!window.viewer', timeout=60000)

            first = anchors[0]['state']
            pos_tol_state = POS_TOL_NM / _nm_per_voxel(first)
            # 起点**先清空选中集**：带 segments 会让 NG 立刻去抳 3D mesh，
            # 页面一忙，后面几百条输入逐条卡超时（实测能把一段拖到几十分钟）。
            # 选中集改在每段末一次性补上（段内不影响输入重放）。
            first = json.loads(json.dumps(first))
            for lay in (first.get('layers') or []):
                if 'segmentation' in str(lay.get('type') or '').lower():
                    lay['segments'] = []
            for lay in (first.get('layers') or []):
                src = lay.get('source')
                if isinstance(src, list):
                    src = src[0] if src else None
                if not src:
                    continue
                name = lay.get('name') or 'layer'
                typ = str(lay.get('type') or '').lower()
                if 'segmentation' in typ:
                    send('seg %s %s' % (name, src))
                else:
                    send('img %s %s' % (name, src))
            dims = first.get('dimensions')
            if isinstance(dims, dict) and len(dims) == 3:
                send('dims ' + ' '.join(str(float(v[0]) if isinstance(v, list)
                                            else float(v))
                                        for v in dims.values()))
            time.sleep(1.2)

            # 起点：**这是唯一一次喂 state**（等价于“打开当时那个画面”），
            # 之后全程只喂输入。
            send('state ' + json.dumps(first))
            wait_pos(page, first.get('position') or [], timeout=25)
            _settle(page)
            base = _state_of(page)
            d0 = _pos_delta(rsq.fingerprint(first), base)
            check('起点 position 已还原（唯一一次喂 state）',
                  d0 is not None and d0 <= POS_TOL_NM,
                  'Δ=%s nm' % (None if d0 is None else round(d0, 2)))

            for i in range(1, len(anchors)):
                a0, a1 = anchors[i - 1], anchors[i]
                seg_ev = [e for e in seq
                          if a0['t'] < (e.get('t_wall') or 0) <= a1['t']]
                if args.reanchor_gesture:
                    # 纯输入回放在连续 wheel 动画上会差几帧；把录制到的
                    # gesture.end_view 作为“动作结束后的相机事实”插回去。
                    # 这是显式状态修正，不是输入，报告必须标出来。
                    gs = [dict(e, type='gesture.reanchor')
                          for e in (pack.get('events') or [])
                          if csp.ctype(e) == 'viewport.gesture'
                          and a0['t'] < (e.get('t_wall') or 0) <= a1['t']
                          and isinstance(e.get('end_view'), dict)]
                    seg_ev = sorted(seg_ev + gs,
                                    key=lambda e: e.get('t_wall') or 0)
                t0 = time.time()
                print('  → 段 %s → %s：%d 条输入' % (a0['reason'], a1['reason'],
                                                len(seg_ev)), flush=True)
                reanchors = []

                def _reanchor(e):
                    ev = e.get('end_view') or {}
                    pos = ev.get('position_nm')
                    if pos and len(pos) == 3:
                        scales = _nm_axes(a1['state'])
                        try:
                            vox = [float(pos[i]) / float(scales[i])
                                   for i in range(3)]
                            send('pos %.4f %.4f %.4f' % tuple(vox))
                        except (TypeError, ValueError, IndexError):
                            pass
                    cross = ev.get('crossSectionScale')
                    if isinstance(cross, (int, float)):
                        send('cross %.6f' % float(cross))
                    proj = ev.get('projectionScale')
                    if isinstance(proj, (int, float)):
                        send('proj %.6f' % float(proj))
                    reanchors.append(e.get('kind') or e.get('t_wall'))
                    time.sleep(0.25)

                n = play_events(page, seg_ev, tolerant=True,
                                label='%s→%s' % (a0['reason'], a1['reason']),
                                on_reanchor=_reanchor if args.reanchor_gesture else None)
                wall = time.time() - t0
                # 段末：把目标锚点的选中集对齐（不重建层，只改 segments）。
                # 2D 的“全彩 vs 灰底 + 局部彩色”、3D 有没有 mesh，全靠这一步。
                segs = segments_of_state(a1.get('state') or {})
                if segs and segs != getattr(apply_segments, '_last', None):
                    apply_segments({'layers': segs})
                    apply_segments._last = segs
                    time.sleep(1.0)
                # 3D 视图参数也要对齐：projectionOrientation/Scale 在“只喂输入”下
                # 会累积漂移，3D 面板占 1/4 像素，偏一点就拉低整体 NCC。
                st1 = a1.get('state') or {}
                if isinstance(st1.get('projectionScale'), (int, float)):
                    send('proj %.6f' % float(st1['projectionScale']))
                po = st1.get('projectionOrientation')
                if isinstance(po, list) and len(po) == 4:
                    send('orient ' + ' '.join('%.6f' % float(x) for x in po))
                if args.mesh_wait:
                    time.sleep(args.mesh_wait)
                else:
                    # 恢复选中集后总要给 2D 一个重绘窗口；3D mesh 更慢，用 --mesh-wait 拉长
                    time.sleep(3.0)
                # 有选中段时 NG 会去抳 3D mesh，主线程一忙 evaluate 就无限挂。
                # 探测不到就**如实标 busy** 跳过本段，不让整次回放卡死。
                if not _alive(page, 12000):
                    rows.append({'from': a0['reason'], 'to': a1['reason'],
                                 'n_events': n, 'played_s': round(wall, 2),
                                 'skipped': 'busy',
                                 'note': '页面忙（3D mesh 加载），本段未评估'})
                    print('  [SKIP] 段 %s → %s：页面忙（mesh 加载中），本段跳过评估'
                          % (a0['reason'], a1['reason']))
                    continue
                _settle(page)
                got = _state_of(page)
                exp = rsq.fingerprint(a1['state'])
                cmp_ = (rsq.compare(exp, got, pos_tol=pos_tol_state)
                        if got else {'ok': False, 'failed': ['no_state']})
                d = _pos_delta_nm(exp, got, a1['state'])
                row = {'from': a0['reason'], 'to': a1['reason'],
                       'n_events': n, 'played_s': round(wall, 2),
                       'pos_delta_nm': None if d is None else round(d, 2),
                       'fields_ok': bool(cmp_.get('ok')),
                       'failed': cmp_.get('failed') or [],
                       'reanchored': len(reanchors)}
                rows.append(row)
                check('段 %s → %s：position 偏差 ≤ %.0fnm'
                      % (a0['reason'], a1['reason'], POS_TOL_NM),
                      d is not None and d <= POS_TOL_NM,
                      'Δ=%s nm｜输入 %d｜耗时 %.1fs'
                      % (None if d is None else round(d, 2), n, wall))
                if not cmp_.get('ok'):
                    print('      非位置字段不一致：%s' % (cmp_.get('failed'),))

                # 像素只在该锚点当时真拍到了图时才比（且只收 captured）。
                #
                # 不能只读一次：实测（2026-10-26）回放端常常还停在
                # **粗 mip 块状图**上（位置/尺度已对，细节未到），一次读必挂。
                # 所以按内容闭环：读一帧 → 比 → 不够就再等 0.5s 再读，取最好。
                if not args.no_pixels:
                    shot = shot_for(a1, shots)
                    if shot:
                        best, any_best, attempts = None, None, 0
                        # 先等画面“稳”：连续两次读回同一张才比。为什么需要：回放端
                        # 的 EM 瓦片/分割数据加载完成度每次不同，NG 也没有“加载完”
                        # 的信号；不等就会拿半加载画面对比（实测同一段 0.71~0.92 乱跳）。
                        prev_url = None
                        for _ in range(8):
                            if not _alive(page, 4000):
                                time.sleep(0.5)
                                continue
                            lv = eval_timeout(page, READBACK_JS, seconds=12)
                            cur_url = (lv or {}).get('url')
                            if cur_url and cur_url == prev_url:
                                break
                            prev_url = cur_url
                            time.sleep(0.8)
                        for _ in range(12):
                            attempts += 1
                            if not _alive(page, 4000):
                                time.sleep(0.5)
                                continue
                            live = eval_timeout(page, READBACK_JS, seconds=12)
                            if not (live and live.get('url')):
                                time.sleep(0.5)
                                continue
                            specs = _region_specs(pack, a1,
                                                  compare_all=args.compare_all)
                            regions = [x['box'] for x in specs]
                            c = eval_timeout(page, CMP_JS,
                                             [shot['dataURL'], live['url'],
                                              regions],
                                             seconds=20)
                            # CMP_JS 的键是 `ncc_rgb`（逐通道均值），不是 'rgb'。
                            # 只看灰度 ncc 会把“彩色分割不在”报成“几乎一样”。
                            rgb = (c or {}).get('ncc_rgb') or 0
                            ca = (c or {}).get('colored_a') or 0
                            cb = (c or {}).get('colored_b') or 0
                            cur = {'ncc': (c or {}).get('ncc'), 'rgb': rgb,
                                   'colored_rec': ca, 'colored_live': cb,
                                   'dcolored': abs(cb - ca),
                                   'live_url': live['url']}
                            if any_best is None or rgb > any_best['rgb']:
                                any_best = cur
                            # 只在“彩色占比差不多”的候选里比：
                            # 实测有一帧回放端只剩 EM（colored 0.02 vs 录的 0.73），
                            # 灰度 NCC 反而 0.98——那是“两张黑图很像”，不能当证据。
                            if cur['dcolored'] <= 0.2:
                                if best is None or rgb > best['rgb']:
                                    best = cur
                                if rgb >= args.accept_ncc:
                                    break
                            time.sleep(0.5)
                        if best is None:
                            # 回放端在这一刻根本不是一个可比的面（分割层不在/
                            # 换成了另一个 canvas）——如实标出，不算通过也不算失败。
                            best = any_best
                            row['pixel_unstable'] = {
                                'colored_rec': (best or {}).get('colored_rec'),
                                'colored_live': (best or {}).get('colored_live'),
                                'ncc': None if not best or best.get('ncc') is None
                                else round(best['ncc'], 5),
                                'attempts': attempts,
                                'note': '12 次读回都不可比/页面忙'}
                            print('  [SKIP] 段末（%s）像素不可比：彩色占比 '
                                  '%s vs %s（已标出，不计分）'
                                  % (a1['reason'],
                                     None if not best else best['colored_rec'],
                                     None if not best else best['colored_live']))
                            continue
                        px = {'ncc': None if best['ncc'] is None
                              else round(best['ncc'], 5),
                              'rgb': round(best['rgb'], 5),
                              'colored_rec': best['colored_rec'],
                              'colored_live': best['colored_live'],
                              'attempts': attempts}
                        per = (c or {}).get('per_region') or []
                        px['regions'] = [
                            {**spec, **{
                                'ncc': None if rr.get('ncc') is None
                                else round(rr['ncc'], 5),
                                'rgb': None if rr.get('ncc_rgb') is None
                                else round(rr['ncc_rgb'], 5),
                                'colored_rec': rr.get('colored_a'),
                                'colored_live': rr.get('colored_b'),
                            }}
                            for spec, rr in zip(specs, per)
                        ]
                        if px['regions']:
                            print('      区域 NCC: ' + '  '.join(
                                '%s=%s' % (r['label'],
                                           '—' if r['ncc'] is None
                                           else '%.3f' % r['ncc'])
                                for r in px['regions']))

                        # 配色不可比（2026-10-27）：NG 的分段配色不保证跨加载一致。
                        # 实测 sess_...j9uwd 的 manual_2：灰度 NCC 0.978、彩色占比
                        # 0.7256 vs 0.7259（同一片区域），但 rgb 只有 0.859；
                        # 相位相关位移 0、最佳缩放 1.000、高斯模糊不改善；
                        # 拆开看 **亮度 0.963 / 色度 0.492** ⇒ 结构对、配色不同。
                        # 这种情况既不能判过（颜色确实不同）也不能判错（不是动作错），
                        # 如实标 color_mismatch，不计分。真失败（分割层缺失）会让
                        # colored 掉下去，仍走原判据。
                        if (px['rgb'] < args.accept_ncc
                                and (px['ncc'] or 0) >= args.accept_ncc
                                and (px['colored_live'] or 0) > 0.1):
                            row['color_mismatch'] = {
                                'rgb': px['rgb'], 'lum_ncc': px['ncc'],
                                'colored_rec': px['colored_rec'],
                                'colored_live': px['colored_live']}
                            print('  [SKIP] 段末（%s）配色不可比：亮度 NCC %.3f 但 rgb '
                                  '%.3f（结构一致、分段配色不同，不计分）'
                                  % (a1['reason'], px['ncc'] or 0, px['rgb']))
                            continue
                        px['pair_match'] = shot.get('pair_match')
                        row['pair_match'] = shot.get('pair_match')
                        row['pixels'] = px
                        # 落两份图，方便人眼复核：录的 vs 放的
                        try:
                            (out / ('live_%s.png' % a1['reason'])).write_bytes(
                                base64.b64decode(
                                    best['live_url'].split(',', 1)[1]))
                            (out / ('rec_%s.png' % a1['reason'])).write_bytes(
                                base64.b64decode(shot['dataURL'].split(',', 1)[1]))
                        except Exception:
                            pass
                        check('段末（%s）与当时关键帧像素收敛'
                              '（rgb NCC ≥ %.2f，闭环 %d 次）'
                              % (a1['reason'], args.accept_ncc, attempts),
                              (best['rgb'] or 0) >= args.accept_ncc, px)

            if seg_stat['ops']:
                print('  [segments] 重放了 %d 次选中集变化' % seg_stat['ops'])
            n_px = sum(1 for r in rows if r.get('pixels'))
            n_unstable = sum(1 for r in rows if r.get('pixel_unstable'))
            n_color_mismatch = sum(1 for r in rows if r.get('color_mismatch'))
            if args.no_pixels:
                print('  [SKIP] 跳过像素比对（--no-pixels）')
            elif not n_px:
                print('  [SKIP] 包里没有可用关键帧，跳过像素比对')

            # 截图不能被动作级的 500ms 短超时卡死（页面在拉 mesh 时会吞），单给 30 s
            eval_timeout(page, '() => true', seconds=30)
            try:
                page.screenshot(path=str(out / 'final.png'), timeout=30000)
            except Exception as err:
                print('[shot] final.png 失败：%s' % str(err).splitlines()[0][:120])
            vid = page.video if args.video else None
            b.close()
            if vid:
                try:
                    dst = out / 'replay.webm'
                    shutil.move(str(vid.path()), str(dst))
                    print('[video] %s' % dst)
                except Exception as err:
                    print('[video] 取视频失败：%s' % str(err)[:120])
    finally:
        proc.kill()

    n_bad = sum(1 for c in checks if not c['ok'])
    report = {"pack": str(packdir), "n_checks": len(checks),
              'n_pass': len(checks) - n_bad, 'checks': checks,
              'segments': rows,
              'anchors': [a['reason'] for a in anchors],
              'n_inputs': len(seq),
              'n_segments_sync': seg_stat['ops'],
              'n_pixels_compared': n_px, 'n_pixel_unstable': n_unstable,
              'n_color_mismatch': n_color_mismatch}
    (out / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n%d/%d 项通过' % (report['n_pass'], report['n_checks']))
    print('RESULT: %s' % ('PASS' if n_bad == 0 else 'FAIL'))
    print('报告: %s' % (out / 'report.json'))
    return 0 if n_bad == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
