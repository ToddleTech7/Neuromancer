"""回放保真度自检：录到的那张图 vs 按 state 重新渲染出来的那张图。

为什么要有这个：只说"能回放"没有说服力。这里给可复现的数字，并且把**两种**
误差分开，避免把"回放不稳"和"记录/版本不一行"混成一个数：

  ncc        记录图 vs 回放图      —— 总体保真度
  self_ncc   回放图 vs 同一个 state 再回放一次 —— 回放自身的噪声上界
  field_ok   记录 state 里的位置/尺度/朝向/图层/坐标系，逐项在回放端对上了吗

有了 self_ncc，ncc 掉下来就能归因：self 高而 ncc 低 ⇒ 记录或数据版本的问题；
self 本身低 ⇒ 回放端（渲染/等待）不稳。只看 ncc 一个数永远分不清这两件事。

与 cave_replay_viewer 的分工：
  cave_replay_viewer     逐点恢复 + 写门禁 + 人看（manifest）
  verify_replay_fidelity 不问对错，只算"恢复后画面 ≈ 当时画面"，出表 + 对比页

产物：
  <pack>/derived/replay_frames/<shot>_live.png / _live2.png   回放的像素（可人眼复核）
  <out>（默认 <pack>/derived/replay_fidelity.json）           逐行数字
  <out 同目录>/replay_compare.html                            三图并排 + 逐字段表

如实边界：
- 公开栈没有历史分割快照，所以"同一 state 重放"用的是**当前**数据；
  相似度低可能是版本变了，不一定是回放坏了。
- 尺寸不一致（1280×800 vs 1400×900）直接判 skipped，不做缩放瞎比。
- 没有图的点没法验，如实计 skipped。

用法：
    python3 scripts/verify_replay_fidelity.py results/capture_demo/<ts>/session_pack/<sid>
    python3 scripts/verify_replay_fidelity.py <pack> --max-pairs 6 --headed
    python3 scripts/verify_replay_fidelity.py <pack> --no-self-check   # 省一半时间
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# 本地 demo 服务的输出收进管道按 UTF-8 读，所以让它按 UTF-8 写
# （中文 Windows 缺省是 cp936；只改这个子进程，不动整个进程的环境）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}

import cave_session_pack as csp          # noqa: E402
import replay_state as rsq               # noqa: E402

SERVER = HERE / 'cave_local_demo_server.py'
GPU_PATCH = HERE / 'cave_gpu_patch.js'

CMP_JS = r"""
async ([urlA, urlB, regions]) => {
  const load = (u) => new Promise((res, rej) => {
    const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = u;
  });
  let a, b;
  try { a = await load(urlA); b = await load(urlB); }
  catch (e) { return {err: 'img_load'}; }
  const N = 256;
  const ca = document.createElement('canvas'); ca.width = N; ca.height = N;
  const cb = document.createElement('canvas'); cb.width = N; cb.height = N;
  const xa = ca.getContext('2d'), xb = cb.getContext('2d');
  // regions 是**比例**框 [[x0,y0,x1,y1], ...]；不给就是整图（旧口径）。
  // 为什么要分块：4panel 布局里左下是 3D 视图，它的 mesh 是另一路请求、
  // 加载慢且没有就绪信号。整图比时，3D 面板这一块（占 1/4）会把“2D 已完全一致”
  // 报成 0.83——问题在 mesh，不在复刻。默认只比 2D 三块，3D 单列。
  const pieces = (regions && regions.length) ? regions : [[0, 0, 1, 1]];
  const sum = () => ({sa: 0, sb: 0, saa: 0, sbb: 0, sab: 0, n: 0});
  const accAll = sum(), chAll = [sum(), sum(), sum()];
  let nAll = 0, sumAbsAll = 0, colAAll = 0, colBAll = 0;
  const nccOf = (s) => {
    const ma = s.sa / s.n, mb = s.sb / s.n;
    const cov = s.sab / s.n - ma * mb;
    const vA = s.saa / s.n - ma * ma, vB = s.sbb / s.n - mb * mb;
    return (vA <= 0 || vB <= 0) ? null : cov / Math.sqrt(vA * vB);
  };
  const rgbOf = (ch) => {
    const xs = ch.map(nccOf).filter(x => x !== null);
    return xs.length ? (xs.reduce((x, y) => x + y, 0) / xs.length) : null;
  };
  const per = [];
  for (const r of pieces) {
    const sx = Math.round(r[0] * a.width), sy = Math.round(r[1] * a.height);
    const sw = Math.max(1, Math.round((r[2] - r[0]) * a.width));
    const sh = Math.max(1, Math.round((r[3] - r[1]) * a.height));
    xa.drawImage(a, sx, sy, sw, sh, 0, 0, N, N);
    xb.drawImage(b, sx, sy, sw, sh, 0, 0, N, N);
    const da = xa.getImageData(0, 0, N, N).data;
    const db = xb.getImageData(0, 0, N, N).data;
    const acc = sum(), ch = [sum(), sum(), sum()];
    let sumAbs = 0, colA = 0, colB = 0;
    for (let i = 0; i < da.length; i += 4) {
      const ra = da[i], ga = da[i + 1], ba = da[i + 2];
      const rb = db[i], gb = db[i + 1], bb = db[i + 2];
      const va = (ra + ga + ba) / 3, vb = (rb + gb + bb) / 3;
      sumAbs += Math.abs(va - vb);
      for (const [s, x, y] of [[acc, va, vb], [ch[0], ra, rb],
                               [ch[1], ga, gb], [ch[2], ba, bb]]) {
        s.n++; s.sa += x; s.sb += y; s.saa += x * x; s.sbb += y * y; s.sab += x * y;
      }
      const mxA = Math.max(ra, ga, ba), mnA = Math.min(ra, ga, ba);
      const mxB = Math.max(rb, gb, bb), mnB = Math.min(rb, gb, bb);
      if (mxA - mnA > 12) colA++;
      if (mxB - mnB > 12) colB++;
    }
    // 合并到全局累加器（按样本数加权）
    for (const [dst, src] of [[accAll, acc], [chAll[0], ch[0]],
                              [chAll[1], ch[1]], [chAll[2], ch[2]]]) {
      dst.n += src.n; dst.sa += src.sa; dst.sb += src.sb;
      dst.saa += src.saa; dst.sbb += src.sbb; dst.sab += src.sab;
    }
    nAll += acc.n; sumAbsAll += sumAbs; colAAll += colA; colBAll += colB;
    per.push({box: r, ncc: nccOf(acc), ncc_rgb: rgbOf(ch),
              colored_a: colA / acc.n, colored_b: colB / acc.n});
  }
  const rgb = chAll.map(k => nccOf(k)).filter(x => x !== null);
  return {
    mean_abs_diff: sumAbsAll / nAll,
    ncc: nccOf(accAll),
    ncc_rgb: rgb.length ? (rgb.reduce((x, y) => x + y, 0) / rgb.length) : null,
    colored_a: colAAll / nAll,
    colored_b: colBAll / nAll,
    mean_a: accAll.sa / nAll, mean_b: accAll.sb / nAll,
    n_regions: pieces.length,
    per_region: per,
  };
}
"""

READBACK_JS = r"""
() => new Promise((resolve) => {
  const cs = Array.from(document.querySelectorAll('canvas'))
    .filter(c => c.width > 100 && c.height > 100)
    .sort((a, b) => b.width * b.height - a.width * a.height);
  const c = cs[0];
  if (!c) { resolve(null); return; }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    try { resolve({url: c.toDataURL('image/png'), w: c.width, h: c.height}); }
    catch (e) { resolve({err: String(e)}); }
  }));
})
"""

POS_JS = r"""
() => { const v = window.viewer; if (!v) return null;
  try { const p = v.state.toJSON().position;
        return p ? [p[0], p[1], p[2]] : null; } catch (e) { return null; } }
