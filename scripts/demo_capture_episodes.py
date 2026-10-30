"""公开数据采集 demo：真浏览器 → 真录制 → 离线派生 episode。

与 demo_full_pipeline 的分工：
  demo_full_pipeline  = 提交链路（op 锚点 → join → 样本）
  demo_capture_episodes = 采集与离线清洗（输入 → 手势 → episode → no-op）

做什么：
  1. 起本地 Neuroglancer，加载 MICrONS minnie65 真实 EM + 分割（公开数据）；
  2. 用 Playwright 模拟一段**自由浏览**的校对：拖动、缩放、点选、跳到别处、
     回看、写备注、中间留几段静默；
  3. recorder v0.7 全程录制（事件名对齐 RF01–RF15，手势已聚合）；
  4. 落目录包 → 离线派生 PP06/PP07/PP10 → episodes.jsonl + 甘特图 HTML；
  5. 断言并打印摘要。

如实边界：
  - 操作是**脚本模拟**的，不是真人；能验证“链路与字段”，不能当训练数据。
  - 无提交 ⇒ 所有 episode `no_edit=true`、`disposition=unknown`（这是正确的：
    没提交不等于“检查过没问题”，Q08/PP10）。
  - 派生策略用 demo 专用阈值（真实用默认 30s/50µm）。

用法：
    python3 scripts/demo_capture_episodes.py
    python3 scripts/demo_capture_episodes.py --headful
"""

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = HERE / 'cave_local_demo_server.py'
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')
# R2 前置：必须 document-start 注入，等 ng 建好 WebGL 上下文就改不动了
GPU_PATCH = HERE / 'cave_gpu_patch.js'

sys.path.insert(0, str(HERE))
import browser_util                      # noqa: E402
import cave_session_pack as csp          # noqa: E402
import cave_episodes as cep              # noqa: E402

OUT = ROOT / 'results' / 'capture_demo'
TS = datetime.now().strftime('%Y%m%d_%H%M%S')
DIMS = (4, 4, 40)

# “画面画完了吗”：数一下有颜色的像素占比（分割叠加是彩色的）。
# 作用：关键帧必须在**画面已经画出来之后**再取，否则录到的是一帧还没加载
# 完的灰图——它和回放帧的灰度 NCC 能到 0.96，看起来“几乎一样”，实际内容不同。
COLOR_JS = r"""
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


def wait_painted(page, tag, min_frac=0.10, timeout=15.0):
    """等画面真的画出来（彩色像素占比达标）；超时如实记录，不假装已画完。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = page.evaluate(COLOR_JS) or {}
        last = r.get('colored')
        if last is not None and last >= min_frac:
            print(f'[paint] {tag} 彩色占比 {last:.2f}（{time.time() - t0:.1f}s）')
            return last
        time.sleep(0.25)
    print(f'[paint] {tag} 超时：彩色占比 {last}（照实继续，不假装已画完）')
    return last

EM_SOURCE = ('precomputed://https://storage.googleapis.com/'
             'iarpa_microns/minnie/minnie65/em')
SEG_SOURCE = ('precomputed://https://storage.googleapis.com/'
              'iarpa_microns/minnie/minnie65/seg')

A = [190587, 114102, 21773]                 # voxel（dims 4/4/40）
B = [196587, 114102, 21773]                 # x +6000 voxel = 24000 nm 跳变
JUMPS_NM = (B[0] - A[0]) * DIMS[0]

# demo 专用策略：真实默认是 30s / 50µm；这里缩短以便一次跑完
DEMO_POLICY = {'gap_ms': 8000, 'jump_nm': 20000, 'idle_ms': 1500,
               # 残片合并不狠一点，回看时会切出一堆 1s 的碎段
               'min_events': 8,
               'version': 'demo.v1.spatial-temporal'}


