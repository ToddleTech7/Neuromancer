"""提交锚点钩子验证：recorder v0.5 的 fetch/XHR 观察机制。

用法（仓库根目录）：
    python scripts/verify_cave_recorder_submit_hooks.py

装置：本地 Neuroglancer（真实前端）+ 本地 fake CAVE 端点（模拟
ChunkedGraph 写接口回执：do_merge / execute_split / split_preview /
operation details）。

流程：注入 recorder → 页面内对 fake 端点发起 5 个调用（3 fetch 写 +
1 fetch 读 + 1 XHR 写）→ 检查 recorder 是否捕获请求/回执/operation_id
（fetch 与 XHR 两路，v0.5）+ 响应 Date 头锚点（server_date_ms），
且原 fetch/XHR 功能不受影响（透传正确）。

如实边界：fake 端点是"钩子机制验证装置"，不代表已接通真实 CAVE 写接口；
装置显式暴露 Date 头（Access-Control-Expose-Headers）以验证 server_date_ms
链路——真实部署中 recorder 与 CAVE 同源、不受跨域头限制。真实环境启用
待 C0 之后的现场验证（公开栈不做任何写操作）。
"""

import json
import re
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_util
import cave_session_pack as csp   # ctype：事件名对齐 RF01–RF15

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SERVER = HERE / 'cave_local_demo_server.py'
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')

OUT = ROOT / 'results' / 'recorder_verify'
OUT.mkdir(parents=True, exist_ok=True)
TS = datetime.now().strftime('%Y%m%d_%H%M%S')


# ---------- fake CAVE 端点（线程内） ----------
class _FakeCAVE(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静音
        pass

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods',
                         'GET, POST, OPTIONS')
        # 让 Date 头对跨域页面可见（验证 server_date_ms 链路）
        self.send_header('Access-Control-Expose-Headers', 'Date')

    def _json(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if 'details' in self.path:
            self._json({'operation_id': 424244, 'status': 'ok'})
        else:
            self._json({'ok': True})

    def do_POST(self):
        if 'do_merge' in self.path:
            self._json({'operation_id': 424242, 'status': 'ok'})
        elif 'execute_split' in self.path:
            self._json({'operation_id': 424243, 'status': 'ok'})
        elif 'split_preview' in self.path:
            self._json({'preview': [[1, 2, 3]], 'status': 'ok'})
        else:
            self._json({'ok': True})


fake = ThreadingHTTPServer(('127.0.0.1', 0), _FakeCAVE)
fake_port = fake.server_address[1]
threading.Thread(target=fake.serve_forever, daemon=True).start()
print(f'fake CAVE 端点（线程内）: http://127.0.0.1:{fake_port}')

# ---------- 本地 Neuroglancer（子进程） ----------
proc = subprocess.Popen(
    [sys.executable, '-u', str(SERVER)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True, encoding='utf-8', errors='replace', bufsize=1,
    cwd=str(ROOT))

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

checks = []


def check(name, cond, detail=''):
    checks.append({'name': name, 'ok': bool(cond),
                   'detail': str(detail)[:220]})
    print(f'  [{"PASS" if cond else "FAIL"}] {name}'
          + (f' — {detail}' if detail else ''))


pack_raw = None
fetch_result = None
xhr_result = None
try:
    with sync_playwright() as p:
        b = browser_util.launch_browser(p)
        page = b.new_page(viewport={'width': 1280, 'height': 800})
        page.on('pageerror', lambda e: print('[pageerror]', str(e)[:160]))
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_function('() => !!window.viewer', timeout=30000)

        page.add_script_tag(content=RECORDER)
        page.wait_for_function(
            '() => window.__caveRecorder && window.__caveRecorder.loaded',
            timeout=10000)
        sid = page.evaluate('() => window.__caveRecorder.start()')
        print('session:', sid)
        time.sleep(0.5)

        base = f'http://127.0.0.1:{fake_port}'
        fetch_result = page.evaluate("""async (base) => {
            const out = {};
            const H = {'Content-Type': 'application/json'};
            const post = async (path, body) => {
                const r = await fetch(base + path, {
                    method: 'POST', headers: H,
                    body: JSON.stringify(body)});
                return {status: r.status, json: await r.json()};
            };
            out.merge = await post(
                '/segmentation/api/v1/table/test/operation/do_merge',
                {sink_id: 1, source: [10, 20, 30]});
            out.split = await post(
                '/segmentation/api/v1/table/test/operation/execute_split',
                {sources: [[1, 2, 3]]});
            out.preview = await post(
                '/segmentation/api/v1/table/test/split_preview',
                {sources: [[1, 2, 3]]});
            const r = await fetch(base +
                '/segmentation/api/v1/table/test/operation/details?ids=1');
            out.details = {status: r.status, json: await r.json()};
            return out;
        }""", base)
        time.sleep(0.4)

        xhr_result = page.evaluate("""async (base) => {
            return await new Promise((resolve) => {
                const x = new XMLHttpRequest();
                x.open('POST', base +
                    '/segmentation/api/v1/table/test/operation/do_merge');
                x.setRequestHeader('Content-Type', 'application/json');
                x.onloadend = () => resolve(
                    {status: x.status, text: x.responseText.slice(0, 90)});
                x.send(JSON.stringify({sink_id: 2}));
            });
        }""", base)
        time.sleep(0.8)

        page.evaluate('() => window.__caveRecorder.stop()')
        time.sleep(0.3)
        pack_raw = page.evaluate(
            '() => window.__caveRecorder.exportJSON()')
        b.close()
finally:
    proc.kill()
    fake.shutdown()

pack = json.loads(pack_raw)
pack_path = OUT / f'session_hooks_{TS}.json'
pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1),
                     encoding='utf-8')

