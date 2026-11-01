"""数据链控制台：一个本地 Web 页面，把 demo 命令变成按钮。

功能卡（点按钮即跑，实时日志 + 完成后直达产物）：
  - 端到端链条 demo：无头 / 可见浏览器（demo_full_pipeline.py）
  - 样本卡导出：可选指定 op（demo_export_samples.py）
  - 快速验证：单元测试 58 项（unittest discover）
产物区（自动扫描 results/）：
  - 回放页列表（results/replay/*.html，点击新标签打开）
  - 样本卡（results/export_demo：contact_sheet 预览 + 样本/跳过列表）
  - 链条报告（results/pipeline_demo/report_*.json 摘要）

用法（仓库根目录）：
  python3 scripts/demo_console_server.py [--port 8788]
然后浏览器打开打印出的 CONSOLE_URL。

约束：仅监听 127.0.0.1；同一时间只允许一个任务；/files 只服务 results/ 下文件。
"""

import argparse
import json
import os
import random
import string
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# 跑起来的子进程输出收进管道按 UTF-8 读，所以让它按 UTF-8 写
# （中文 Windows 缺省是 cp936；只改这个子进程，不动整个进程的环境）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}
RES = ROOT / 'results'

LOCK = threading.Lock()
TASKS = {}

PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>Neuromancer 数据链控制台</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #0f1115; color: #d7dce3;
         font: 14px/1.5 -apple-system, "PingFang SC", system-ui, sans-serif; }
  header { padding: 18px 24px; border-bottom: 1px solid #232833;
           display: flex; align-items: baseline; gap: 12px; }
  header h1 { font-size: 17px; margin: 0; }
  header .sub { color: #7d8794; font-size: 12px; }
  main { padding: 20px 24px 40px; max-width: 1200px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
  .card { background: #171a21; border: 1px solid #242a35; border-radius: 10px;
          padding: 14px 16px; }
  .card h3 { margin: 0 0 6px; font-size: 14px; }
  .card p { margin: 0 0 10px; color: #8b95a3; font-size: 12px; }
  button { background: #1f6feb; border: 0; color: #fff; border-radius: 7px;
           padding: 7px 12px; font-size: 13px; cursor: pointer; margin-right: 6px; }
  button:hover { background: #2f81f7; }
  button.ghost { background: #222835; }
  button.ghost:hover { background: #2b3342; }
  button:disabled { opacity: .45; cursor: default; }
  input[type=text] { background: #0f1115; border: 1px solid #2a3140;
        color: #d7dce3; border-radius: 7px; padding: 6px 9px; width: 100%;
        font-size: 12px; margin-bottom: 8px; }
  #log { background: #0a0c10; border: 1px solid #242a35; border-radius: 10px;
         margin-top: 18px; padding: 12px 14px; height: 260px; overflow: auto;
         font: 12px/1.45 ui-monospace, Menlo, monospace; white-space: pre-wrap;
         color: #9fe39f; }
  #logtitle { color: #7d8794; font-size: 12px; margin-top: 16px; }
  section { margin-top: 26px; }
  section h2 { font-size: 14px; margin: 0 0 10px; color: #cdd5df; }
  .list a { color: #6cb2ff; text-decoration: none; display: block;
            padding: 5px 0; border-bottom: 1px solid #1c212b; }
  .list a:hover { color: #9ccaff; }
  .meta { color: #7d8794; font-size: 12px; }
  .grid2 { display: grid; grid-template-columns: 1.1fr 1fr; gap: 14px; }
  .shot { width: 100%; border-radius: 8px; border: 1px solid #242a35; }
  .pill { display: inline-block; border-radius: 20px; padding: 1px 9px;
          font-size: 11px; margin-left: 6px; }
  .pill.ok { background: #14311d; color: #57d364; }
  .pill.no { background: #3a1d1d; color: #ff8a8a; }
  .empty { color: #5c6672; font-size: 12px; }
  code { background: #171a21; padding: 1px 6px; border-radius: 5px;
         font-size: 12px; color: #9fb1c8; }
</style>
</head>
<body>
<header>
  <h1>Neuromancer 数据链控制台</h1>
  <span class="sub">本地演示 · 公开数据 · 无真实写操作</span>
</header>
<main>
  <div class="cards">
    <div class="card">
      <h3>端到端链条 demo</h3>
      <p>前端（EM+分割）→ 选段/合并操作 → join → 回放</p>
      <button onclick="run('pipeline', {})">快速运行（无头）</button>
      <button class="ghost" onclick="run('pipeline', {headed: true, pace: 2.5, keep_open: 20})">慢速演示（可见）</button>
    </div>
    <div class="card">
      <h3>样本卡导出</h3>
      <p>真实 ops → 图像 + 标签（3–5 分钟）</p>
      <input id="opsInput" type="text" placeholder="指定 op（可留空，空格分隔）">
      <button onclick="run('export', {ops: document.getElementById('opsInput').value})">导出</button>
    </div>
    <div class="card">
      <h3>快速验证</h3>
      <p>单元测试 66 项（约 15 秒）</p>
      <button class="ghost" onclick="run('unit', {})">跑单元测试</button>
      <button class="ghost" onclick="run('validate', {})">校验数据集合约</button>
    </div>
  </div>

  <div id="logtitle">任务输出（点击上方按钮开始）</div>
  <div id="log">等待任务…</div>

  <section>
    <h2>回放页</h2>
    <div id="replays" class="list"><span class="empty">暂无（跑一次链条 demo）</span></div>
  </section>

  <section>
    <h2>样本卡</h2>
    <div id="exportArea"><span class="empty">暂无（跑一次样本导出）</span></div>
  </section>

  <section>
    <h2>链条报告</h2>
    <div id="reports" class="list"><span class="empty">暂无（跑一次链条 demo）</span></div>
  </section>

  <section>
    <h2>说明</h2>
    <p class="meta">真实部署时："可见浏览器"对应的本地前端换成自建 CAVE 前端，
    recorder 注入方式不变；其余环节（会话包 / join / 回放 / 导出）原样使用。</p>
  </section>
</main>
<script>
let polling = null;
function esc(x) {
  return String(x == null ? '' : x).replace(/[&<>"]/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
async function run(kind, extra) {
  const r = await fetch('/api/run', {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign({kind}, extra))});
  if (!r.ok) { alert('无法启动：' + await r.text()); return; }
  startPoll((await r.json()).id);
}
function startPoll(id) {
  const logEl = document.getElementById('log');
  const title = document.getElementById('logtitle');
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  if (polling) clearInterval(polling);
  polling = setInterval(async () => {
    const d = await (await fetch('/api/task?id=' + encodeURIComponent(id))).json();
    logEl.textContent = (d.log || []).join('\\n') || '（启动中…）';
    logEl.scrollTop = logEl.scrollHeight;
    title.textContent = '任务 ' + d.id + ' · ' + d.kind + ' · ' + d.status
      + (d.status === 'running' ? '' : ' · rc=' + d.rc);
    if (d.status !== 'running') {
      clearInterval(polling); polling = null;
      document.querySelectorAll('button').forEach(b => b.disabled = false);
      refresh();
    }
  }, 900);
}
async function refresh() {
  const s = await (await fetch('/api/state')).json();
  const a = s.artifacts;
  const rep = document.getElementById('replays');
  rep.innerHTML = a.replays.length ? a.replays.map(r =>
    '<a href="' + esc(r.url) + '" target="_blank">' + esc(r.name) + '</a>'
  ).join('') : '<span class="empty">暂无（跑一次链条 demo）</span>';
  const ex = document.getElementById('exportArea');
  if (!a.export.contact_sheet && !a.export.samples.length) {
    ex.innerHTML = '<span class="empty">暂无（跑一次样本导出）</span>';
  } else {
    let h = '<div class="grid2"><div>';
    h += '<div class="list">' + a.export.samples.map(s =>
      '<a href="' + esc(s.dir_url) + '" target="_blank">op ' + esc(s.op_id)
      + ' · ' + esc(s.state) + ' · mask ' + esc(s.mask_hit_px)
      + ' px</a>').join('') + '</div>';
    if (a.export.skipped.length) {
      h += '<p class="meta">跳过（窗口内 0 px，如实记录）：'
        + a.export.skipped.map(s => esc(s.op_id)).join('、') + '</p>';
    }
    h += '</div><div>';
    if (a.export.contact_sheet) {
      h += '<a href="' + esc(a.export.contact_sheet)
        + '" target="_blank"><img class="shot" src="'
        + esc(a.export.contact_sheet) + '"></a>';
    }
    h += '</div></div>';
    ex.innerHTML = h;
  }
  const rp = document.getElementById('reports');
  rp.innerHTML = a.reports.length ? a.reports.map(r =>
    '<a href="' + esc(r.url) + '" target="_blank">' + esc(r.name)
    + ' · matched ' + esc(r.matched) + ' · ' + esc(r.ts) + '</a>'
  ).join('') : '<span class="empty">暂无（跑一次链条 demo）</span>';
}
window.addEventListener('load', refresh);
</script>
</body>
</html>
"""


def build_cmd(kind, opts):
    py = sys.executable
    if kind == 'pipeline':
        cmd = [py, 'scripts/demo_full_pipeline.py']
        if opts.get('headed'):
            cmd.append('--headed')
        if opts.get('keep_open'):
            cmd += ['--keep-open', str(int(opts['keep_open']))]
        if opts.get('pace'):
            cmd += ['--pace', str(float(opts['pace']))]
        return cmd
    if kind == 'export':
        cmd = [py, 'scripts/demo_export_samples.py']
        ops = str(opts.get('ops') or '').split()
        if ops:
            cmd += ['--ops'] + ops
        return cmd
    if kind == 'unit':
        return [py, '-m', 'unittest', 'discover', '-s', 'scripts',
                '-p', 'test_*.py']
    if kind == 'validate':
        return [py, 'scripts/validate_samples.py', 'results/dataset_v0']
    raise ValueError(f'未知任务类型 {kind}')


def kick(kind, opts):
    with LOCK:
        if any(t['status'] == 'running' for t in TASKS.values()):
            raise RuntimeError('已有任务在运行，请等待完成')
    cmd = build_cmd(kind, opts)
    tid = 't_' + datetime.now().strftime('%H%M%S') + '_' + ''.join(
        random.choices(string.ascii_lowercase + string.digits, k=4))
    t = {'id': tid, 'kind': kind, 'status': 'running', 'rc': None,
         'log': deque(maxlen=1500), 'started': time.time()}
    with LOCK:
        TASKS[tid] = t
    threading.Thread(target=_run_task, args=(t, cmd), daemon=True).start()
    return tid


def _run_task(t, cmd):
    t['log'].append(f'$ {" ".join(cmd[1:])}')
    try:
        p = subprocess.Popen(
            cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding='utf-8',
            errors='replace', bufsize=1, env=CHILD_ENV)
        for line in p.stdout:
            t['log'].append(line.rstrip())
        rc = p.wait()
    except Exception as err:
        t['log'].append(f'[console] 启动失败: {type(err).__name__}: {err}')
        rc = -1
    t['rc'] = rc
    t['status'] = 'done' if rc == 0 else 'failed'
    t['ended'] = time.time()
    t['log'].append(f'[console] 任务结束 rc={rc} '
                    f'（{t["ended"] - t["started"]:.0f}s）')


def _tail(path, n=200):
    with path.open('r', encoding='utf-8') as f:
        return deque(f, maxlen=n)


def artifacts():
    out = {'replays': [], 'reports': [],
           'export': {'samples': [], 'skipped': [],
                      'contact_sheet': None}}
    rd = RES / 'replay'
    if rd.exists():
        for p in sorted(rd.glob('*.html'),
                        key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
            out['replays'].append({'name': p.name,
                                   'url': '/files/replay/' + p.name})
    pd = RES / 'pipeline_demo'
    if pd.exists():
        for p in sorted(pd.glob('report_*.json'),
                        key=lambda x: x.stat().st_mtime, reverse=True)[:10]:
            try:
                d = json.loads(p.read_text(encoding='utf-8'))
                matched = d.get('checks', {}).get('join_matched')
            except Exception:
                matched = None
            out['reports'].append({'name': p.name,
                                   'url': '/files/pipeline_demo/' + p.name,
                                   'matched': matched, 'ts': p.stem[7:]})
    ed = RES / 'export_demo'
    if (ed / 'index.jsonl').exists():
        for line in (ed / 'index.jsonl').read_text(
                encoding='utf-8').splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            d = r.get('dir', '')
            out['export']['samples'].append({
                'op_id': r.get('op_id'), 'state': r.get('state'),
                'mask_hit_px': r.get('mask_hit_px'),
                'dir_url': '/files/' + d})
        if (ed / 'skipped.jsonl').exists():
            for line in (ed / 'skipped.jsonl').read_text(
                    encoding='utf-8').splitlines():
                try:
                    out['export']['skipped'].append(json.loads(line))
                except Exception:
                    pass
        if (ed / 'contact_sheet.png').exists():
            out['export']['contact_sheet'] = \
                '/files/export_demo/contact_sheet.png'
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _bytes(self, raw, mime, code=200):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            return self._bytes(PAGE.encode(), 'text/html; charset=utf-8')
        if u.path == '/api/state':
            return self._json({'artifacts': artifacts()})
        if u.path == '/api/task':
            q = parse_qs(u.query)
            t = TASKS.get((q.get('id') or [''])[0])
            if not t:
                return self._json({'error': 'not found'}, 404)
            return self._json({'id': t['id'], 'kind': t['kind'],
                               'status': t['status'], 'rc': t['rc'],
                               'log': list(t['log'])[-500:]})
        if u.path.startswith('/files/'):
            return self._serve_file(u.path[len('/files/'):])
        self._json({'error': 'not found'}, 404)

    def _serve_file(self, rel):
        import mimetypes
        full = (RES / unquote(rel)).resolve()
        base = RES.resolve()
        # is_relative_to：跨平台安全的穿越防护（Windows 路径分隔符为反斜杠，不能用 str(base) + '/' 前缀判断）
        if not full.is_relative_to(base) or not full.is_file():
            return self._json({'error': 'forbidden or missing'}, 403)
        mime = mimetypes.guess_type(str(full))[0] or 'application/octet-stream'
        if mime.startswith('text/') or mime in ('application/json',
                                                'application/javascript'):
            mime += '; charset=utf-8'
        return self._bytes(full.read_bytes(), mime)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != '/api/run':
            return self._json({'error': 'not found'}, 404)
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
            tid = kick(body.get('kind'), body)
        except Exception as err:
            return self._json(f'{type(err).__name__}: {err}', 409)
        return self._json({'id': tid})


def main():
    ap = argparse.ArgumentParser(description='数据链控制台')
    ap.add_argument('--port', type=int, default=8788)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'CONSOLE_URL=http://127.0.0.1:{args.port}/', flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