"""

# “画面画完了吗”：彩色像素占比（分割叠加是彩色的）。
# 为什么要等：state 到位只说明参数设好了，瓦片/分割可能还没画出来；
# 这时候拍下来跟记录的图比，得到的是“拿未加载图当回放失败”的假低分。
PAINT_JS = r"""
() => new Promise((resolve) => {
  const cs = Array.from(document.querySelectorAll('canvas'))
    .filter(c => c.width > 100 && c.height > 100)
    .sort((a, b) => b.width * b.height - a.width * a.height);
  const c = cs[0];
  if (!c) { resolve(null); return; }
  requestAnimationFrame(() => requestAnimationFrame(() => {
    try {
      const t = document.createElement('canvas');
      t.width = 160; t.height = 100;
      const x = t.getContext('2d');
      x.drawImage(c, 0, 0, 160, 100);
      const d = x.getImageData(0, 0, 160, 100).data;
      let col = 0, n = 0;
      for (let i = 0; i < d.length; i += 4) {
        const mx = Math.max(d[i], d[i + 1], d[i + 2]);
        const mn = Math.min(d[i], d[i + 1], d[i + 2]);
        if (mx - mn > 12) col++;
        n++;
      }
      resolve({colored: col / n});
    } catch (e) { resolve({err: String(e)}); }
  }));
})
"""


# 读“当时那张图”的内容统计：只在需要时用（例如无窗口对比）。
# 对齐本身靠 CMP_JS 的逐像素 NCC 闭环，不靠这个阈值。
IMG_STATS_JS = r"""
(url) => new Promise((resolve) => {
  const i = new Image();
  i.onload = () => {
    try {
      const t = document.createElement('canvas');
      t.width = 160; t.height = 100;
      const x = t.getContext('2d');
      x.drawImage(i, 0, 0, 160, 100);
      const d = x.getImageData(0, 0, 160, 100).data;
      let col = 0, n = 0, sum = 0, sum2 = 0;
      for (let k = 0; k < d.length; k += 4) {
        const r = d[k], g = d[k + 1], b = d[k + 2];
        if (Math.max(r, g, b) - Math.min(r, g, b) > 12) col++;
        const v = (r + g + b) / 3; sum += v; sum2 += v * v; n++;
      }
      const mean = sum / n;
      resolve({colored: col / n, mean: mean,
               std: Math.sqrt(Math.max(0, sum2 / n - mean * mean))});
    } catch (e) { resolve({err: String(e)}); }
  };
  i.onerror = () => resolve({err: 'load'});
  i.src = url;
})
"""


def wait_paint(page, min_frac, timeout):
    """等到画面真的画出来，或超时。返回 (colored, 用时ms, 是否超时)。"""
    t0 = time.time()
    last = None
    while True:
        r = page.evaluate(PAINT_JS) or {}
        last = r.get('colored')
        if last is not None and last >= min_frac:
            return last, int((time.time() - t0) * 1000), False
        if time.time() - t0 >= timeout:
            return last, int((time.time() - t0) * 1000), True
        time.sleep(0.25)



def _write_data_url(url, path):
    """data:image/png;base64,... → 文件。返回 True/False，失败不抛。"""
    try:
        head, _, b64 = url.partition(',')
        if 'base64' not in head:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(b64))
        return True
    except Exception:
        return False


def collect_points(events, shots, pair_tol_ms=3000):
    """可恢复点 + “图 → 点”配对。实现只有一份（replay_state.collect_points），

    回放器与保真度自检共用，避免两处判据漂移。
    """
    return rsq.collect_points(events, shots, pair_tol_ms)


def load_pack_pairs(path):
    """→ (pack, pack_dir, pairs, n_points)。IO 部分：读包、解析 PNG 路径。"""
    pack = csp.load_session(path)
    pack_dir = Path(pack.get('pack_dir') or path)
    shots = pack.get('shots') or []
    points, specs, stats = collect_points(pack.get('events') or [], shots)

    pairs = []
    n_partial_paint = 0
    for sp in specs:
        s = sp['shot']
        # 采集端已判定“画面没画完”的帧（painted_partial）不是证据：
        # 拿它去比回放只能得到假低分，必须单列计数而不是悄悄参与。
        if s.get('capture_status') == 'painted_partial':
            n_partial_paint += 1
            continue
        png = pack_dir / 'client' / 'shots' / Path(s['image_ref']).name
        if not png.exists():
            continue
        pt = sp['point']
        pairs.append({'state_id': pt.get('state_id'), 'checkpoint': pt,
                      'shot': s, 'pair_match': sp['pair_match'],
                      'points_source': pt.get('source'), 'png': png})
    stats['n_partial_paint'] = n_partial_paint
    pack['_stats'] = stats
    pack['_skipped_partial'] = stats['n_partial']
    return pack, pack_dir, pairs, len(points)


def compare_html(rows, out_path, summary, note):
    """三图并排（当时 / 回放 #1 / 回放 #2）+ 逐字段匹配表。

    图片路径在渲染时按“相对对比页”算，避免把绝对路径写进 HTML。
    """
    def rel(p):
        try:
            return os.path.relpath(p, out_path.parent)
        except Exception:
            return p

    def card(r):
        a = rel(r['png'])
        b = rel(r['live_png']) if r.get('live_png') else None
        c = rel(r['live2_png']) if r.get('live2_png') else None
        ncc = r.get('ncc')
        self_ncc = r.get('self_ncc')
        fields = r.get('state_fields') or {}
        chips = ''.join(
            f'<span class="chip {"ok" if v.get("ok") else "bad"}">'
            f'{k}</span>' for k, v in fields.items())
        return (
            '<figure>'
            f'<div class="imgs">'
            f'<img src="{a}"><div class="cap">当时</div>'
            + (f'<img src="{b}"><div class="cap">回放#1</div>' if b else '')
            + (f'<img src="{c}"><div class="cap">回放#2</div>' if c else '')
            + '</div>'
            f'<figcaption>state={r.get("state_id")} · {r.get("moment")} · '
            f'pair={r.get("pair_match")} · status={r.get("status")}<br>'
            f'<b>ncc={ncc}</b> · rgb_ncc={r.get("rgb_ncc")} · '
            f'彩色占比 {r.get("colored_rec")} / {r.get("colored_live")} '
            f'{"" if r.get("content_ok") is None else ("(一致)" if r.get("content_ok") else "<b style=color:#e88>(不一致)</b>")}'
            f' · self_ncc={self_ncc} · '
            f'mad={r.get("mean_abs_diff")} · '
            f'fields {"OK" if r.get("state_ok") else "FAIL: " + ",".join(r.get("state_failed") or [])}'
            f'<div class="chips">{chips}</div></figcaption></figure>')

    html = f"""<!doctype html><meta charset="utf-8">