ev = pack['events']
# v0.7 起事件名对齐需求清单：写请求走 edit.request/edit.ack，
# 读请求留在 api.request/api.ack。用 ctype 归一化，新旧包都能读。
cnt = Counter(csp.ctype(e) for e in ev)
print()
print('events:', len(ev), '| counts:', dict(cnt))
print('fetch 透传: merge =', fetch_result['merge'],
      '| details =', fetch_result['details'])
print('XHR 透传 :', xhr_result)

# ---- 断言 ----
check('fetch 透传正确（do_merge 回执）',
      fetch_result['merge']['json'].get('operation_id') == 424242,
      fetch_result['merge'])
check('fetch 透传正确（details 回执）',
      fetch_result['details']['json'].get('operation_id') == 424244)
check('XHR 透传正确', xhr_result['status'] == 200
      and '424242' in xhr_result['text'], xhr_result)

reqs = [e for e in ev if csp.ctype(e) in ('api.request', 'edit.request')]
resps = [e for e in ev if csp.ctype(e) in ('api.ack', 'edit.ack')]
ops = [e for e in ev if csp.ctype(e) == 'edit.operation']
kinds = Counter(e.get('kind') for e in reqs)

check('api/edit.request 捕获 >= 5', len(reqs) >= 5, len(reqs))
check('api/edit.ack 捕获 >= 5', len(resps) >= 5, len(resps))
check('XHR 路径被捕获', any(e.get('via') == 'xhr' for e in reqs),
      [e.get('via') for e in reqs])
check('operation_id 捕获（>= 3 条）', len(ops) >= 3,
      [e.get('op_id') for e in ops])
check('op_id 含 424242/424243', 
      {424242, 424243} <= {e.get('op_id') for e in ops},
      [e.get('op_id') for e in ops])
check('XHR operation_id via=xhr（v0.5）',
      any(e.get('via') == 'xhr' for e in ops),
      [e.get('via') for e in ops])
sdate_n = sum(1 for e in resps if e.get('server_date_ms'))
check('server_date_ms 锚点捕获（v0.5，>= 4 条）', sdate_n >= 4, sdate_n)
check('分类正确（merge/split/split_preview/read 各 >= 1）',
      all(kinds.get(k, 0) >= 1
          for k in ('merge', 'split', 'split_preview', 'read')),
      dict(kinds))
check('请求体被记录（do_merge body）',
      any(e.get('body') and 'sink_id' in str(e.get('body'))
          for e in reqs))
check('recorder errors 为空', len(pack.get('errors') or []) == 0,
      pack.get('errors'))
check('页面无 Javascript 崩溃', len(ev) > 0
      and cnt.get('session.end') == 1)

summary = {
    'ts': TS, 'session_id': pack.get('session_id'),
    'mode': 'hook mechanism verification (local viewer + fake CAVE)',
    'fake_port': fake_port,
    'fetch_result': fetch_result, 'xhr_result': xhr_result,
    'counts': dict(cnt), 'kinds': dict(kinds),
    'op_ids': [e.get('op_id') for e in ops],
    'checks': checks, 'pass': all(c['ok'] for c in checks),
    'files': {'session': str(pack_path)},
}
sum_path = OUT / f'summary_hooks_{TS}.json'
sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                    encoding='utf-8')
print()
print('session pack:', pack_path)
print('summary:', csp.relpath(sum_path))
print('RESULT:', 'PASS' if summary['pass'] else 'FAIL')
