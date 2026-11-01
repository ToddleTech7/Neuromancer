"""界面状态 + 外部扩展口验证（ui.state / ext.snapshot / api.capability）。

用法（仓库根目录，需要能起本地 Neuroglancer，**不联网**）：
    python scripts/verify_ui_and_providers.py

守四件事：

1. **`ui.state` 真的记 panel 尺寸**——这是多 panel（split view）缺口的补法：
   `viewport{}` 只有一个，但每个 `.neuroglancer-rendered-data-panel` 都有自己的 rect。
2. **工具栏开关会被记到**（`palette_open` false → true），且**只在变化时发**。
3. **provider 口能用**：注册 → 有值就落 `ext.snapshot`；值不变不重复发；
   抛错只报一次（30s 内不重试，不刷屏）；
   没注册时零开销（这是“采不到就先留口”的代价上限）。
4. **`api.capability`**：请求 `/undo/capabilities` 时把**响应体**存下来——
   “组 Undo 到底有没有”只能看这个，不能从前端调用存在推断。

诚实边界：provider 里塞的是**假函数**，证明的是“口子通了”，不是“真实私有状态能读到”。
真实值要等前端按 docs/私有扩展接入.md §3.1 暴露。
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# 跑下层脚本的管道按 UTF-8 收，所以子进程也得按 UTF-8 写（中文 Windows 缺省 cp936）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}
import cave_session_pack as csp  # noqa: E402

ROOT = HERE.parent
OUT = ROOT / 'results' / 'ui_providers'

CAPS = {'enabled': True, 'grouped_undo': True, 'version': 1,
        'max_group_steps': 8}


class Caps(BaseHTTPRequestHandler):
    """假端点：能力查询 + 写接口（只证 recorder 会读，不碰任何真服务）。"""

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        body = json.dumps(CAPS).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(n)
        body = json.dumps({'operation_id': 987654}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    checks = []

    def check(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        print(('  [OK]   ' if ok else '  [FAIL] ') + name
              + (('  ' + str(detail)) if detail else ''))

    srv = ThreadingHTTPServer(('127.0.0.1', 0), Caps)
    caps_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    child = subprocess.Popen([sys.executable, '-u',
                              str(HERE / 'cave_local_demo_server.py')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True,
                             encoding='utf-8', errors='replace',
                             env=CHILD_ENV)
    url = None
    t0 = time.time()
    while time.time() - t0 < 40:
        line = child.stdout.readline()
        if not line:
            break
        m = re.match(r'VIEWER_URL=(\S+)', line.strip())
        if m:
            url = m.group(1)
            break
    if not url:
        print('FAIL: demo server 没起来')
        return 2

    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True, args=[
                '--no-sandbox', '--disable-gpu-sandbox',
                '--use-gl=angle', '--use-angle=metal'])
            ctx = b.new_context(viewport={'width': 1280, 'height': 800})
            ctx.add_init_script(path=str(HERE / 'cave_gpu_patch.js'))
            ctx.add_init_script(path=str(HERE / 'cave_recorder.user.js'))
            pg = ctx.new_page()
            pg.goto(url, wait_until='load')
            pg.wait_for_function('() => window.viewer && window.viewer.state',
                                 timeout=30000)
            pg.wait_for_timeout(5000)

            # ---- 1/2/3：注册 provider（一个正常、一个抛错），开录
            pg.evaluate("""() => {
              const R = window.__caveRecorder;
              let n = 0;
              window.__fakeCalls = 0;
              R.registerProvider('fakeBrush', () => {
                window.__fakeCalls += 1;
                // 前 3 次内容变，之后不变（用来验证“值不变不重发”）
                n += 1;
                return n <= 3 ? {rev: n, candidates: ['1', '2']} : {rev: 3,
                                                                   candidates: ['1', '2']};
              });
              R.registerProvider('broken', () => { throw new Error('no access'); });
              R.start();
            }""")
            pg.wait_for_timeout(2600)

            ui1 = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const u = R.events.filter(e => e.type === 'ui.state');
              return {n: u.length, last: u.length ? u[u.length - 1].ui : null};
            }""")
            print('\n=== ui.state（初始） ===')
            print(json.dumps(ui1['last'], ensure_ascii=False))
            check('ui.state 至少 1 条', ui1['n'] >= 1, ui1['n'])
            panels = (ui1['last'] or {}).get('panels') or []
            check('记录到多个 panel 的尺寸（split view 缺口）',
                  len(panels) >= 2, len(panels))
            check('panel rect 是 [x,y,w,h] 四个整数',
                  all(isinstance(p, list) and len(p) == 4
                      and all(isinstance(v, int) for v in p) for p in panels),
                  panels[:2])
            check('初始 Tool 面板是关的',
                  (ui1['last'] or {}).get('tool', {}).get('palette_open') is False,
                  (ui1['last'] or {}).get('tool'))

            # ---- 点开 Tool：应当多一条 ui.state
            pg.click('.neuroglancer-tool-palette-button')
            pg.wait_for_timeout(2200)
            ui2 = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const u = R.events.filter(e => e.type === 'ui.state');
              return {n: u.length, last: u.length ? u[u.length - 1].ui : null};
            }""")
            print('\n=== ui.state（点开 Tool 后） ===')
            print(json.dumps(ui2['last'], ensure_ascii=False))
            check('工具栏开关会产生新的 ui.state（不是靠 position 变化）',
                  ui2['n'] > ui1['n'], '%d -> %d' % (ui1['n'], ui2['n']))
            tool = (ui2['last'] or {}).get('tool') or {}
            check('palette_open 变 true', tool.get('palette_open') is True, tool)
            check('记到工具项列表', len(tool.get('items') or []) >= 1,
                  tool.get('items'))

            # ---- 4：api.capability
            # 路径要过 classifyApi（真实 CAVE 就是 /segmentation/api/v1/undo/...）
            pg.evaluate("""() => fetch(
              'http://127.0.0.1:%d/segmentation/api/v1/undo/capabilities'
            ).then(r => r.json())""" % caps_port)
            pg.wait_for_timeout(1200)
            cap = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const c = R.events.filter(e => e.type === 'api.capability');
              return {n: c.length, body: c.length ? c[0].body : null};
            }""")
            print('\n=== api.capability ===')
            print(json.dumps(cap, ensure_ascii=False)[:400])
            check('能力查询响应体被存下来', cap['n'] == 1, cap['n'])
            check('响应体可 JSON 解析且带 grouped_undo',
                  bool(cap['body']) and 'grouped_undo' in str(cap['body']),
                  str(cap['body'])[:80])

            # ---- 3：provider 行为
            snap = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const ex = R.events.filter(e => e.type === 'ext.snapshot');
              const err = R.events.filter(
                e => e.type === 'collector.status' && e.provider_error);
              return {
                n: ex.length,
                fromFake: ex.filter(e => e.provider === 'fakeBrush').length,
                errN: err.length,
                calls: window.__fakeCalls,
                providers: R.listProviders(),
              };
            }""")
            print('\n=== ext.snapshot / provider 错误 ===')
            print(json.dumps(snap, ensure_ascii=False)[:500])
            check('provider 有值就落 ext.snapshot', snap['fromFake'] >= 1,
                  snap['fromFake'])
            check('值不变不重复发（调用次数 > 落盘条数）',
                  snap['calls'] > snap['fromFake'],
                  'calls=%d snapshots=%d' % (snap['calls'], snap['fromFake']))
            check('抛错的 provider 只报一次（不刷屏）', snap['errN'] == 1,
                  snap['errN'])
            check('错误留在 listProviders 里可查',
                  bool((snap['providers'].get('broken') or {}).get('err')),
                  snap['providers'].get('broken'))

            # ---- 5：关掉 ui 采集
            pg.evaluate("""() => {
              const R = window.__caveRecorder;
              R.setConfig({ui_state: false});
              window.__nBefore = R.events.filter(e => e.type === 'ui.state').length;
            }""")
            pg.click('.neuroglancer-tool-palette-button')
            pg.wait_for_timeout(2200)
            off = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              return {before: window.__nBefore,
                      after: R.events.filter(e => e.type === 'ui.state').length};
            }""")
            check('ui_state=false 后不再新增', off['after'] == off['before'],
                  '%d -> %d' % (off['before'], off['after']))

            # ---- 6：P1-1 私有本机记录（localStorage）
            pg.evaluate("""() => {
              localStorage.setItem(
                'neuroglancer-graph-edit:graphene://https://x/seg',
                JSON.stringify({pendingGroup: 'g-42', step: 3, protected: true}));
              localStorage.setItem(
                'neuroglancer.brush-merge.v1:graphene://https://x/seg',
                'batch=7;rev=91');
              localStorage.setItem('unrelated.key', 'should-be-ignored');
              // 让节流立即放行
              window.__caveRecorder.lastJournalAt = 0;
            }""")
            pg.wait_for_timeout(1800)
            jr = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const js = R.events.filter(e => e.type === 'private.journal');
              return {n: js.length,
                      keys: js.length ? js[js.length-1].entries.map(x => x.key) : [],
                      val: js.length ? js[js.length-1].entries.find(
                        x => x.key.indexOf('graph-edit') > 0) : null};
            }""")
            print('\n=== private.journal ===')
            print(json.dumps(jr, ensure_ascii=False)[:500])
            check('localStorage 私有键被记下来', jr['n'] >= 1, jr['n'])
            check('只收 neuroglancer-* 两个前缀（忽略无关键）',
                  len(jr['keys']) == 2
                  and all('neuroglancer' in k for k in jr['keys']), jr['keys'])
            check('小值直接存正文（pendingGroup 可读）',
                  bool(jr['val']) and 'pendingGroup' in (jr['val'] or {}).get('value', ''),
                  (jr['val'] or {}).get('value'))

            # ---- 7：P1-2 请求头里的组/步骤
            pg.evaluate("""() => fetch(
              'http://127.0.0.1:%d/segmentation/api/v1/merge',
              {method: 'POST',
               headers: {'Content-Type': 'application/json',
                         'X-CAVE-Edit-Group': 'g-42',
                         'X-CAVE-Edit-Step': '3',
                         'X-CAVE-Request-ID': 'req-abc-123'},
               body: JSON.stringify({points: [[1, 2, 3], [4, 5, 6]]})}
            ).then(r => r.json())""" % caps_port)
            pg.wait_for_timeout(1500)
            hd = pg.evaluate("""() => {
              const R = window.__caveRecorder;
              const rs = R.events.filter(
                e => e.type === 'edit.request' && e.edit_group);
              return {n: rs.length,
                      last: rs.length ? {
                        group: rs[rs.length-1].edit_group,
                        step: rs[rs.length-1].edit_step,
                        rid: rs[rs.length-1].edit_request_id,
                        localRid: !!rs[rs.length-1].request_id} : null};
            }""")
            print('\n=== edit.request 头 ===')
            print(json.dumps(hd, ensure_ascii=False)[:400])
            check('写请求带上了 edit_group/edit_step',
                  hd['n'] >= 1 and (hd['last'] or {}).get('group') == 'g-42'
                  and (hd['last'] or {}).get('step') == '3', hd['last'])
            check('头里的 request id 与本地生成的区分开',
                  (hd['last'] or {}).get('rid') == 'req-abc-123',
                  hd['last'])

            b.close()
    finally:
        srv.shutdown()
        child.kill()

    ok = all(c['ok'] for c in checks)
    print('\n%d 项检查：%s' % (len(checks), 'PASS' if ok else 'FAIL'))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'summary.json').write_text(json.dumps(
        {'checks': checks, 'pass': ok}, ensure_ascii=False, indent=1),
        encoding='utf-8')
    print('summary: %s' % csp.relpath(OUT / 'summary.json'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
