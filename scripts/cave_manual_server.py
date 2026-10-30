"""手动会话的服务端：起真 neuroglancer（直连）+ 收会话包。

分工（与旧版反向代理方案的区别）：

    旧方案 = 代理 NG 页面，把 recorder 注入到 HTML 里
             → **实测不可用**：页面能开，但从不发起 `/state` 请求
               （Playwright 记录里 0 条），于是 Python 侧改图层永远到不了浏览器
               （`layers=[]` / `position=null`，关键帧全黑）。
    现方案 = NG 页面**直连**（状态通道正常），recorder 由油猴脚本注入；
             本服务只做两件事：起 NG + 收包落盘。

这样也更接近真实用法：真 CAVE 的页面不可能经过我们的代理，只能靠油猴。

用法：

    ../.venv-cavecopilot/bin/python scripts/cave_manual_server.py --port 8899

它会打印三样东西：
    1. VIEWER_URL   —— 真实 neuroglancer 地址（**直连**，端口随机）
    2. SAVE_URL     —— 会话包接收端点
    3. SSH 命令     —— 远程访问要转发的两个端口

然后生成油猴脚本并装到浏览器：

    ../.venv-cavecopilot/bin/python scripts/build_manual_userscript.py \
        --sink http://127.0.0.1:8899/save

保存落到 `results/manual_sessions/<session_id>/`（cave_session_pack 目录包），
回放用 `scripts/replay_manual_session.py --pack <该目录>`。
"""

import argparse
import getpass
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp          # noqa: E402

DIMS = (4, 4, 40)
EM_DEFAULT = ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
              'iarpa_microns/minnie/minnie65/em')
SEG_DEFAULT = ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
               'iarpa_microns/minnie/minnie65/seg')
A_DEFAULT = (190587, 114102, 21773)


_DRAFT_LOCK = threading.Lock()
_SAVE_LOCK = threading.Lock()


def _draft_dir(session_id, root):
    """草稿目录：_draft_<session>。前缀下划线，避免被当成正式会话包。"""
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(session_id or 'session'))
    return Path(root) / ('_draft_' + safe[:80])


def _write_draft(pack, root):
    """增量追加草稿：只写 seq 大于已收的事件，崩溃最多丢最后一次间隔（默认 30s）。"""
    sid = pack.get('session_id')
    if not sid:
        raise ValueError('缺少 session_id')
    d = _draft_dir(sid, root)
    (d).mkdir(parents=True, exist_ok=True)
    meta_path = d / 'draft.json'
    with _DRAFT_LOCK:
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text('utf-8'))
            except Exception:
                meta = {}
        last_seq = int(meta.get('last_seq') or 0)
        evs = [e for e in (pack.get('events') or [])
               if isinstance(e.get('seq'), int) and e['seq'] > last_seq]
        if evs:
            with (d / 'events.jsonl').open('a', encoding='utf-8') as f:
                for e in evs:
                    f.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + '\n')
            last_seq = evs[-1]['seq']
        notes = pack.get('notes') or []
        if notes:
            (d / 'notes.jsonl').write_text(
                '\n'.join(json.dumps(n, ensure_ascii=False, sort_keys=True)
                           for n in notes) + '\n', encoding='utf-8')
        # 图也增量落盘：没传图的草稿只能恢复过程，不能做像素比对
        shots_dir = d / 'shots'
        shots_dir.mkdir(exist_ok=True)
        idx_path = shots_dir / 'shots.jsonl'
        have = set()
        if idx_path.exists():
            for line in idx_path.read_text('utf-8').splitlines():
                try:
                    row = json.loads(line)
                    if row.get('shot_id'):
                        have.add(row['shot_id'])
                except Exception:
                    pass
        n_shots = 0
        for sh in (pack.get('shots') or []):
            sid2 = sh.get('shot_id')
            if not sid2 or sid2 in have:
                continue
            url = sh.get('dataURL') or ''
            if not url.startswith('data:image/'):
                continue
            try:
                head, b64 = url.split(',', 1)
                (shots_dir / (sid2 + '.png')).write_bytes(base64.b64decode(b64))
            except Exception:
                continue
            row = {k: sh.get(k) for k in
                   ('shot_id', 't_wall', 'w', 'h', 'ok', 'note',
                    'capture_status', 'readback_via', 'moment', 'request_id',
                    'state_id', 'state_sha', 'state_in_stream', 'state_age_ms',
                    'state_partial', 'state_truncated', 'paint_settled', 'paint_stable',
                    'paint_changed', 'paint_waited_ms', 'paint_timed_out',
                    'content', 'state')}
            row['image_ref'] = sid2 + '.png'
            with idx_path.open('a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
            have.add(sid2)
            n_shots += 1
        meta.update({
            'session_id': sid,
            'collector_version': pack.get('collector_version'),
            'page': pack.get('page') or {},
            'config': pack.get('config') or {},
            'counts': pack.get('counts') or {},
            'time': pack.get('time') or {},
            'last_seq': last_seq,
            'n_events': last_seq,
            'updated_wall_ms': int(time.time() * 1000),
        })
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                             encoding='utf-8')
    return {'ok': True, 'dir': str(d), 'appended': len(evs),
            'appended_shots': n_shots, 'last_seq': last_seq}