def gantt_html(result, path):
    eps = result['episodes']
    if not eps:
        return
    t0 = min(e['t_start_ms'] for e in eps)
    span = max(1, max(e['t_end_ms'] for e in eps) - t0)
    rows = []
    for e in eps:
        left = (e['t_start_ms'] - t0) / span * 100
        width = max(0.6, e['duration_ms'] / span * 100)
        idle = ' '.join(
            f'<i style="left:{(s["start_ms"]-e["t_start_ms"])/max(1,e["duration_ms"])*100:.1f}%;'
            f'width:{s["duration_ms"]/max(1,e["duration_ms"])*100:.1f}%"></i>'
            for s in e['idle_segments'])
        rows.append(
            f'<div class="row"><span class="lbl">{e["episode_id"]}</span>'
            f'<div class="bar" style="left:{left:.1f}%;width:{width:.1f}%">'
            f'{idle}</div>'
            f'<span class="meta">{e["duration_ms"]/1000:.1f}s · '
            f'ev {e["n_events"]} · 手势 {e["gestures"].get("n", 0)} · '
            f'no_edit {e["no_edit"]} · {e["disposition"]}</span></div>')
    html = f"""<!doctype html><meta charset="utf-8">
<title>采集 demo · episode 时间轴</title>
<style>
body{{background:#111;color:#ddd;font:13px/1.6 Consolas,monospace;padding:20px}}
h2{{color:#7fd;font-size:15px}}
.row{{position:relative;height:34px;margin:4px 0;display:flex;align-items:center}}
.lbl{{width:64px;color:#8ad}}
.bar{{position:absolute;left:64px;height:20px;background:#2a5;border-radius:3px;
      opacity:.85;top:7px}}
.bar i{{position:absolute;top:0;height:20px;background:#e6a23c;opacity:.55;
        border-radius:2px}}
.meta{{margin-left:auto;color:#888;font-size:11px}}
.note{{color:#666;font-size:11px;margin-top:14px;white-space:pre-wrap}}
</style>
<h2>实例采集 demo · episode 时间轴（橙=静默段，不代表在思考）</h2>
{''.join(rows)}
<div class="note">policy {result['policy_version']} · 事件 {result['n_events']} ·
seq 断档 {result['normalized']['seq_gaps'] or '无'} ·
no_edit=true 只表示“没提交编辑”，disposition=unknown 表示“没有显式答复”，二者不可互推。</div>
"""
    Path(path).write_text(html, encoding='utf-8')


def r2_html(pack_dir, result, path):
    """R2：把录到的关键帧按时间轴排开，标出它属于哪个 episode。

    只展示**当时真存在的东西**：每张图配 t_wall / state_id / 所在 episode。
    没有任何关键帧的时段如实标“无图像证据”，不用别的时刻的图凑。
    """
    shots = []
    sh = Path(pack_dir) / 'client' / 'shots' / 'shots.jsonl'
    if sh.exists():
        for line in sh.read_text(encoding='utf-8').splitlines():
            if line.strip():
                shots.append(json.loads(line))
    cards = []
    for s in shots:
        t = s.get('t_wall') or 0
        ep = None
        for e in result['episodes']:
            if e['t_start_ms'] <= t <= e['t_end_ms']:
                ep = e
                break
        rel = s.get('image_ref')
        img = (f'<img src="session_pack/{Path(pack_dir).name}/client/shots/'
               f'{Path(rel).name}">') if rel else '<div class="noimg">无图像证据</div>'
        cards.append(
            f'<figure>{img}<figcaption>'
            f'{s.get("shot_id")} · {s.get("moment")} · '
            f'state={s.get("state_id")} · '
            f'ep={ep["episode_id"] if ep else "-"} · '
            f'{s.get("capture_status")} ({s.get("readback_via") or "-"})'
            f'</figcaption></figure>')
    html = f"""<!doctype html><meta charset="utf-8">
<title>R2 关键帧回放</title>
<style>
body{{background:#111;color:#eee;font:13px/1.6 Consolas,monospace;padding:20px}}
h2{{color:#7fd;font-size:15px;font-weight:600}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:16px}}
figure{{margin:0;background:#1b1b1b;border:1px solid #333;border-radius:6px;padding:8px}}
img{{width:100%;display:block;border-radius:4px}}
figcaption{{color:#8ad;font-size:11px;margin-top:6px}}
.noimg{{height:160px;display:flex;align-items:center;justify-content:center;
  color:#a55;border:1px dashed #533;border-radius:4px}}
.note{{color:#777;font-size:11px;margin-top:16px;white-space:pre-wrap}}
</style>
<h2>R2 视觉回放 · 关键帧（{len(shots)} 张）</h2>
<div class="g">{''.join(cards) or '<p>本次会话没有关键帧。</p>'}</div>
<div class="note">图像来自页面内 WebGL 帧缓冲（cave_gpu_patch 保住后 readback），
不是宿主机截屏。seq 断档 {result['normalized']['seq_gaps'] or '无'} ·
policy {result['policy_version']}。公开栈无历史分割快照，分割归属以当时加载版本为准。
</div>
"""
    Path(path).write_text(html, encoding='utf-8')