<title>回放对比 · 记录 vs 重放</title>
<style>
body{{background:#111;color:#ddd;font:13px/1.6 Consolas,monospace;padding:20px}}
h2{{color:#7fd;font-size:15px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(640px,1fr));gap:18px}}
figure{{margin:0;background:#1b1b1b;border:1px solid #333;border-radius:6px;padding:8px}}
.imgs{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:6px}}
.imgs img{{width:100%;display:block;border-radius:4px;background:#000}}
.cap{{color:#8ad;font-size:11px;text-align:center}}
figcaption{{color:#9ab;font-size:11px;margin-top:6px}}
.chips{{margin-top:4px}}
.chip{{display:inline-block;padding:0 5px;margin:2px 3px 0 0;border-radius:3px;
  font-size:10px;border:1px solid #333}}
.chip.ok{{color:#6d6;border-color:#264}}
.chip.bad{{color:#e88;border-color:#522}}
.note{{color:#777;font-size:11px;margin-top:16px;white-space:pre-wrap}}
</style>
<h2>回放对比 · 当时画面 vs 按 state 重放出来的画面（{len(rows)} 组）</h2>
<div class="g">{''.join(card(r) for r in rows)}</div>
<div class="note">平均 NCC {summary.get('mean_ncc')} · 最低 {summary.get('min_ncc')} ·
逐通道 NCC 均值 {summary.get('mean_rgb_ncc')} ·
回放自一致（噪声上界）最低 {summary.get('min_self_ncc')} ·
字段全对 {summary.get('n_state_ok')}/{summary.get('n_pairs')} ·
内容一致 {summary.get('n_content_ok')}/{summary.get('n_content_checked')}
{note}</div>
"""
    out_path.write_text(html, encoding='utf-8')
    return out_path


def main(argv=None):
    ap = argparse.ArgumentParser(description='回放保真度自检')
    ap.add_argument('session', help='会话包目录')
    ap.add_argument('--max-pairs', type=int, default=8)
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--out', default=None)
    ap.add_argument('--html', default=None,
                    help='对比页路径（默认与 --out 同目录的 replay_compare.html）')
    ap.add_argument('--frames-dir', default=None,
                    help='回放帧落盘目录（默认 <pack>/derived/replay_frames）')
    ap.add_argument('--settle', type=float, default=6.0,
                    help='等画面画出来的最长时间（秒，等不够就如实记 paint_timeout）')
    ap.add_argument('--min-colored', type=float, default=None,
                    help='“画完了”的判据下限；缺省读包里的 keyframe_paint.colored_min，'
                         '再缺省才用 0.05（两边必须用同一份规则）')
    ap.add_argument('--accept-ncc', type=float, default=0.98,
                    help='闭环对齐的收敷阈值：连续读到 NCC ≥ 本值就停止等待')
    ap.add_argument('--no-self-check', action='store_true',
                    help='不做"同一 state 再回放一次"的自一致对比（省时间）')
    args = ap.parse_args(argv)

    pack, pack_dir, pairs, n_points = load_pack_pairs(args.session)
    stats = pack.get('_stats') or {}
    # 两边用同一份规则：优先读**包里声明的** keyframe_paint，而不是命令行的默认值。
    cfg = (pack.get('manifest') or {}).get('config') or {}
    rule = cfg.get('keyframe_paint') or {}
    min_colored = (args.min_colored if args.min_colored is not None
                   else float(rule.get('colored_min') or 0.05))
    print(f'[pack] {pack_dir}')
    print(f'[规则] colored_min={min_colored}（来源 '
          f'{"命令行" if args.min_colored is not None else "包内 keyframe_paint/默认"}）')
    print(f"[可恢复点] {n_points} 个（流内 state {stats.get('n_from_state', 0)} + "
          f"快门自带 {stats.get('n_from_shot_state', 0)}），"
          f"其中有图可比 {len(pairs)} 个"
          f"（跳过残缺 state {stats.get('n_partial', 0)} 个）")
    pm = stats.get('pair_match') or {}
    print(f"[配对] shot_state {pm.get('shot_state', 0)} · "
          f"state_id {pm.get('state_id', 0)} · "
          f"nearest_time {pm.get('nearest_time', 0)}")
    if stats.get('n_partial_paint'):
        print(f"[skip] {stats['n_partial_paint']} 张图采集时就判为未画完"
              '（painted_partial）：不当证据，不参与比对')
    if not pairs:
        print('[skip] 没有"图 + 可恢复点"的配对。')
        print('       需要：full_state_on_change（默认开）或 checkpoint 带 state，'
              '并且有 keyframe 图（?cave_kf=1）。')
        return 2

    pairs = pairs[:args.max_pairs]
    w = pairs[0]['shot'].get('w') or 1400
    h = pairs[0]['shot'].get('h') or 900
    frames_dir = (Path(args.frames_dir) if args.frames_dir
                  else pack_dir / 'derived' / 'replay_frames')

    out = Path(args.out) if args.out else (pack_dir / 'derived'
                                          / 'replay_fidelity.json')
    html_path = (Path(args.html) if args.html
                 else out.with_name('replay_compare.html'))

    import browser_util
    from playwright.sync_api import sync_playwright

    proc = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', bufsize=1, cwd=str(ROOT),
        env=CHILD_ENV)
    url = None
    t0 = time.time()
    while time.time() - t0 < 40:
        line = proc.stdout.readline()
        if line == '' and proc.poll() is not None:
            break
        m = re.match(r'VIEWER_URL=(\S+)', line.strip())
        if m:
            url = m.group(1)
            break
    if not url:
        proc.kill()
        raise SystemExit('未取到 VIEWER_URL')

    def send(state):
        proc.stdin.write('state ' + json.dumps(state, ensure_ascii=False) + '\n')
        proc.stdin.flush()

    rows = []
    try:
        with sync_playwright() as p:
            b = browser_util.launch_browser(p, headless=not args.headed)
            page = b.new_page(viewport={'width': int(w), 'height': int(h)})
            page.on('pageerror', lambda e: None)
            page.add_init_script(path=str(GPU_PATCH))   # 必须 document-start
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_function('() => !!window.viewer', timeout=30000)

            for i, pr in enumerate(pairs, 1):
                cp, shot = pr['checkpoint'], pr['shot']
                raw = (cp['state'] or {}).get('position') or []
                want = ([float(x) for x in raw] if len(raw) == 3 and
                        all(isinstance(x, (int, float)) for x in raw) else None)
                send(cp['state'])
                applied = False
                deadline = time.time() + 15
                while time.time() < deadline:
                    got = page.evaluate(POS_JS)
                    if want and got and all(
                            isinstance(a, (int, float)) and abs(a - bb) < 1.0
                            for a, bb in zip(got, want)):
                        applied = True
                        break
                    time.sleep(0.4)
                # 状态到位 ≠ 瓦片画完。这里**不用“等到某个彩色占比”的阈值**：
                # 那个数在对侧渲染里本来就不一样（实测同一画面 0.714 vs 0.662），
                # 拍一个数只能把一种情况调对、另一种调错。改用闭环：
                # 读一帧 → 跟当时那张图比一次 → 不一致就再等再读，直到比到一致
                # 或预算用完；并记下第几次才一致，让“对齐要多久”可查。
                rec_url = ('data:image/png;base64,'
                           + base64.b64encode(pr['png'].read_bytes()).decode())
                paint, paint_ms, paint_timeout = (None, 0, False)
                if applied:
                    paint, paint_ms, paint_timeout = wait_paint(
                        page, min_colored, args.settle)
                    p0 = page.evaluate(PAINT_JS) or {}
                    paint = p0.get('colored', paint)

                best, attempts, live = None, 0, None
                converged = False
                t_settle = time.time()
                if applied:
                    while True:
                        live = page.evaluate(READBACK_JS)
                        if not (live and live.get('url')):
                            break
                        c0 = page.evaluate(CMP_JS, [rec_url, live['url']])
                        n0 = c0.get('ncc')
                        attempts += 1
                        if n0 is not None and (best is None or n0 > best[0]):
                            best = (n0, c0, live,
                                    int((time.time() - t_settle) * 1000))
                        if n0 is not None and n0 >= args.accept_ncc:
                            converged = True
                            break
                        if time.time() - t_settle >= args.settle:
                            break
                        time.sleep(0.5)
                settle_ms = int((time.time() - t_settle) * 1000)
                if best:
                    live = best[2]
                live_fp = page.evaluate(rsq.LIVE_FP_JS)

                base = Path(shot.get('shot_id') or f'shot_{i}').stem
                live_png = f'{base}_live.png'
                live2_png = f'{base}_live2.png'
                has_live = bool(live and live.get('url')
                                and _write_data_url(live['url'],
                                                    frames_dir / live_png))

                live2 = None
                if not args.no_self_check:
                    # 同一个 state 再放一次：两次回放之间的差异 = 回放噪声上界
                    send(cp['state'])
                    time.sleep(max(0.6, args.settle * 0.5))
                    live2 = page.evaluate(READBACK_JS)
                    _write_data_url(live2['url'], frames_dir / live2_png) \
                        if live2 and live2.get('url') else False

                expected = rsq.fingerprint(cp['state'], cp.get('viewpoint'))
                got_fp = rsq.fingerprint(state=live_fp) if live_fp else None
                cmp_fp = rsq.compare(expected, got_fp) if got_fp else {
                    'ok': False, 'failed': ['<no_live_state>'], 'fields': {}}

                row = {
                    'state_id': pr['state_id'], 'moment': shot.get('moment'),
                    'pair_match': pr.get('pair_match'),
                    'point_source': pr.get('points_source'),
                    't_wall': shot.get('t_wall'),
                    'applied': applied,
                    'size_rec': [shot.get('w'), shot.get('h')],
                    'size_live': [live.get('w'), live.get('h')] if live else None,
                    'png': str(pr['png']),
                    'live_png': (str(frames_dir / live_png) if has_live
                                 else None),
                    'live2_png': (str(frames_dir / live2_png)
                                  if live2 and live2.get('url') else None),
                    'state_ok': bool(cmp_fp.get('ok')),
                    'state_failed': cmp_fp.get('failed') or [],
                    'paint_live': paint,
                    'settle_attempts': attempts,
                    'settle_converged': converged,
                    'settle_ms': settle_ms,
                    # 采集时画面是否静止：拖动中的帧与快门时 state 差一帧运动，
                    # 那点差异是记录语义造成的，不是回放错——必须能区分开。
                    'capture_stable': shot.get('paint_stable'),
                    'paint_waited_ms': paint_ms,
                    'paint_timeout': paint_timeout,
                    'state_fields': {k: {'ok': bool(v.get('ok'))}
                                     for k, v in
                                     (cmp_fp.get('fields') or {}).items()},
                }
                if not has_live:
                    row.update({'status': 'no_readback', 'ncc': None})
                elif (shot.get('w') and live.get('w')
                      and (shot['w'] != live['w'] or shot['h'] != live['h'])):
                    row.update({'status': 'size_mismatch', 'ncc': None})
                else:
                    pending = bool(applied and paint_timeout
                                   and (paint or 0) < min_colored)
                    cmp = best[1] if best else {}
                    ncc = cmp.get('ncc')
                    rgb = cmp.get('ncc_rgb')
                    cr, cl = cmp.get('colored_a'), cmp.get('colored_b')
                    row.update({
                        'ncc': None if ncc is None else round(ncc, 4),
                        'rgb_ncc': None if rgb is None else round(rgb, 4),
                        'colored_rec': (None if cr is None
                                        else round(cr, 3)),
                        'colored_live': (None if cl is None
                                         else round(cl, 3)),
                        # 分割叠加在不在：两边“有颜色像素占比”差得太多，
                        # 说明看的不是同一类画面（典型：录的那帧还没画完）
                        'content_ok': (None if cr is None or cl is None
                                       else abs(cr - cl) <= 0.15),
                        'color_mismatch': bool(
                            cr is not None and cl is not None
                            and abs(cr - cl) <= 0.15
                            and ncc is not None and ncc >= 0.90
                            and rgb is not None and rgb < 0.90),
                        'bytes_rec': pr['png'].stat().st_size,
                        'mean_abs_diff': (None if cmp.get('mean_abs_diff') is None
                                          else round(cmp['mean_abs_diff'], 2)),
                        'status': ('pending_tiles' if (not applied or pending)
                                   else ('compared' if converged
                                         else ('motion_lag'
                                               if shot.get('paint_stable')
                                               is False
                                               else 'not_converged'))),
                    })
                    if pending:
                        # 冷启动时分割层还没画出来：不能拿“EM 灰图 vs 彩色图”
                        # 当内容失败，也不能当通过。单列，等下次可比的帧。
                        row.update({'ncc': None, 'rgb_ncc': None,
                                    'content_ok': None,
                                    'settle_converged': False,
                                    'pending_reason': 'paint_timeout'})
                    if has_live:
                        try:
                            row['bytes_live'] = (frames_dir / live_png).stat().st_size
                        except OSError:
                            pass
                    if (not pending and live2 and live2.get('url')
                            and has_live):
                        sc = page.evaluate(CMP_JS, [live['url'], live2['url']])
                        s_ncc = sc.get('ncc')
                        row['self_ncc'] = (None if s_ncc is None
                                           else round(s_ncc, 4))
                rows.append(row)
                print(f'  [{i}/{len(pairs)}] state={row["state_id"]} '
                      f'applied={applied} ncc={row["ncc"]} '
                      f'rgb={row.get("rgb_ncc")} '
                      f'color={row.get("colored_rec")}/{row.get("colored_live")} '
                      f'self_ncc={row.get("self_ncc")} '
                      f'fields={"OK" if row["state_ok"] else row["state_failed"]} '
                      f'{row["status"]}')
            b.close()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    def _isnum(v):
        # 注意：JS 的 1.0 传回 Python 可能是 int。像素级完全一致就是 1.0，
        # 只判 float 会把这个最好的结果从统计里漏掉。
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    ok = [r for r in rows if _isnum(r.get('ncc'))]
    self_ok = [r['self_ncc'] for r in rows
               if _isnum(r.get('self_ncc'))]
    rgb_ok = [r['rgb_ncc'] for r in rows if _isnum(r.get('rgb_ncc'))]
    content_rows = [r for r in rows if r.get('content_ok') is not None]
    css = rsq.summarize([{'ok': r['state_ok'],
                          'failed': r['state_failed'],
                          'fields': r['state_fields']} for r in rows])
    summary = {
        'n_pairs': len(rows),
        'n_compared': len(ok),
        'mean_ncc': round(sum(r['ncc'] for r in ok) / len(ok), 4) if ok else None,
        'min_ncc': round(min(r['ncc'] for r in ok), 4) if ok else None,
        'n_high': sum(1 for r in ok if r['ncc'] >= 0.9),
        'n_medium': sum(1 for r in ok if 0.7 <= r['ncc'] < 0.9),
        'n_low': sum(1 for r in ok if r['ncc'] < 0.7),
        'n_self_compared': len(self_ok),
        'mean_self_ncc': (round(sum(self_ok) / len(self_ok), 4)
                          if self_ok else None),
        'min_self_ncc': round(min(self_ok), 4) if self_ok else None,
        'mean_rgb_ncc': (round(sum(rgb_ok) / len(rgb_ok), 4)
                         if rgb_ok else None),
        'min_rgb_ncc': round(min(rgb_ok), 4) if rgb_ok else None,
        'n_content_ok': sum(1 for r in content_rows if r.get('content_ok')),
        'n_content_checked': len(content_rows),
        'content_mismatch': [r['png'].split('/')[-1] for r in content_rows
                             if not r.get('content_ok')],
        # 闭环对齐：几次读到一致、最慢一次用了多久（可比“拍一个固定等待”的）
        'n_settle_converged': sum(1 for r in rows if r.get('settle_converged')),
        'n_motion_lag': sum(1 for r in rows if r.get('status') == 'motion_lag'),
        'n_color_mismatch': sum(1 for r in rows if r.get('color_mismatch')),
        'color_mismatch': [r['png'].split('/')[-1] for r in rows
                           if r.get('color_mismatch')],
        'n_pending_tiles': sum(1 for r in rows
                               if r.get('status') == 'pending_tiles'),
        'pending_tiles': [r['png'].split('/')[-1] for r in rows
                          if r.get('status') == 'pending_tiles'],
        'n_not_converged': sum(1 for r in rows
                               if r.get('status') == 'not_converged'),
        'mean_settle_attempts': (round(sum(r.get('settle_attempts') or 0
                                          for r in rows) / len(rows), 2)
                                 if rows else None),
        'max_settle_ms': (max((r.get('settle_ms') or 0) for r in rows)
                          if rows else None),
        'n_state_ok': css['n_ok'],
        'per_field_ok': css['per_field_ok'],
        'state_failed_fields': css['failed_fields'],
        'paint_rule': {'colored_min': min_colored,
                       'accept_ncc': args.accept_ncc,
                       'source': ('cli' if args.min_colored is not None
                                  else 'pack_or_default')},
        'pairs_by_match': pm,
        'n_skipped_partial_paint': stats.get('n_partial_paint', 0),
        'points': {'n_total': n_points,
                   'n_from_state': stats.get('n_from_state', 0),
                   'n_from_shot_state': stats.get('n_from_shot_state', 0)},
        'frames_dir': str(frames_dir),
        'note': '公开栈无历史分割快照：低相似也可能是数据版本变了；'
                'self_ncc 是回放自身噪声上界，用来分开这两种原因',
    }
    report = {'pack_dir': str(pack_dir), 'rows': rows, 'summary': summary}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding='utf-8')

    try:
        compare_html(rows, html_path, summary, '')
        wrote_html = True
    except Exception as err:
        wrote_html = False
        print('[warn] 对比页写失败:', type(err).__name__, err)

    print()
    print(f"对比 {summary['n_compared']}/{summary['n_pairs']} 张 · "
          f"平均 NCC {summary['mean_ncc']} · 最低 {summary['min_ncc']}")
    print(f"高(≥0.9) {summary['n_high']} / 中(0.7–0.9) {summary['n_medium']} / "
          f"低(<0.7) {summary['n_low']}")
    print(f"逐通道 NCC 平均 {summary['mean_rgb_ncc']} · "
          f"最低 {summary['min_rgb_ncc']}")
    print(f"内容一致（彩色占比差≤0.15）"
          f"{summary['n_content_ok']}/{summary['n_content_checked']}"
          + (f" · 不一致 {summary['content_mismatch']}"
             if summary['content_mismatch'] else ''))
    print(f"回放自一致 {summary['n_self_compared']} 张 · "
          f"平均 {summary['mean_self_ncc']} · 最低 {summary['min_self_ncc']}")
    print(f"字段全对 {summary['n_state_ok']}/{summary['n_pairs']} · "
          f"失败字段 {summary['state_failed_fields'] or '无'}")
    print(f"pending_tiles {summary['n_pending_tiles']}（冷启动分割层未加载，已单列）")
    print('报告:', csp.relpath(out))
    print('回放帧:', frames_dir)
    print('对比页:', html_path if wrote_html else '（未生成）')
    # 不设"必须高"的硬门槛：公开栈无历史快照，相似度低本身就是要报出来的事实。
    return 0 if summary['n_compared'] else 1


if __name__ == '__main__':
    sys.exit(main())