def _drop_draft(session_id, root):
    if not session_id:
        return
    d = _draft_dir(session_id, root)
    try:
        with _DRAFT_LOCK:
            if d.is_dir():
                # 草稿目录里有 shots/ 子目录，逐个 unlink 会抛 IsADirectoryError，
                # 之前的实现因此让“保存成功后删草稿”从未真正发生。
                shutil.rmtree(d)
    except Exception:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    out_root = None
    userscript = None
    console_js = None
    verbose = False
    INDEX = (
        '<!doctype html><meta charset="utf-8"><title>CAVE 采集服务</title>'
        '<body style="font:14px/1.7 ui-monospace,monospace;padding:24px">'
        '<h3>CAVE 手动采集服务</h3>'
        '<p>① 生成并安装油猴脚本：<code>python scripts/build_manual_userscript.py'
        ' --sink http://127.0.0.1:PORT/save</code><br>'
        '② 打开终端里打印的 <b>VIEWER_URL</b><br>'
        '③ 页面上正常校对；点「打检查点」留锚点，点「结束并保存」落盘</p>'
        '<p>本端点：<code>POST /save</code>、<code>GET /ping</code></p></body>')

    INSTALL = (
        '<!doctype html><meta charset="utf-8"><title>安装 CAVE 采集脚本</title>'
        '<body style="font:15px/1.8 system-ui,sans-serif;padding:28px;max-width:720px">'
        '<h2>安装 CAVE 采集脚本</h2>'
        '<p><b>① 先确认浏览器装了油猴</b>（只装一次）：'
        '<a href="https://chromewebstore.google.com/detail/tampermonkey/dhdgffkkebhmkfjojejmpbldmpobfkfo" target="_blank">Chrome 商店</a>'
        ' ｜ <a href="https://microsoftedge.microsoft.com/addons/detail/tampermonkey/iikmkjmpaadaobahmlepeloendndfphd" target="_blank">Edge 商店</a></p>'
        '<p><b>② 点这个链接安装</b>：<a href="/cave_manual.user.js">cave_manual.user.js</a>'
        '（油猴会弹“安装”页，点确认）</p>'
        '<p style="color:#a00"><b>如果你看到的是源代码文本</b>：说明油猴没有生效——'
        '要么还没装（回到 ①），要么扩展被禁用/不在当前浏览器配置里。'
        '这个地址必须由油猴拦截才会变成安装页。</p>'
        '<hr><h3>不想装扩展？</h3>'
        '<p>可以只在控制台跑（<b>代价：关键帧可能没有，只能验位置/尺度</b>）：</p>'
        '<p><button id="cp" style="font-size:15px;padding:6px 14px">复制控制台脚本</button>'
        ' <span id="msg" style="color:#666"></span></p>'
        '<ol><li>打开数据页 → <b>F12</b> → Console</li>'
        '<li>如果提示“不要粘贴代码”，先输入 <code>allow pasting</code> 回车</li>'
        '<li><b>Ctrl+V</b> 粘贴 → 回车；左下角出现「CAVE 采集」即成功</li></ol>'
        '<script>document.getElementById("cp").onclick=async()=>{'
        'const r=await fetch("/console.js");const t=await r.text();'
        'try{await navigator.clipboard.writeText(t);'
        'document.getElementById("msg").textContent="已复制（'+'"+t.length+"'+' 字节），去控制台粘贴";}'
        'catch(e){document.getElementById("msg").textContent="复制失败，请手动打开 /console.js";}};</script>'
        '<hr><p style="color:#666">装好后：回到数据页，左下角出现「CAVE 采集」即成功；'
        '不需要刷新以外的任何操作。</p></body>')

    def log_message(self, fmt, *args):
        if self.verbose:
            sys.stderr.write('[sink] ' + (fmt % args) + '\n')

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    CORS = {'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS'}

    def do_OPTIONS(self):
        self._send(204, b'', 'text/plain', self.CORS)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/ping':
            return self._send(200, b'{"ok":true}', 'application/json', self.CORS)
        if path in ('/', '/index.html'):
            return self._send(200, self.INDEX.encode(), 'text/html; charset=utf-8')
        # 两个入口分工（实测踩过）：
        #   /install            → 说明页（装油猴 / 安装链接 / 看到文本怎么办）
        #   /cave_manual.user.js→ 真正的脚本文件。**必须以 .user.js 结尾**，
        #                         油猴只按这个后缀拦截导航；叫 /install 时它不拦，
        #                         浏览器就把源码原样显示出来。
        if path == '/install':
            return self._send(200, self.INSTALL.encode(), 'text/html; charset=utf-8')
        if path == '/console.js':
            f = self.console_js
            if not f or not Path(f).exists():
                return self._send(404, b'console bundle not built')
            return self._send(200, Path(f).read_bytes(),
                              'application/javascript; charset=utf-8')
        if path == '/cave_manual.user.js':
            f = self.userscript
            if not f or not Path(f).exists():
                return self._send(404, b'userscript not built')
            data = Path(f).read_bytes()
            return self._send(200, data, 'application/javascript; charset=utf-8',
                              {'Content-Disposition': 'inline'})
        return self._send(404, b'not found')

    def do_POST(self):
        path = self.path.split('?')[0]
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n) if n else b''
        if path not in ('/save', '/draft'):
            return self._send(404, b'not found', 'text/plain', self.CORS)
        # 上限按"图多"算：一张 1280x740 PNG ≈ 0.8MB，base64 后 ≈1.1MB。
        # 原来 12MB 只够 ~10 张，长会话会直接 413（用户看到的"保存失败"）。
        limit = 128 * 1024 * 1024
        if len(body) > limit:
            return self._send(413, json.dumps(
                {'error': '包太大：%.1f MB，上限 %d MB'
                          % (len(body) / 1048576.0, limit // 1048576)}
            ).encode(), 'application/json', self.CORS)
        try:
            pack = json.loads(body.decode('utf-8'))
        except Exception as err:
            return self._send(400, json.dumps(
                {'error': 'bad json: %s' % err}).encode(), 'application/json',
                self.CORS)
        if path == '/draft':
            try:
                out = _write_draft(pack, self.out_root)
            except Exception as err:
                return self._send(500, json.dumps(
                    {'error': '%s: %s' % (type(err).__name__, err)}).encode(),
                    'application/json', self.CORS)
            return self._send(200, json.dumps(out, ensure_ascii=False).encode(),
                              'application/json', self.CORS)
        try:
            # 不在这里生成 打开.html：它要读全部事件再渲染表格，
            # 大包会把 /save 拖到前端 20s 超时之外（用户看到"保存请求失败"）。
            # 先把包落盘、把响应发回去，再补 HTML。
            with _SAVE_LOCK:
                d = csp.pack_session(
                    pack, self.out_root, with_html=False,
                    legs={'client_process': 'real'},
                    reasons={'client_process':
                             'recorder 在工作人员浏览器内旁路记录'})
        except Exception as err:
            return self._send(500, json.dumps(
                {'error': '%s: %s' % (type(err).__name__, err)}).encode(),
                'application/json', self.CORS)
        n_ev = len(pack.get('events') or [])
        n_sh = len(pack.get('shots') or [])
        _drop_draft(pack.get('session_id'), self.out_root)
        print('[sink] 已保存 %s（事件 %d / 图 %d）' % (csp.relpath(d), n_ev, n_sh),
              flush=True)
        self._send(200, json.dumps(
            {'ok': True, 'dir': str(d), 'n_events': n_ev,
             'n_shots': n_sh}).encode(), 'application/json', self.CORS)
        # 响应已经发出，再补 打开.html —— 慢也不影响"已保存"。
        # 同一线程里做（不是后台线程）：进程退出时能等到它写完。
        try:
            import cave_session_replay as _csr
            _csr.replay_one(d, out_path=Path(d) / '打开.html', quiet=True)
            print('[sink] 打开.html 已生成', flush=True)
        except Exception as err:      # noqa: BLE001
            print('[sink] 打开.html 未生成：%s: %s'
                  % (type(err).__name__, str(err)[:120]), flush=True)
        return


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def cdp_inject_thread(userscript_path, url_prefix, port=9222, timeout=1800):
    """连到**用户自己浏览器**的调试端口，在 document-start 注入脚本并刷新页面。

    这条路不需要任何扩展：补丁在 `Page.addScriptToEvaluateOnNewDocument` 里执行，
    时机和油猴的 document-start 等价。代价是用户启动 Chrome 时要带
    `--remote-debugging-port=9222`（且 Chrome≥136 要求用独立 profile 目录）。

    连接保持不关闭：这样用户后续刷新页面也仍是注入态。
    """
    def _run():
        from playwright.sync_api import sync_playwright
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with sync_playwright() as pw:
                    b = pw.chromium.connect_over_cdp('http://127.0.0.1:%d' % port)
                    done = False
                    for ctx in b.contexts:
                        ctx.add_init_script(path=str(userscript_path))
                        for pg in ctx.pages:
                            if url_prefix in (pg.url or ''):
                                pg.reload()
                                print('[cdp] 已注入并刷新：%s' % pg.url, flush=True)
                                done = True
                    if done:
                        # 保持连接：后续刷新仍然注入
                        while True:
                            time.sleep(10)
                            try:
                                b.contexts
                            except Exception:
                                print('[cdp] 浏览器已关闭，退出注入线程', flush=True)
                                return
                    b.close()
            except Exception as err:
                if int(time.time() - t0) % 30 < 3:
                    print('[cdp] 等待端口 %d…（%s）'
                          % (port, str(err).splitlines()[0][:80]), flush=True)
            time.sleep(2)
        print('[cdp] 等不到调试端口，放弃', flush=True)

    threading.Thread(target=_run, daemon=True).start()


def _port_free(port):
    """该端口在 wildcard 和 127.0.0.1 上都空吗。

    为什么两者都要看：实测本机 Qoder IDE 占着 `127.0.0.1:8899`，我们绑
    `0.0.0.0:8899` 居然也能成功（SO_REUSEADDR），但浏览器发到
    `http://127.0.0.1:8899/save` 会被**更具体的那个监听**（IDE）接走，于是
    保存永远停在“正在保存…”——绑成功 ≠ 能收包。
    """
    for host in ('', '127.0.0.1'):
        s = socket.socket()
        try:
            s.bind((host, port))
        except OSError:
            return False
        finally:
            s.close()
    return True


def _pick_port(port, tries=20):
    for p in range(port, port + tries):
        if _port_free(p):
            if p != port:
                print('[port] %d 已被占用（可能是 IDE / 旧实例），改用 %d' % (port, p),
                      flush=True)
            return p
    raise SystemExit('端口 %d~%d 全被占用，用 --port 指定一个空端口' %
                     (port, port + tries - 1))


def _pack_root():
    """和 nx.py / 工作台一致的会话根目录。"""
    return Path(os.environ.get('NM_SESSIONS')
                or (ROOT / 'results' / 'manual_sessions')).expanduser()


def _latest_pack(root=None):
    """最新正式包。必须过滤掉 _draft_*/_trash 与没有 manifest 的目录，
    否则 --anchor/--replay 会把草稿当成会话包读。"""
    root = Path(root) if root else _pack_root()
    dirs = [d for d in root.glob('*')
            if d.is_dir() and not d.name.startswith(('.', '_'))
            and (d / 'manifest.json').exists()]
    if not dirs:
        raise SystemExit('还没有会话包；先跑一次采集')
    return max(dirs, key=lambda d: d.stat().st_mtime)


def _url_with_query(url, key, value):
    """把参数加到 query 上，保留 URL 里已有的 ?/# 结构。

    直接 `url + '?cave_session_name=…'` 在 URL 带 `#fragment` 时会把参数写进
    fragment，`location.search` 读不到，会话名就悄悄失效。
    """
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    q.append((key, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(q), parts.fragment))


def _anchor_state(packdir, reason):
    """从会话包里取某个检查点的完整 state（带 position 的才算）。

    用途：想“回到当时那个位置看一眼”时直接恢复，而不是重放一遍输入。
    没带 position 的锚点（NG 还没吐完整 state）不能用于定位，必须过滤。
    """
    pack = csp.load_session(str(packdir))
    anchors = [e for e in (pack.get('events') or [])
               if e.get('type') == 'viewer.checkpoint'
               and (e.get('state') or {}).get('position')]
    for e in reversed(anchors):
        if e.get('reason') == reason:
            return e['state']
    raise SystemExit('包里没有带 position 的检查点 %r；可选：%s'
                     % (reason, sorted({e.get('reason') for e in anchors})))


def _has_desktop():
    """有没有能看、能点的桌面。

    只看 `DISPLAY` / `WAYLAND_DISPLAY` 在 macOS 上是错的：那两变量通常不存在，
    但窗口照样能开——结果 `--open` 在本机永远退化成无头自检（无法操作）。

    优先级：`CAVE_HEADLESS=1`（强制无头）> `CAVE_HEADLESS=0`（强制有头）
    > 平台默认（macOS/Windows 有头；Linux 看 DISPLAY/WAYLAND_DISPLAY）。
    """
    forced = os.environ.get('CAVE_HEADLESS')
    if forced is not None:
        return forced.strip().lower() not in ('1', 'true', 'yes', 'on')
    if sys.platform == 'darwin' or sys.platform.startswith('win'):
        return True
    return bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def _host_save(pg, out_root, session_name=None):
    """页面存不了时由宿主直接取整份包落盘。

    触发条件：页面是 https、保存端点是 http://127.0.0.1（混合内容被拦），
    或 CORS 被挡。这时前端只会看到 `TypeError: Failed to fetch`，
    数据还在内存里——让 playwright 直接 evaluate 拿出来，不经过 HTTP。
    """
    try:
        raw = pg.evaluate('() => window.__caveRecorder.exportWithImagesJSON()')
        pack = json.loads(raw)
        with _SAVE_LOCK:
            d, _m = csp.pack_session(
                pack, out_root, with_html=False,
                legs={'client_process': 'real'},
                reasons={'client_process':
                         'recorder 在工作人员浏览器内旁路记录'})
        # 宿主写入也算正式保存；同 session 的草稿必须一起清掉。
        _drop_draft(pack.get('session_id'), out_root)
        print('[host] 已保存 %s（事件 %d / 图 %d）'
              % (csp.relpath(d), len(pack.get('events') or []),
                 len(pack.get('shots') or [])), flush=True)
        try:
            pg.evaluate('(dir) => { window.__caveSaveRequested = null;'
                        ' window.__caveSavedDir = dir; }', str(d))
        except Exception:
            pass
        try:
            import cave_session_replay as _csr
            _csr.replay_one(d, out_path=d / '打开.html', quiet=True)
        except Exception as err:      # noqa: BLE001
            print('[host] 打开.html 未生成：%s' % str(err)[:100], flush=True)
        return d
    except Exception as err:          # noqa: BLE001
        print('[host] 保存失败：%s: %s'
              % (type(err).__name__, str(err)[:160]), flush=True)
        return None


def open_browser_thread(userscript_path, url, on_close=None, profile=None,
                        session_name=None, out_root=None):
    """本机模式：由我们开一个**真的浏览器窗口**，注入后直接给你用。

    这条路不需要扩展、不需要调试端口、不需要隧道——因为浏览器是本地起的，
    我们在启动时就调 `add_init_script()`，等价油猴的 document-start。
    没有图形界面时自动退化为无头（仅用于验证注入是否生效）。

    `on_close`：窗口被关掉（或浏览器被终止）后回调，用来停掉服务进程——
    所以本机模式的退出方式是"关窗口"，不用记 Ctrl+C。
    """
    def _run():
        from playwright.sync_api import sync_playwright
        headed = _has_desktop()
        try:
            with sync_playwright() as pw:
                # `--window-position`：Playwright 默认不指定位置，实测本机会把窗口丢到
                # 屏幕外（bounds 左边 -162），人看不到还以为没启动。
                # 视口尺寸：必须能装进屏内可视区。写死 1400×900 在 13 寸 Mac
                # （屏 1470×956，扣菜单栏 25px、Dock 和标签栏 ~90px 后窗口最高 867）
                # 上比可视区高，fixed 在底部的「打检查点」面板会被窗口下边裁掉一半。
                # 仍然用固定视口（不用 no_viewport）：devicePixelRatio 才能保持可预测，
                # 回放端按包里的 css_px 建同尺寸上下文。
                win = os.environ.get('CAVE_WINDOW_SIZE', '1280,740')
                vw, vh = (int(x) for x in win.split(','))
                if profile:
                    # 外部站点通常要登录态：用 persistent context 复用用户目录，
                    # 这样 Cookie/SSO 都在，不会落到登录页。
                    ctx = pw.chromium.launch_persistent_context(
                        profile, headless=not headed,
                        args=['--no-sandbox', '--disable-gpu-sandbox'],
                        viewport={'width': vw, 'height': vh})
                    ctx.add_init_script(path=str(userscript_path))
                    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                    b = None
                else:
                    b = pw.chromium.launch(headless=not headed,
                                           args=['--no-sandbox',
                                                 '--disable-gpu-sandbox'])
                    ctx = b.new_context(viewport={'width': vw, 'height': vh})
                    ctx.add_init_script(path=str(userscript_path))
                    pg = ctx.new_page()
                # 会话名随 URL 进页面：recorder 在 start() 时读 cave_session_name
                if session_name:
                    goto_url = _url_with_query(url, 'cave_session_name',
                                               session_name)
                else:
                    goto_url = url
                pg.goto(goto_url)
                pg.wait_for_function('() => !!window.viewer', timeout=60000)
                if headed:
                    # 窗口位置只能用 CDP 定（`--window-position` 实测会被
                    # viewport resize 冲掉，窗口会飘到屏幕外，人以为没启动）
                    try:
                        cdp = ctx.new_cdp_session(pg)
                        wid = cdp.send('Browser.getWindowForTarget')['windowId']
                        cdp.send('Browser.setWindowBounds', {
                            'windowId': wid,
                            'bounds': {'left': 40, 'top': 40,
                                       'windowState': 'normal'}})
                        cdp.detach()
                    except Exception:
                        pass
                    # 不抢占焦点的话，窗口即使存在也躲在别的应用后面
                    try:
                        pg.bring_to_front()
                    except Exception:
                        pass
                patched = pg.evaluate('() => !!window.__caveGLPatched')
                print('[open] 浏览器已打开（%s %s），注入 patched=%s'
                      % ('有界面' if headed else '无头',
                         '视口 ' + win,
                         patched), flush=True)
                if headed:
                    print('[open] 就在这个窗口里操作；**关掉窗口即结束录制并退出**。',
                          flush=True)
                    # 循环等关闭，顺便处理"页面存不了、改由宿主取"的情况。
                    # 为什么会存不了：页面是 https、保存端点是 http://127.0.0.1 时，
                    # 浏览器按混合内容拦掉，前端只会看到 TypeError: Failed to fetch。
                    while True:
                        try:
                            pg.wait_for_timeout(500)
                        except Exception:
                            break          # 窗口关了
                        try:
                            if pg.evaluate('() => !!window.__caveSaveRequested'):
                                _host_save(pg, out_root, session_name)
                        except Exception:
                            break
                else:
                    # 无头只能验证注入是否生效，验完就退出，不留后台进程
                    print('[open] 无图形界面，只做注入自检（20s 后退出）', flush=True)
                    time.sleep(20)
                if b is not None:
                    b.close()
                else:
                    ctx.close()
        except Exception as err:
            print('[open] 打开浏览器失败：%s' % str(err).splitlines()[0][:160],
                  flush=True)
            print('[open] 服务继续运行；可改用 --cdp / 油猴 / Ctrl+C 退出', flush=True)
            return
        if on_close:
            on_close()

    threading.Thread(target=_run, daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='127.0.0.1',
                    help='监听地址；默认只允许本机。局域网直连才用 0.0.0.0')
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--out', default=str(ROOT / 'results' / 'manual_sessions'))
    ap.add_argument('--em', default=EM_DEFAULT)
    ap.add_argument('--seg', default=SEG_DEFAULT)
    ap.add_argument('--pos', default='%d %d %d' % A_DEFAULT)
    ap.add_argument('--dims', default='%d %d %d' % DIMS)
    ap.add_argument('--cross', type=float, default=6.0)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--open', action='store_true',
                    help='本机模式：自动开一个已注入的浏览器窗口（不用扩展、不用 CDP 参数）')
    ap.add_argument('--session-name', default=None,
                    help='会话名（目录名）：results/manual_sessions/<它>/；'
                         '同名已存在时自动加 __<时间戳> 后缀，不覆盖')
    ap.add_argument('--url', default=None,
                    help='直接打开外部 Neuroglancer 链接（不启动本地 demo server）')
    ap.add_argument('--profile', default=None,
                    help='复用浏览器用户目录（外部站点要登录态时用）')
    ap.add_argument('--cdp', action='store_true',
                    help='不装油猴：连你本机 Chrome 的调试端口，注入脚本并刷新页面')
    ap.add_argument('--cdp-port', type=int, default=9222)
    ap.add_argument('--replay', action='store_true',
                    help='不回放采集：直接回放最新的会话包')
    ap.add_argument('--pack', default=None, help='配合 --replay 指定包目录')
    ap.add_argument('--anchor', default=None,
                    help='配合 --pack：启动后直接停在这个检查点的视角（不重放）；'
                         '例：--anchor manual_3；不带 --pack 时用最新包')
    args = ap.parse_args(argv)

    # 避开已被别人占住的端口（否则保存会静悄悄挂起，见 _port_free 注释）
    args.port = _pick_port(args.port)
    if args.host in ('0.0.0.0', '::', ''):
        print('[安全] 监听所有网卡：同一局域网内能访问这个端口；'
              '只在对端可信时这样做。默认是 127.0.0.1。', flush=True)

    if args.replay:
        try:
            pack = Path(args.pack) if args.pack else _latest_pack()
        except SystemExit:
            raise SystemExit('还没有会话包；先跑一次采集再 --replay')
        cmd = [sys.executable, str(HERE / 'replay_manual_session.py'),
               '--pack', str(pack)]
        print('回放 %s' % pack, flush=True)
        raise SystemExit(subprocess.call(cmd))

    child = None
    if args.url:
        # 直开外部 NG：不启本地 demo server，直接用给定 URL。
        url = args.url
        print('[url] 直开外部 NG：%s' % url, flush=True)

        def send(cmd):
            return None

        def setup_layers():
            return None
    else:
        child = subprocess.Popen(
            [sys.executable, '-u', str(HERE / 'cave_local_demo_server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, cwd=str(ROOT))
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
            child.kill()
            raise SystemExit('演示服务没有给出 VIEWER_URL')

        # 子进程后续输出（尤其 `ERR ...`）要透出来，否则只能看到“页面就是空的”
        def _pump():
            for line in child.stdout:
                if line.strip():
                    print('[ng] ' + line.rstrip()[:200], flush=True)
        threading.Thread(target=_pump, daemon=True).start()

        def send(cmd):
            child.stdin.write(cmd + '\n')
            child.stdin.flush()

        def setup_layers():
            send('dims ' + args.dims)
            time.sleep(0.2)
            send('img em ' + args.em)
            time.sleep(0.2)
            send('seg autoseg ' + args.seg)
            time.sleep(0.3)
            send('cross %g' % args.cross)
            send('pos ' + args.pos)

        setup_layers()

    # “回到当时那个位置看一眼”：直接恢复锚点 state，不重放输入。
    if args.anchor:
        if not child:
            print('[anchor] --url 模式没有本地 NG 桥，无法恢复锚点（已忽略）；'
                  '要恢复锚点请用本地 demo 数据或 --pack + studio', flush=True)
        else:
            if args.pack:
                packdir = Path(args.pack)
            else:
                # 没显式给 --out 时用 NM_SESSIONS/默认根；显式 --out 才从那里找。
                default_out = str(ROOT / 'results' / 'manual_sessions')
                packdir = _latest_pack(
                    Path(args.out) if str(args.out) != default_out
                    else _pack_root())
            st = _anchor_state(packdir, args.anchor)
            send('state ' + json.dumps(st))
            print('[anchor] %s @ %s（位置 %s）'
                  % (args.anchor, packdir.name,
                     [round(float(x), 1) for x in st.get('position') or []]),
                  flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    Handler.out_root = out
    Handler.verbose = args.verbose

    # 每次启动都重建油猴脚本（确保补丁/recorder 是最新的），并对外提供
    # /install —— 浏览器打开即弹安装，不用自己找文件、不用粘代码。
    us_path = ROOT / 'results' / 'manual_userscript' / 'cave_manual.user.js'
    con_path = ROOT / 'results' / 'manual_userscript' / 'cave_console.js'
    subprocess.call([sys.executable, str(HERE / 'build_manual_userscript.py'),
                     '--sink', 'http://127.0.0.1:%d/save' % args.port,
                     '--out', str(us_path), '--console', str(con_path)],
                    stdout=subprocess.DEVNULL)
    Handler.userscript = us_path
    Handler.console_js = con_path

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    u = urlparse(url)
    ng_port = u.port or (443 if u.scheme == 'https' else 80)
    ip = _lan_ip()
    print('VIEWER_URL=%s' % url, flush=True)
    print('', flush=True)
    if args.cdp:
        print('================ CDP 模式（不用装油猴）================', flush=True)
        print('1) 先在你电脑上启动 Chrome（独立 profile + 调试端口）：', flush=True)
        print('   google-chrome --remote-debugging-port=%d '
              '--user-data-dir=/tmp/cave-profile' % args.cdp_port, flush=True)
        print('2) 再建隧道（-R 送调试端口上来，-L 带数据页/保存端口下去）：',
              flush=True)
        print('   ssh -R %d:127.0.0.1:%d -L %d:127.0.0.1:%d -L %d:127.0.0.1:%d '
              '%s@%s'
              % (args.cdp_port, args.cdp_port, ng_port, ng_port,
                 args.port, args.port, getpass.getuser(), ip or '<host>'),
              flush=True)
        print('3) 在这个 Chrome 里打开：%s' % url, flush=True)
        print('   （1～3 秒后会自动刷新一次；刷新完左下角出现「CAVE 采集」）',
              flush=True)
        print('4) 操作 -> 「结束并保存」-> python3 scripts/nx.py replay',
              flush=True)
        cdp_inject_thread(us_path, u.path, args.cdp_port)
    else:
        print('================ 照这个做，一共三步 ================', flush=True)
        print('1) 首次安装（点一次，弹“安装”确认；以后不再需要）', flush=True)
        print('   http://127.0.0.1:%d/install        （说明页）' % args.port,
              flush=True)
        print('   http://127.0.0.1:%d/cave_manual.user.js  （直接安装链接）'
              % args.port, flush=True)
        print('2) 打开数据页，正常校对（左下角会出现「CAVE 采集」）', flush=True)
        print('   %s' % url, flush=True)
        print('3) 点面板上的「结束并保存」，然后在本机终端跑：', flush=True)
        print('   python3 scripts/nx.py replay', flush=True)
        print('', flush=True)
        print('远程访问（在你的电脑上先跑这一行，保持开着；本机可忽略）：',
              flush=True)
        print('ssh -N -L %d:127.0.0.1:%d -L %d:127.0.0.1:%d %s@%s'
              % (ng_port, ng_port, args.port, args.port,
                 getpass.getuser(), ip or '<host>'), flush=True)
    print('====================================================', flush=True)

    stop = threading.Event()
    if args.open:
        print('【本机模式】会自动打开一个已注入的浏览器窗口；不用装扩展、不用隧道。',
              flush=True)
        open_browser_thread(us_path, url, on_close=stop.set,
                            profile=args.profile,
                            session_name=args.session_name,
                            out_root=out)
    print('保存目录=%s' % out, flush=True)
    # 服务跑在后台线程，主线程负责等退出信号（关窗口 / Ctrl+C）
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.5},
                     daemon=True).start()
    try:
        if args.open:
            while not stop.is_set():
                stop.wait(0.5)
            print('\n[open] 结束，服务退出（关窗口或自检完成）。', flush=True)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print('\n[sink] 退出', flush=True)
    finally:
        srv.shutdown()
        if child:
            child.kill()


if __name__ == '__main__':
    main()