def index_html(out, items, path):
    """产物索引：一次 demo 的四个页面放在一个入口里，不用记路径。"""
    rows = ''.join(
        f'<li><a href="{name}">{title}</a><span>{desc}</span></li>'
        for name, title, desc in items)
    Path(path).write_text(f"""<!doctype html><meta charset="utf-8">
<title>采集 demo 产物</title>
<style>
body{{background:#111;color:#ddd;font:14px/1.7 Consolas,monospace;padding:24px}}
h2{{color:#7fd;font-size:16px}}
a{{color:#8cf}}
li{{margin:8px 0}}
span{{color:#777;margin-left:10px;font-size:12px}}
.note{{color:#777;font-size:12px;margin-top:18px;white-space:pre-wrap}}
</style>
<h2>采集 demo 产物（{Path(out).name}）</h2><ul>{rows}</ul>
<div class="note">操作是脚本模拟的（not_for_training）；所有页面只展示会话包里
确实录到的内容，缺失处不补画。</div>
""", encoding='utf-8')


def main():
    ap = argparse.ArgumentParser(description='公开数据采集 demo（episode 派生）')
    ap.add_argument('--headful', action='store_true')
    # D10：粒度可配。0 = move 全量（回放最保真，事件量大约 ×7）
    ap.add_argument('--move-ms', type=int, default=50,
                    help='move 采样间隔 ms；0 = 全量（默认 50）')
    # 默认跑回放保真度：每次改动都能立刻看出“回放是不是变差了”
    ap.add_argument('--no-fidelity', action='store_true',
                    help='跳过回放保真度自检（默认会跑）')
    args = ap.parse_args()

    out = OUT / TS
    out.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, cond, detail=''):
        checks.append({'name': name, 'ok': bool(cond),
                       'detail': str(detail)[:200]})
        print(f'  [{"PASS" if cond else "FAIL"}] {name}'
              + (f' — {detail}' if detail else ''))

    proc = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        errors='replace', bufsize=1, cwd=str(ROOT))

    def send(cmd):
        proc.stdin.write(cmd + '\n')
        proc.stdin.flush()

    url = None
    deadline = time.time() + 40
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line == '':
            if proc.poll() is not None:
                break
            continue
        m = re.match(r'VIEWER_URL=(\S+)', line.strip())
        if m:
            url = m.group(1)
            break
    if not url:
        proc.kill()
        raise SystemExit('未能取到 VIEWER_URL')
    print('[server]', url)

    pack = None
    try:
        with sync_playwright() as p:
            b = browser_util.launch_browser(p, headless=not args.headful)
            page = b.new_page(viewport={'width': 1400, 'height': 900})
            page.on('pageerror', lambda e: print('[pageerror]', str(e)[:160]))
            # 必须在 goto 之前：保住帧缓冲，否则关键帧只能拍到全透明
            page.add_init_script(path=str(GPU_PATCH))
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_function('() => !!window.viewer', timeout=30000)

            send('dims %d %d %d' % DIMS)
            time.sleep(0.4)
            send('img em ' + EM_SOURCE)
            time.sleep(0.4)
            send('seg autoseg ' + SEG_SOURCE)
            time.sleep(0.4)
            send('cross 6')
            send('pos %d %d %d' % tuple(A))
            time.sleep(2.0)
            # 分割层是懒加载的：先等它真画出来，再开始录（否则 session 开头那张
            # 关键帧会是一帧灰图，回放看起来“很像”，实际内容不同）
            wait_painted(page, 'session_start')

            page.add_script_tag(content=RECORDER)
            page.wait_for_function(
                '() => window.__caveRecorder && window.__caveRecorder.loaded',
                timeout=10000)
            # 先把粒度定下来再开始，session.start 就会带上它
            page.evaluate(
                '(n) => window.__caveRecorder.setConfig({move_sample_ms: n})',
                args.move_ms)
            # checkpoint 也抓一张图：这样回放保真度可以逐个 checkpoint 对图
            page.evaluate(
                '() => window.__caveRecorder.setConfig('
                '{keyframe_on_checkpoint: true, dwell_ms: 0})')
            sid = page.evaluate('() => window.__caveRecorder.start()')
            print('[session]', sid, '· move 采样',
                  '全量' if args.move_ms <= 0 else f'{args.move_ms}ms')
            time.sleep(0.8)

            # 脚本自己知道自己什么时候进入下一段（= 已知真值，不需要人工勾）
            marks = []

            def mark(name):
                marks.append({'name': name,
                              't_wall': page.evaluate('() => Date.now()')})
                # 在这一刻钉一个 checkpoint：回放保真度才有对照物
                page.evaluate('(r) => window.__caveRecorder.checkpointNow(r)',
                              name)
                print(f'[mark] {name}')

            # ---------- ep1：在 A 附近看、拖、缩放、点选 ----------
            print('[ep1] 拖动 / 缩放 / 点选')
            cx, cy = 700, 450
            page.mouse.move(cx, cy)
            page.mouse.down()
            for i in range(1, 11):                 # 10 步拖动，产生真实 pointermove
                page.mouse.move(cx + i * 22, cy + i * 6)
                time.sleep(0.05)
            page.mouse.up()
            time.sleep(0.3)
            for _ in range(3):
                page.mouse.wheel(0, -120)
                time.sleep(0.12)
            time.sleep(0.6)
            page.mouse.click(cx + 40, cy + 20)
            time.sleep(0.4)
            # RF13 关键帧：点击引发重绘，取的是“人当时看到的那一帧”
            wait_painted(page, 'ep1')
            kf1 = page.evaluate(
                "() => window.__caveRecorder.keyframe('demo_ep1')")
            print('[ep1] 关键帧', kf1)
            print('[ep1] 静默 3.2s（应被记为 idle 段）')
            time.sleep(3.2)

            # ---------- ep2：跳到远处（空间跳变触发切分） ----------
            mark('ep2_start')
            print('[ep2] 跳到 x+24000nm（jump %.0f nm）' % JUMPS_NM)
            send('pos %d %d %d' % tuple(B))
            time.sleep(1.2)
            page.mouse.move(600, 400)
            page.mouse.down()
            for i in range(1, 9):
                page.mouse.move(600 - i * 18, 400 + i * 5)
                time.sleep(0.05)
            page.mouse.up()
            time.sleep(0.3)
            page.mouse.click(620, 430)
            time.sleep(0.5)
            wait_painted(page, 'ep2')
            kf2 = page.evaluate(
                "() => window.__caveRecorder.keyframe('demo_ep2')")
            print('[ep2] 关键帧', kf2)
            print('[ep2] 静默 2.0s')
            time.sleep(2.0)

            # ---------- ep3：停 12s 后回看 A，写备注（时间间隔触发切分） ----------
            mark('ep3_start')
            print('[ep3] 静默 12s（> gap_ms 8s，触发时间切分）')
            time.sleep(12.0)
            send('pos %d %d %d' % tuple(A))
            time.sleep(1.0)
            page.mouse.move(cx, cy)
            page.mouse.wheel(0, 120)
            time.sleep(0.5)
            page.evaluate(
                "() => window.__caveRecorder.emit('note', {"
                "text: 'demo：这段看起来是同一条轴突，先记下位置',"
                "target: {root_id: null, position_nm: null}})")

            page.evaluate('() => window.__caveRecorder.stop()')
            # 关键帧要落盘就必须带 dataURL——不带图的导出会让
            # “截图 ok=True 但 n_png=0”，那是假成功。
            pack = json.loads(page.evaluate(
                '() => window.__caveRecorder.exportWithImagesJSON()'))
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

    if not pack:
        raise SystemExit('未取得会话包')

    ev = pack.get('events') or []
    print(f'\n采集完成：{len(ev)} 事件')

    # ---------- 落目录包 ----------
    pack_dir = out / 'session_pack'
    d, manifest = csp.pack_session(
        pack, pack_dir,
        legs={'client_process': 'real'},
        reasons={'client_process': 'demo_capture_episodes：本地真前端 + 公开数据'},
        extra_manifest={'session_mode': 'scripted_demo',
                        'not_for_training': True,
                        'note': '操作由脚本模拟，仅验证链路与字段'})

    # ---------- 离线派生 ----------
    res = cep.derive_episodes(csp.load_session(str(d)), DEMO_POLICY)
    ep_path = cep.write_derived(res, Path(d) / 'derived')
    gantt_html(res, out / 'episodes.html')
    r2_html(d, res, out / 'replay_r2.html')

    # ---------- 时间线回放页：看到 → 做了什么（双击即开）----------
    timeline = out / 'timeline_replay.html'
    try:
        from cave_session_replay import replay_one as _replay_one
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _replay_one(str(d), auto_join=True, out_path=str(timeline))
    except Exception as err:
        print('[warn] 时间线回放页未生成:', type(err).__name__, err)

    norm = res['normalized']
    print(f"\n归一化: {norm['n_events']} 事件 / seq {norm['seq_first']}–"
          f"{norm['seq_last']} / 断档 {norm['seq_gaps'] or '无'} / "
          f"单调序 {'OK' if norm['monotonic_order_ok'] else '异常'}")
    print(f"episode: {res['n_episodes']} 个")
    for e in res['episodes']:
        idle = sum(s['duration_ms'] for s in e['idle_segments'])
        print(f"  {e['episode_id']} {e['duration_ms']/1000:6.1f}s "
              f"ev={e['n_events']:<3} 手势={e['gestures'].get('n', 0)} "
              f"idle={idle/1000:4.1f}s no_edit={e['no_edit']} "
              f"disp={e['disposition']} "
              f"触发={((e['trigger'] or {}).get('reason')) or '-'}")

    # ---------- 断言 ----------
    print()
    check('包健康：seq 无断档', not manifest['health']['seq_gaps'],
          manifest['health']['seq_gaps'])
    check('事件名已对齐（session.start 存在）',
          any(csp.ctype(e) == 'session.start' for e in ev))
    _cfg = ((ev[0].get('config') if ev else None)
            or (manifest.get('config') or {}))
    check('采样粒度已记录（D10）',
          _cfg.get('move_sample_ms') == args.move_ms, _cfg)
    check('派生结果带着输入粒度（不信默认值）',
          (res.get('sampling') or {}).get('move_sample_ms') == args.move_ms,
          res.get('sampling'))
    n_gest = sum(1 for e in ev if csp.ctype(e) == 'viewport.gesture')
    check('手势聚合产出 >= 2（RF02/PP02）', n_gest >= 2, n_gest)
    check('拖动被识别为 drag',
          any(csp.ctype(e) == 'viewport.gesture'
              and e.get('kind') == 'drag' for e in ev))
    check('缩放被识别为 zoom',
          any(csp.ctype(e) == 'viewport.gesture'
              and e.get('kind') == 'zoom' for e in ev))
    check('episode >= 2（切分生效）', res['n_episodes'] >= 2,
          res['n_episodes'])
    def _has_trigger(res, reason):
        """切分理由可能在 trigger，也可能在“被吞掉”的 extra_triggers 里。"""
        for e in res['episodes']:
            if (e.get('trigger') or {}).get('reason') == reason:
                return True
            if any((t or {}).get('reason') == reason
                   for t in e.get('extra_triggers') or []):
                return True
        return False

    check('有空间跳变触发切分', _has_trigger(res, 'spatial_jump'))
    check('有时间间隔触发切分', _has_trigger(res, 'time_gap'))

    # ---- 用“脚本已知真值”当准绳验切分（替代人工勾线）----
    # 注意边界：脚本的切点是刻意的、机械的，所以这里只能证明“算法能把我
    # 故意插入的切点找回来”，**不能当成真人边界准确率**。真人边界更模糊。
    BOUND_TOL = 10000
    bounds = [e['t_start_ms'] for e in res['episodes'][1:]]
    missed = [m['name'] for m in marks
              if not any(abs(m['t_wall'] - b) <= BOUND_TOL for b in bounds)]
    extra = [b for b in bounds
             if not any(abs(b - m['t_wall']) <= BOUND_TOL for m in marks)]
    check(f'脚本插入的切点全部找回（{len(marks) - len(missed)}/{len(marks)}）',
          not missed, missed)
    print(f'  额外切点 {len(extra)} 个（可能是真实跳变；不算误切，也不当正确）')
    check('记录了 idle 段（且不被当成“思考”）',
          any(e['idle_segments'] for e in res['episodes']),
          sum(len(e['idle_segments']) for e in res['episodes']))
    check('关键帧被归入对应 episode',
          sum(e['event_counts'].get('keyframe.capture', 0)
              for e in res['episodes']) >= 2)
    check('无提交 ⇒ 全部 no_edit=true',
          all(e['no_edit'] for e in res['episodes']))
    check('no_edit 不推出“检查过” ⇒ disposition 全 unknown',
          all(e['disposition'] == 'unknown' for e in res['episodes']),
          [e['disposition'] for e in res['episodes']])
    check('备注成了 evidence.bookmark',
          any(csp.ctype(e) == 'evidence.bookmark' for e in ev))

    # ---- RF13 关键帧：R2 视觉回放的唯一直接证据 ----
    kfs = [e for e in ev if csp.ctype(e) == 'keyframe.capture']
    check('关键帧事件 >= 2（RF13）', len(kfs) >= 2, len(kfs))
    n_cap = sum(1 for e in kfs if e.get('capture_status') == 'captured')
    check('关键帧真取到像素（capture_status=captured）', n_cap >= 1,
          [(e.get('shot_id'), e.get('capture_status'),
            e.get('readback_via')) for e in kfs])
    check('取样方式可解释（gl_draw 或 raf_preserved）',
          all(e.get('readback_via') in ('gl_draw', 'raf_preserved')
              for e in kfs),
          [e.get('readback_via') for e in kfs])
    check('GPU 补丁生效（帧缓冲保住）',
          (manifest.get('collector') or {}).get('capabilities', {})
          .get('gpu_patch') == 'applied',
          (manifest.get('collector') or {}).get('capabilities', {})
          .get('gpu_patch'))
    n_png = manifest['integrity']['n_png']
    check('会话包落了 PNG（非纯白）', n_png >= 1, n_png)

    # ---- L1：图 ↔ state 的绑定、完整性参数（“不靠时间猜”）----
    _sh_path = Path(d) / 'client' / 'shots' / 'shots.jsonl'
    shots_j = []
    if _sh_path.exists():
        shots_j = [json.loads(x) for x in
                   _sh_path.read_text(encoding='utf-8').splitlines() if x.strip()]
    check('关键帧落盘带 state 绑定（state_sha）',
          all(s.get('state_sha') for s in shots_j),
          [(s.get('shot_id'), s.get('state_sha')) for s in shots_j])
    check('关键帧落盘带内容指纹（colored/std/thumb16_sha）',
          all(isinstance(s.get('content'), dict)
              and s['content'].get('thumb16_sha') for s in shots_j),
          [(s.get('shot_id'), (s.get('content') or {}).get('colored'))
           for s in shots_j])
    # 关键不变量：每张图都必须“能精确恢复”——要么 state 已在事件流里，
    # 要么随图带了一份完整 state（快门时钉的）。两者都没有才是真缺口。
    _REQ = ('position', 'layers', 'dimensions', 'layout')

    def _recoverable(s):
        if s.get('state_in_stream'):
            return True
        st = s.get('state')
        return isinstance(st, dict) and all(k in st for k in _REQ)

    check('每张关键帧都能精确恢复（在流里或自带完整 state）',
          all(_recoverable(s) for s in shots_j),
          [(s.get('shot_id'), s.get('state_in_stream'),
            bool(isinstance(s.get('state'), dict)), s.get('state_age_ms'))
           for s in shots_j])
    _rule = (_cfg.get('keyframe_paint') or {})
    check('“算画完”的规则随包走（keyframe_paint）',
          _rule.get('colored_min') is not None
          and _rule.get('timeout_ms') is not None, _rule)
    _settled = [s for s in shots_j if s.get('paint_settled')]
    # “没画完”可以发生（刚跳转就拍），但不许**不标**：
    # 要么 settled=true；要么 capture_status 明说不可用（painted_partial 等）。
    _flagged_np = [s for s in shots_j if not s.get('paint_settled')
                   and s.get('capture_status') == 'captured']
    check('关键帧要么画完才拍，要么被标为不可用',
          not _flagged_np and len(_settled) >= 2,
          {'settled': len(_settled), 'total': len(shots_j),
           'unflagged': [(s.get('shot_id'), s.get('capture_status'),
                          (s.get('content') or {}).get('colored'))
                         for s in _flagged_np],
           'all': [(s.get('shot_id'), s.get('moment'), s.get('paint_settled'),
                    s.get('capture_status')) for s in shots_j]})
    _colored_min = float(_rule.get('colored_min') or 0)
    check('画完才拍的图彩色占比达标（分割叠加真在）',
          all(float((s.get('content') or {}).get('colored') or 0)
              >= _colored_min for s in _settled),
          [(s.get('shot_id'), (s.get('content') or {}).get('colored'))
           for s in _settled])

    # ---- 回放保真度：录下来的画面 vs 按 state 重放出来的画面 ----
    # 不靠人：只比“同一 state 重渲染”与“当时那张图”。它同时能抓
    # “当时瓦片没加载完”（V_loaded）与“数据版本变了”（版本锁）。
    fid, fid_path = None, out / 'replay_fidelity.json'
    if args.no_fidelity:
        print('\n[回放保真度] 已按 --no-fidelity 跳过')
    else:
        print('\n[回放保真度] 逐个 checkpoint 恢复 state 并与当时关键帧比对…')
        try:
            r = subprocess.run(
                [sys.executable, str(HERE / 'verify_replay_fidelity.py'),
                 str(d), '--out', str(fid_path)],
                capture_output=True, text=True, timeout=480)
            if r.returncode != 0:
                print('  [warn] 保真度自检返回', r.returncode,
                      (r.stderr or r.stdout or '')[-300:])
            if fid_path.exists():
                fid = json.loads(fid_path.read_text(encoding='utf-8'))
        except Exception as err:
            print('  [warn] 保真度自检异常:', err)

        if fid is None:
            check('回放保真度有成报告', False, '未产出 replay_fidelity.json')
        else:
            s = fid['summary']
            for row in fid['rows']:
                print(f"  state={row['state_id']} "
                      f"ncc={row['ncc']} mad={row.get('mean_abs_diff')} "
                      f"{row['status']}")
            print(f"  平均 NCC {s.get('mean_ncc')} · 最低 {s.get('min_ncc')}"
                  f" · 高 {s.get('n_high')}/中 {s.get('n_medium')}/"
                  f"低 {s.get('n_low')}")
            check('回放保真度有可对比样本',
                  (s.get('n_compared') or 0) >= 1, s)
            check('回放保真度平均 NCC ≥ 0.90',
                  (s.get('mean_ncc') or 0) >= 0.90, s)
            check('回放保真度最低 NCC ≥ 0.80',
                  (s.get('min_ncc') or 0) >= 0.80, s)
            # 位置对上不等于“同一帧”：尺度/朝向/图层/坐标系也要对。
            check('回放字段全对（位置/尺度/朝向/图层/坐标系）',
                  (s.get('n_state_ok') or 0) == (s.get('n_pairs') or 0),
                  {'ok': s.get('n_state_ok'), 'pairs': s.get('n_pairs'),
                   'failed': s.get('state_failed_fields')})
            # 自一致 = 回放自身的噪声上界；它高才说明 ncc 的差距有解释
            check('回放自一致 ≥ 0.90（噪声上界可解释）',
                  (s.get('min_self_ncc') or 0) >= 0.90,
                  {'mean': s.get('mean_self_ncc'),
                   'min': s.get('min_self_ncc'),
                   'n': s.get('n_self_compared')})
            # 逐通道 NCC：灰度 NCC 对“分割叠加在不在”几乎不敏感，
            # 必须再看彩色通道，否则“灰图 vs 彩色分割图”会被报成 0.99。
            # 只看**收敛了的**帧：拖动中拍到的那帧与快门时 state 差一帧运动，
            # 它的低分是记录语义，不是回放错（那些行被标为 motion_lag）。
            _rgbs = [r['rgb_ncc'] for r in fid['rows']
                     if r.get('settle_converged')
                     and not r.get('color_mismatch')
                     and isinstance(r.get('rgb_ncc'), (int, float))]
            check('收敛帧的逐通道 NCC ≥ 0.90',
                  bool(_rgbs) and min(_rgbs) >= 0.90,
                  {'min': round(min(_rgbs), 4) if _rgbs else None,
                   'n': len(_rgbs),
                   'motion_lag': [(r['png'].split('/')[-1], r.get('ncc'))
                                  for r in fid['rows']
                                  if r.get('status') == 'motion_lag'],
                   'color_mismatch': s.get('color_mismatch')})
            # 内容一致只看“可比”的帧：拖动/加载中拍下的那帧已经标了 motion_lag，
            # 它的彩色占比本来就与静止回放不同（那不是内容错，是拍早了一点）。
            _cm = [r for r in fid['rows']
                   if r.get('status') not in ('motion_lag', 'pending_tiles')]
            check('收敛帧内容一致（彩色占比差 ≤ 0.15）',
                  all(r.get('content_ok') for r in _cm
                      if r.get('content_ok') is not None),
                  {'ok': sum(1 for r in _cm if r.get('content_ok')),
                   'checked': len(_cm),
                   'mismatch': [r['png'].split('/')[-1] for r in _cm
                                if r.get('content_ok') is False],
                   'motion_lag': [(r['png'].split('/')[-1],
                                   r.get('colored_rec'), r.get('colored_live'))
                                  for r in fid['rows']
                                  if r.get('status') == 'motion_lag']})
            # 对齐不靠“等够时间”，靠闭环比到一致。静止拍下的帧必须收敛；
            # 拖动中拍下的帧（采集时就标记了 paint_stable=false）单列为 motion_lag，
            # 但**绝不允许**出现“既没收敛、也没解释”的帧。
            check('回放对齐：静止帧全部收敛，运动中帧已标出',
                  (s.get('n_not_converged') or 0) == 0,
                  {'converged': s.get('n_settle_converged'),
                   'pairs': s.get('n_pairs'),
                   'motion_lag': s.get('n_motion_lag'),
                   'pending_tiles': s.get('n_pending_tiles'),
                   'max_settle_ms': s.get('max_settle_ms'),
                   'unexplained': [(r.get('png', '').split('/')[-1],
                                    r.get('ncc'), r.get('capture_stable'))
                                   for r in fid['rows']
                                   if r.get('status') == 'not_converged']})
            # 采到一半的帧必须“既不参赛、也不静默消失”：两边计数得对上
            _np = [x for x in shots_j
                   if x.get('capture_status') == 'painted_partial']
            check('未画完的帧已从保真度排除（不混进均值）',
                  (s.get('n_skipped_partial_paint') or 0) == len(_np),
                  {'skipped_in_fidelity': s.get('n_skipped_partial_paint'),
                   'flagged_in_pack': len(_np)})

    # ---- 复刻前提：每个状态变化都能恢复（不是只有 30s 剖点）----
    sc = [e for e in ev if csp.ctype(e) == 'viewer.state-change']
    full = [e for e in sc if isinstance(e.get('state'), dict)
            and not e.get('state_partial')]
    check(f'状态变化带可恢复 state（{len(full)}/{len(sc)}）',
          bool(sc) and len(full) >= max(1, int(len(sc) * 0.9)),
          f'partial={sum(1 for e in sc if e.get("state_partial"))}')
    check('可恢复点 >= 5（复刻粒度不是 30s 剖点）', len(full) >= 5, len(full))
    check('时间线回放页已生成',
          timeline.exists() and timeline.stat().st_size > 5000,
          (timeline.stat().st_size if timeline.exists() else 0))
    cmp_html = out / 'replay_compare.html'
    if args.no_fidelity:
        print('  [skip] 回放对比页：--no-fidelity 下不生成（不计入断言）')
    else:
        check('回放对比页（当时 vs 回放像素）已生成',
              cmp_html.exists() and cmp_html.stat().st_size > 3000,
              (cmp_html.stat().st_size if cmp_html.exists() else 0))

    n_pass = sum(1 for c in checks if c['ok'])
    result = 'PASS' if n_pass == len(checks) else 'FAIL'

    report = {
        'result': result, 'ts': TS, 'session_id': pack.get('session_id'),
        'pack_dir': str(d), 'episodes_jsonl': str(ep_path),
        'episodes_html': str(out / 'episodes.html'),
        'replay_r2_html': str(out / 'replay_r2.html'),
        'n_events': len(ev), 'n_episodes': res['n_episodes'],
        'policy_version': res['policy_version'],
        'normalized': norm,
        'episodes': [{k: e[k] for k in
                      ('episode_id', 'duration_ms', 'n_events', 'trigger',
                       'no_edit', 'disposition', 'gestures')}
                     for e in res['episodes']],
        'checks': checks, 'n_pass': n_pass, 'n_checks': len(checks),
        'replay_fidelity': (fid['summary'] if fid else None),
        'provenance': {'client_process': 'real',
                       'session_mode': 'scripted_demo',
                       'not_for_training': True},
    }
    (out / 'report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=1, default=str),
        encoding='utf-8')

    index_html(out, [
        ('index.html', '本页', '产物索引'),
        ('timeline_replay.html', '时间线回放（R1）',
         '看到→做了什么；每行都有时间戳与 state'),
        ('replay_r2.html', '关键帧墙（R2）',
         '录到的那几张图，按 episode 归位'),
        ('replay_compare.html', '回放对比',
         '当时 / 回放#1 / 回放#2 三图并排 + 逐字段匹配'),
        ('episodes.html', 'episode 时间轴', '切分与静默段（橙条）'),
        ('replay_fidelity.json', '保真度数字', 'NCC / self_ncc / 字段逐项'),
    ], out / 'index.html')

    print(f'\n{n_pass}/{len(checks)} 项通过')
    print('RESULT:', result)
    print('产物:', csp.relpath(out))
    print('  回放/甘特图:', out / 'episodes.html')
    print('  episode 派生:', ep_path)
    print('  回放保真度:', (fid_path if fid else '（未跑）'))
    if result != 'PASS':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
