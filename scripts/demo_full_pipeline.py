"""端到端链条 demo v0.2：本地前端 → 采集 → 打包 → 关联 → 回放 → 样本。

链条（公开数据；本地网关只演示"提交被执行"，不做任何真实写操作）：
  [1] 本地 Neuroglancer 前端（真实前端 + minnie65 公开 EM + 分割层）
  [2] 本地 CAVE 网关：回执里给出**真实 op_id**（取自 ops.jsonl 的公开记录）
      并回显 client_request_id；不写任何远端数据
  [3] recorder v0.6 注入并录制：真实交互 + 1Hz 心跳 + 提交前后关键帧
      + state_checkpoint + 图层/视口/nm-per-px
  [4] 导出含图像会话包 → cave_session_pack 落成目录契约（manifest/client/shots）
  [5] 服务端 op 快照 = 那一条**真实 op 原样记录**（不改时间戳）
  [6] cave_join 关联：exact-anchor（op_id 一致）；时钟偏移如实报出
  [7] 回放：静态时间线 HTML（看到→做了什么→提交了什么）
  [8] 样本导出：真实 op + 钉住版本渲染 + 真实过程列（process 不再为 null）
  [9] 报告 report.json + 五段 provenance

五段 provenance（用户 2026-10-26 确认的口径）：
  service_facts      = real       公开 op 记录（minnie65_public）
  visual_assets      = real       按 op 时刻钉住版本重渲染 EM / mask
  client_process     = real       本会话真实录制
  submit_execution   = simulated  公开栈只读；回执由本地网关给出
  join               = simulated  锚点来自仿真回执（op_id 真实，因果关系仿真）

用法（仓库根目录）：
  python scripts/demo_full_pipeline.py
  python scripts/demo_full_pipeline.py --headed --keep-open 25
  python scripts/demo_full_pipeline.py --out results/pipeline_demo
产物：<out>/session_pack/<sid>/（目录包）、server_ops_*.jsonl、joined/、report_*.json；
      HTML 由回放器写入 results/replay/（路径记入 report）。

如实边界：编辑未被真实执行；root id 与前后状态来自公开栈真实记录；
时钟偏移会是一个很大的负值（真实 op 发生于 2025-12，会话在本次运行），
这恰好是"因果关系为仿真"的可核验证据，不做时间戳伪造。
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image
from cloudvolume import CloudVolume
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# 跑下层脚本的管道按 UTF-8 收，所以子进程也得按 UTF-8 写（中文 Windows 缺省 cp936）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}
import browser_util  # noqa: E402
import cave_session_pack as csp  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument('--headed', action='store_true',
                help='显示浏览器窗口（观看实际操作）')
ap.add_argument('--keep-open', type=int, default=0, metavar='N',
                help='可见模式：跑完保持浏览器打开 N 秒再关闭（默认 0）')
ap.add_argument('--pace', type=float, default=1.0, metavar='X',
                help='动作间隔倍率（慢速演示建议 2~3；默认 1）')
ap.add_argument('--op', type=int, default=1956846,
                help='用作服务端事实的公开 op id（默认 1956846）')
ap.add_argument('--out', default=str(ROOT / 'results' / 'pipeline_demo'))
ap.add_argument('--no-replay-viewer', action='store_true',
                help='跳过只读状态回放（cave_replay_viewer）')
args = ap.parse_args()

SERVER = HERE / 'cave_local_demo_server.py'
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')
OPS = ROOT / 'results' / 'cave_watch' / 'ops.jsonl'
OUT = Path(args.out)
OUT.mkdir(parents=True, exist_ok=True)
TS = datetime.now().strftime('%Y%m%d_%H%M%S')
EM_SOURCE = ('precomputed://https://storage.googleapis.com/'
             'iarpa_microns/minnie/minnie65/em')
SEG_SOURCE = ('precomputed://https://storage.googleapis.com/'
              'iarpa_microns/minnie/minnie65/seg')

LEGS = {
    'service_facts': ('real', '公开 op 记录（minnie65_public，原样，未改时间戳）'),
    'visual_assets': ('real', '按 op 时刻钉住版本重渲染 EM/mask'),
    'client_process': ('real', 'recorder v0.6 在本会话真实录制'),
    'submit_execution': ('simulated', '公开栈只读；回执由本地网关给出，未执行真实写入'),
    'join': ('simulated', 'op_id 真实但锚点来自仿真回执；时钟偏移如实报出'),
}

report = {'ts': TS, 'stages': [], 'checks': {}, 'warning': [],
          'provenance': {k: {'status': v[0], 'reason': v[1]}
                         for k, v in LEGS.items()}}


def stage(name, detail, artifact=None):
    report['stages'].append({'name': name, 'detail': str(detail)[:300],
                             'artifact': str(artifact) if artifact else None})
    print(f'  [{name}] {detail}')


def sleep_(s):
    time.sleep(max(0.0, s * args.pace))


def with_retry(fn, tries=6, wait=6):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as err:
            last = err
            print(f'    重试 {i + 1}/{tries}: {type(err).__name__}')
            time.sleep(wait)
    raise last


# ---------- 真实 op（服务端事实） ----------
def load_real_op(op_id):
    if not OPS.exists():
        raise SystemExit(f'缺少 {OPS}；先跑 scripts/cave_op_watcher.py')
    for line in OPS.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if int(r.get('op_id') or -1) == int(op_id):
            return r
    raise SystemExit(f'ops.jsonl 中没有 op {op_id}')


REAL = load_real_op(args.op)
BEFORE = [str(x) for x in (REAL.get('before_root_ids') or [])]
AFTER = [str(x) for x in (REAL.get('after_root_ids') or [])]
SC = (REAL.get('source_coords') or [[None, None, None]])[0]
POS_COORD = [int(v) for v in SC] if all(v is not None for v in SC) else None
DIMS = (4, 4, 40)            # 演示前端坐标空间（minnie65 EM/seg 细层）
BASE_RES = (8, 8, 40)        # op 坐标单位 = PCG base_resolution（设计文档 §2.5）
if POS_COORD is None:
    POS_COORD = [95307, 57044, 21782]
# 落点坐标（base_resolution）→ nm → 演示前端的 dims(4/4/40)
DEMO_NM = (POS_COORD[0] * BASE_RES[0], POS_COORD[1] * BASE_RES[1],
           POS_COORD[2] * BASE_RES[2])
DEMO_POS = ' '.join(str(int(round(nm / d)))
                    for nm, d in zip(DEMO_NM, DIMS))
CLIENT_REQUEST_ID = str(uuid.uuid4())


def read_seg_top2():
    """读演示位置的 seg 块，取体素数最大的两个段与质心（dims 坐标）。"""
    IX, IY, IZ = DEMO_NM
    S = 3584.0
    try:
        cv_seg = with_retry(lambda: CloudVolume(SEG_SOURCE, mip=0,
                                                progress=False,
                                                fill_missing=True))
        rx, ry, rz = [float(v)
                      for v in cv_seg.info['scales'][0]['resolution']]
        x0 = round((IX + 16 - S / 2) / rx)
        y0 = round((IY - 32 - S / 2) / ry)
        z0 = round((IZ + 80 - S / 2) / rz)
        blk = np.asarray(with_retry(
            lambda: cv_seg[x0:x0 + round(S / rx), y0:y0 + round(S / ry),
                           z0:z0 + round(S / rz)])).squeeze(-1)
        vals, cnts = np.unique(blk, return_counts=True)
        order = np.argsort(-cnts)
        out = []
        for i in order[:2]:
            vid = int(vals[i])
            if vid <= 0:
                continue
            cx, cy, cz = np.argwhere(blk == vals[i]).mean(0)
            out.append({'id': vid, 'px': int(cnts[i]),
                        'pos': (int(x0 + cx) * 2, int(y0 + cy) * 2,
                                int(z0 + cz))})
        print('  [seg] 演示位置分割段 top2:',
              [(o['id'], o['px']) for o in out])
        return out
    except Exception as err:
        report['warning'].append(f'seg 预读失败: {type(err).__name__}')
        print(f'  [seg] 预读失败（跳过选中步骤）: {type(err).__name__}')
        return []


# ---------- [1] 本地网关（提交腿仿真） ----------
class _Gateway(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Expose-Headers', 'Date')
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in (('Access-Control-Allow-Origin', '*'),
                     ('Access-Control-Allow-Headers', '*'),
                     ('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')):
            self.send_header(k, v)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n).decode('utf-8', 'replace') if n else ''
        try:
            js = json.loads(body) if body else {}
        except Exception:
            js = {}
        rid = js.get('client_request_id') or js.get('request_id')
        if 'do_merge' in self.path or 'merge' in self.path:
            self._json({
                'operation_id': REAL['op_id'],
                'status': 'ok',
                'client_request_id': rid,
                'before_root_ids': BEFORE, 'after_root_ids': AFTER,
                'execution': 'simulated_by_local_gateway',
                'note': 'operation_id 取自公开 op 记录；本次未向 CAVE 写入',
            })
        else:
            self._json({'ok': True, 'client_request_id': rid,
                        'execution': 'simulated_by_local_gateway'})

    def do_GET(self):
        self._json({'ok': True})


gateway = ThreadingHTTPServer(('127.0.0.1', 0), _Gateway)
gw_port = gateway.server_address[1]
threading.Thread(target=gateway.serve_forever, daemon=True).start()
stage('1/9 本地网关', f'http://127.0.0.1:{gw_port}'
      '（提交腿 simulated；op_id 真实）')

# ---------- [2] 本地前端 ----------
print('  [2/9] 启动本地 Neuroglancer 前端…')
proc = subprocess.Popen(
    [sys.executable, '-u', str(SERVER)], stdin=subprocess.PIPE,
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    encoding='utf-8', errors='replace', bufsize=1, cwd=str(ROOT),
    env=CHILD_ENV)
url = None
deadline = time.time() + 40
while time.time() < deadline:
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


def send(cmd):
    proc.stdin.write(cmd + '\n')
    proc.stdin.flush()


stage('2/9 本地前端', url, url)

# ---------- [3] 录制 ----------
pack_raw = None
fetch_ack = None
render_note = ''
host_shots = []
try:
    with sync_playwright() as p:
        b = browser_util.launch_browser(p, headless=not args.headed)
        page = b.new_page(viewport={'width': 1280, 'height': 800})
        page.on('pageerror', lambda e: None)
        page.on('dialog',
                lambda d: d.accept('demo 备注：确认合并位置（仿真实例）'))
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_function('() => !!window.viewer', timeout=30000)
        page.add_script_tag(content=RECORDER)
        page.wait_for_function(
            '() => window.__caveRecorder && window.__caveRecorder.loaded',
            timeout=10000)
        sid = page.evaluate('() => window.__caveRecorder.start()')
        print('  [3/9] 录制开始:', sid)

        send(f'dims {DIMS[0]} {DIMS[1]} {DIMS[2]}')
        sleep_(0.5)
        send(f'img em {EM_SOURCE}')
        sleep_(0.3)
        send(f'seg seg {SEG_SOURCE}')
        sleep_(0.3)
        send('layer seg')
        seg_ids = read_seg_top2()
        send(f'pos {DEMO_POS}')
        send('cross 6')
        t0 = time.time()
        while time.time() - t0 < 90:
            raw = page.screenshot()
            arr = np.asarray(Image.open(io.BytesIO(raw))
                             .convert('RGB')).astype(int)
            sat = float(((arr.max(-1) - arr.min(-1)) > 12).mean())
            std = float(arr.std())
            if std > 8:          # 画面已有结构（EM 灰度即可）
                render_note = (f'渲染完成 {time.time()-t0:.0f}s'
                               f'（std={std:.0f}，分割彩色像素 {sat*100:.0f}%）')
                break
            time.sleep(3)
        if not render_note:
            render_note = '渲染等待超时（机制演示不阻塞）'
            report['warning'].append(render_note)
        print('  [3/9]', render_note)

        # 真实交互：滚轮 + 拖拽（recorder 采 viewport/wheel/pointer）
        page.mouse.move(320, 200)
        page.mouse.wheel(0, -240)
        sleep_(0.9)
        page.mouse.wheel(0, -240)
        sleep_(0.9)
        page.mouse.down()
        page.mouse.move(380, 240, steps=16)
        page.mouse.up()
        sleep_(0.9)

        # 选段（真实点击 + 选中态同步）
        if seg_ids:
            s1 = seg_ids[0]
            send(f"pos {s1['pos'][0]} {s1['pos'][1]} {s1['pos'][2]}")
            sleep_(1.2)
            page.mouse.click(320, 200)
            send(f"sel seg {s1['id']}")
            sleep_(2.0)
            if len(seg_ids) > 1:
                s2 = seg_ids[1]
                send(f"pos {s2['pos'][0]} {s2['pos'][1]} {s2['pos'][2]}")
                sleep_(1.2)
                page.keyboard.down('Shift')
                page.mouse.click(320, 200)
                page.keyboard.up('Shift')
                send(f"sel seg {s1['id']} {s2['id']}")
                sleep_(2.0)

        # 提交（网关回执：真实 op_id + 回显 client_request_id）
        # 宿主侧关键帧：headless 下页面内 canvas readback 为纯白（已实测），
        # 这里用渲染环境截图作视觉锚点，并在 shots.jsonl 里标 source，
        # 不冒充 recorder 自采。
        base = f'http://127.0.0.1:{gw_port}'
        host_shots.append({
            'shot_id': 'host_pre_submit', 'moment': 'pre_submit',
            'request_id': CLIENT_REQUEST_ID, 'w': 1280, 'h': 800,
            'png_bytes': page.screenshot(),
            'source': 'host_page_screenshot',
            'note': '宿主侧截图（预渲染环境 page.screenshot，非页面内 readback）',
        })
        fetch_ack = page.evaluate("""async ([base, rid]) => {
            const r = await fetch(base +
              '/segmentation/api/v1/table/minnie65_public/operation/do_merge',
              {method: 'POST', headers: {'Content-Type': 'application/json'},
               body: JSON.stringify({sink_id: %d,
                                     source: [%s],
                                     client_request_id: rid})});
            return {status: r.status, json: await r.json()};
        }""" % (int(BEFORE[0]) if BEFORE else 0, DEMO_POS.replace(' ', ','),
                ), [base, CLIENT_REQUEST_ID])
        sleep_(1.2)
        host_shots.append({
            'shot_id': 'host_post_submit', 'moment': 'post_submit',
            'request_id': CLIENT_REQUEST_ID, 'w': 1280, 'h': 800,
            'png_bytes': page.screenshot(),
            'source': 'host_page_screenshot',
            'note': '宿主侧截图（预渲染环境 page.screenshot，非页面内 readback）',
        })

        page.evaluate('() => window.__caveRecorder.addNote()')
        sleep_(0.8)
        send('pos 201400 158100 24760')
        sleep_(1.2)

        page.evaluate('() => window.__caveRecorder.stop()')
        time.sleep(0.5)
        pack_raw = page.evaluate(
            '() => window.__caveRecorder.exportWithImagesJSON()')
        if args.keep_open > 0:
            print(f'  [3/9] 采集完成，浏览器保持打开 {args.keep_open}s')
            time.sleep(args.keep_open)
        b.close()
finally:
    proc.kill()
    gateway.shutdown()

# ---------- [4] 会话包目录契约 ----------
pack = json.loads(pack_raw)
legs = {k: v[0] for k, v in LEGS.items()}
reasons = {k: v[1] for k, v in LEGS.items()}
pack_dir, manifest = csp.pack_session(
    pack, OUT / 'session_pack', legs=legs, reasons=reasons,
    extra_shots=host_shots)
ev = pack['events']
_ct = csp.ctype          # v0.7 事件名对齐 RF01–RF15，统一走 ctype
n_anchors = sum(1 for e in ev if _ct(e) == 'edit.operation')
n_kf = sum(1 for e in ev if _ct(e) == 'keyframe.capture')
n_status = sum(1 for e in ev if _ct(e) == 'collector.status')
n_gest = sum(1 for e in ev if _ct(e) == 'viewport.gesture')
health = manifest['health']
stage('3/9 会话包目录',
      f"{health['n_events']} 事件 / seq {health['seq_first']}–{health['seq_last']}"
      f"（断档 {health['seq_gaps'] or '无'}）/ 手势 {n_gest}"
      f" / 锚点 {n_anchors} / 关键帧 {n_kf}"
      f" / 截图 {manifest['integrity']['n_png']} 张", pack_dir)

# ---------- [5] 服务端 op 快照（真实记录，原样） ----------
ops_row = {k: REAL.get(k) for k in
           ('op_id', 'type', 'user', 'timestamp', 'before_root_ids',
            'after_root_ids', 'source_coords', 'sink_coords')}
ops_row['origin'] = 'minnie65_public 公开记录原样（未改时间戳）'
ops_path = OUT / f'server_ops_{TS}.jsonl'
ops_path.write_text(json.dumps(ops_row, ensure_ascii=False, default=str) + '\n',
                    encoding='utf-8')
stage('4/9 服务端 op 快照', f"op {REAL['op_id']}（{REAL.get('type')}）", ops_path)

# ---------- [6] join ----------
joined_dir = OUT / 'joined'
r = subprocess.run(
    [sys.executable, str(HERE / 'cave_join.py'), str(pack_dir),
     '--ops', str(ops_path), '--out', str(joined_dir)],
    capture_output=True, text=True, encoding='utf-8', errors='replace',
    cwd=str(ROOT), env=CHILD_ENV)
if r.returncode != 0:
    raise SystemExit(f'join 失败: {r.stderr[-600:]}')
print(r.stdout.rstrip())
sid_full = pack['session_id']
cands = sorted(joined_dir.glob(f'{sid_full}.join*.jsonl'))
join_jsonl = cands[-1] if cands else joined_dir / f'{sid_full}.join.jsonl'
sums = sorted(joined_dir.glob(f'{sid_full}.join_summary*.json'))
join_summary = sums[-1] if sums else None
n_matched = 0
if join_summary:
    s = json.loads(join_summary.read_text(encoding='utf-8'))
    n_matched = s.get('n_matched', 0)
    report['checks']['join_basis'] = s.get('clock', {}).get('basis')
    report['checks']['join_matched'] = n_matched
    report['checks']['clock_offset_ms'] = s.get('clock', {}).get('offset_ms')
    report['checks']['client_request_id_in_join'] = \
        (s.get('client_request_ids') or [None])[0]
stage('5/9 join 关联', f'matched={n_matched}；'
      f'basis={report["checks"].get("join_basis")}；'
      f'offset={report["checks"].get("clock_offset_ms")}ms', join_jsonl)

# ---------- [7] 回放 ----------
pre_html = {p.name for p in (ROOT / 'results' / 'replay').glob('*.html')}
r = subprocess.run(
    [sys.executable, str(HERE / 'cave_session_replay.py'),
     str(pack_dir), '--join', str(join_jsonl)],
    capture_output=True, text=True, encoding='utf-8', errors='replace',
    cwd=str(ROOT), env=CHILD_ENV)
if r.returncode != 0:
    raise SystemExit(f'replay 失败: {r.stderr[-600:]}')
print(r.stdout.rstrip())
new_html = sorted(
    (p for p in (ROOT / 'results' / 'replay').glob('*.html')
     if p.name not in pre_html), key=lambda p: p.stat().st_mtime)
html_path = new_html[-1] if new_html else None
stage('6/9 回放 HTML', '看到 → 做了什么 → 提交了什么', html_path)

# ---------- [6b] 只读状态回放（R1）+ 写闸门 ----------
replay_manifest = pack_dir / 'derived' / 'replay' / 'replay_manifest.json'
if not args.no_replay_viewer:
    r = subprocess.run(
        [sys.executable, str(HERE / 'cave_replay_viewer.py'), str(pack_dir),
         '--max-checkpoints', '6'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=str(ROOT), env=CHILD_ENV)
    print(r.stdout.rstrip()[-800:])
    if r.returncode != 0:
        report['warning'].append('状态回放未全部恢复（见 replay_manifest）')
    if replay_manifest.exists():
        rm = json.loads(replay_manifest.read_text(encoding='utf-8'))
        report['checks']['readonly_replay_flag'] = rm.get(
            'readonly_replay_flag')
        report['checks']['replay_applied'] = (
            f"{rm.get('n_state_applied')}/{rm.get('n_checkpoints')}")
        report['checks']['replay_guard_blocked'] = len(
            (rm.get('guard') or {}).get('blocked_attempts') or [])
        report['checks']['visual_replay_status'] = rm.get(
            'visual_replay_status')
        stage('6b/9 只读状态回放',
              f"恢复 {report['checks']['replay_applied']}；"
              f"闸门拦截 {report['checks']['replay_guard_blocked']}；"
              f"视觉状态={report['checks']['visual_replay_status']}",
              replay_manifest)

# ---------- [8] 样本（真实 op + 真实过程列） ----------
sample_out = OUT / 'sample_from_session'
r = subprocess.run(
    [sys.executable, str(HERE / 'demo_export_samples.py'),
     '--ops', str(REAL['op_id']), '--out', str(sample_out),
     '--join', str(join_jsonl), '--session-pack', str(pack_dir)],
    capture_output=True, text=True, encoding='utf-8', errors='replace',
    cwd=str(ROOT), env=CHILD_ENV)
print(r.stdout.rstrip()[-1200:])
sample_json = sample_out / f"sample_{REAL['op_id']}" / 'sample.json'
if sample_json.exists():
    smp = json.loads(sample_json.read_text(encoding='utf-8'))
    report['checks']['sample_process_session'] = \
        (smp.get('process') or {}).get('session_id')
    report['checks']['sample_viewpoint'] = bool(
        (smp.get('process') or {}).get('viewpoint_before'))
    report['checks']['sample_keyframes'] = len(
        (smp.get('process') or {}).get('keyframes') or [])
    stage('7/9 样本（含过程列）',
          f"process.session_id={report['checks']['sample_process_session']}；"
          f"关键帧 {report['checks']['sample_keyframes']}", sample_json)
else:
    report['warning'].append('样本未生成（见上面的导出日志）')
    stage('7/9 样本', '未生成', None)

# ---------- [9] 报告 ----------
report['checks']['session_events'] = len(ev)
report['checks']['anchors'] = n_anchors
report['checks']['gateway_ack'] = fetch_ack
report['checks']['pack_health'] = health
report['checks']['client_request_id_sent'] = CLIENT_REQUEST_ID
report['paths'] = {
    'pack_dir': str(pack_dir), 'ops': str(ops_path),
    'join': str(join_jsonl), 'replay': str(html_path),
    'replay_manifest': (str(replay_manifest)
                        if replay_manifest.exists() else None),
    'sample': str(sample_json) if sample_json.exists() else None,
}
report['next'] = '真实 CAVE 接入后：把 submit_execution/join 两段改为 real'
report_path = OUT / f'report_{TS}.json'
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1,
                                  default=str), encoding='utf-8')
stage('8/9 报告', report_path)

ok = (n_matched >= 1
      and report['checks'].get('sample_process_session') == sid_full
      # 活性证据改看 seq 连续（心跳已废除）：
      # 有断档说明采集中断过，只有完整 seq 才能拿来当过程数据用。
      and not health.get('seq_gaps')
      and health['n_events'] >= 30)
print()
print('链条地图（五段来源）：')
for k, v in report['provenance'].items():
    print(f'  {k:<17} {v["status"]:<10} {v["reason"]}')
print(f'  会话包 : {pack_dir}')
print(f'  op 快照: {ops_path}')
print(f'  join   : {join_jsonl}')
print(f'  回放   : {html_path}')
print(f'  样本   : {report["paths"]["sample"]}')
print(f'  报告   : {report_path}')
print('RESULT:', 'PASS' if ok else 'CHECK（见 report.checks）')
