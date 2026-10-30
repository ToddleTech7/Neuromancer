"""把 recorder + GPU 补丁 + 面板打成一个**自包含**的油猴脚本。

为什么不用「装三个文件」：油猴脚本没法 `@require` 本地文件，
拆开就得让用户手动改 URL；而真 CAVE 的页面是内网地址，
我们更不能指望它来加载我们的静态文件。所以构建时全部内联，一个文件装完。

用法：

    ../.venv-cavecopilot/bin/python scripts/build_manual_userscript.py \
        --sink http://127.0.0.1:8899/save
    # 产物：results/manual_userscript/cave_manual.user.js

装法（Chrome/Edge + Tampermonkey）：
    扩展图标 → 添加新脚本 → 全选删除 → 粘贴产物内容 → Ctrl+S
    然后刷新 neuroglancer 页面，左下角出现「CAVE 采集」面板即成功。

关键约束（写死在产物里，改不了也不该改）：
  - `@run-at document-start`：GPU 补丁必须赶在 NG 建 WebGL 上下文之前，
    否则关键帧 `toDataURL()` 全是透明（实测过的坑）。
  - 面板等 `window.viewer` 出现后才装 recorder：recorder 需要真 viewer 才能挂。
  - 保存走绝对 URL：油猴跑在 NG 的源上，相对路径会打到那个源。
"""

import argparse
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

HEADER = """// ==UserScript==
// @name         CAVE 操作采集（手动会话）
// @namespace    neuromancer
// @version      0.1.0
// @description  在真 neuroglancer 页面上旁路记录操作（pointer/wheel/key/state/关键帧），
// @description  面板可打检查点、结束并保存到本地服务。只读，不写远端。
// @author       neuromancer
{match}
// @run-at       document-start
// @grant        GM_xmlhttpRequest
{connect}
// ==/UserScript==

/* 由 scripts/build_manual_userscript.py 生成，请勿手改。
 * 组成：
 *   - cave_gpu_patch.js   （document-start：保住 WebGL 帧缓冲）
 *   - cave_recorder.user.js（旁路记录，window.__caveRecorder）
 *   - manual_boot.js      （左下角面板：打检查点 / 结束并保存）
 * 生成时间：{ts}
 * 保存端点：{sink}
 */
"""


def build(sink, matches, out):
    gpu = (HERE / 'cave_gpu_patch.js').read_text(encoding='utf-8')
    rec = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')
    boot = (HERE / 'manual_boot.js').read_text(encoding='utf-8')
    host = urlparse(sink).hostname or '127.0.0.1'
    connect = '\n'.join('// @connect      %s' % h
                        for h in {host, '127.0.0.1', 'localhost'})
    match = '\n'.join('// @match        %s' % m for m in matches)

    body = f"""(function () {{
  'use strict';
  window.__caveSaveUrl = {sink!r};
  window.__caveDraftUrl = {sink!r}.replace(/\/save$/, '/draft');

  /* ---------- 1) GPU 补丁（必须最先跑） ---------- */
{gpu}

  /* ---------- 2) recorder（等 viewer 出现再装） ---------- */
  function __installRecorder() {{
{rec}
  }}

  /* ---------- 3) 面板 ---------- */
  function __bootPanel() {{
{boot}
  }}

  (function waitViewer(n) {{
    if (window.viewer) {{
      try {{ __installRecorder(); }} catch (e) {{ console.error('[cave] recorder', e); }}
      try {{ __bootPanel(); }} catch (e) {{ console.error('[cave] boot', e); }}
      return;
    }}
    if ((n || 0) > 600) {{ console.warn('[cave] 等不到 window.viewer，放弃'); return; }}
    setTimeout(function () {{ waitViewer((n || 0) + 1); }}, 200);
  }})();
}})();
"""
    text = HEADER.format(match=match, connect=connect, sink=sink,
                         ts=__import__('datetime').datetime.now(
                         ).strftime('%Y-%m-%d %H:%M')) + body
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return out, len(text)


def build_console(sink, out):
    """控制台版：不带 GPU 补丁，**不需要任何浏览器扩展**。

    代价说清楚：补丁太晚 ⇒ `toDataURL` 不一定拿得到像素 ⇒ 关键帧可能全是
    `blank_readback`。回放时会自动跳过像素比对，只验位置/尺度/朝向。
    真要“同一张图”还是得装油猴（document-start 注入）。
    """
    rec = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')
    boot = (HERE / 'manual_boot.js').read_text(encoding='utf-8')
    text = f"""/* CAVE 采集（控制台版，无扩展）
 * 用法：打开数据页 → F12 → Console → 粘贴本文件全部内容 → 回车
 * 说明：没有 document-start 补丁，关键帧可能为空；回放会跳过像素比对。
 */
(function () {{
  window.__caveSaveUrl = {sink!r};
  window.__caveDraftUrl = {sink!r}.replace(/\/save$/, '/draft');
  if (window.__caveRecorder && window.__caveRecorder.loaded) {{ return; }}
  function __installRecorder() {{
{rec}
  }}
  function __bootPanel() {{
{boot}
  }}
  (function waitViewer(n) {{
    if (window.viewer) {{
      try {{ __installRecorder(); }} catch (e) {{ console.error('[cave] recorder', e); }}
      try {{ __bootPanel(); }} catch (e) {{ console.error('[cave] boot', e); }}
      return;
    }}
    if ((n || 0) > 600) {{ console.warn('[cave] 等不到 window.viewer'); return; }}
    setTimeout(function () {{ waitViewer((n || 0) + 1); }}, 200);
  }})();
}})();
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return out, len(text)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--sink', default='http://127.0.0.1:8899/save',
                    help='保存端点（cave_manual_server.py 打印的 SAVE_URL）')
    ap.add_argument('--match', action='append', default=None,
                    help='油猴 @match，可重复；默认 *://*/v/*')
    ap.add_argument('--out', default=str(ROOT / 'results' / 'manual_userscript'
                                        / 'cave_manual.user.js'))
    ap.add_argument('--console', default=None,
                    help='额外出一个控制台版（无补丁、无扩展）：给路径')
    args = ap.parse_args(argv)
    matches = args.match or ['*://*/v/*']
    if args.console:
        c_out, c_n = build_console(args.sink, Path(args.console))
        print('已生成控制台版 %s（%.1f KB，无需扩展）' % (c_out, c_n / 1024.0))
    out, n = build(args.sink, matches, Path(args.out))
    print('已生成 %s（%.1f KB）' % (out, n / 1024.0))
    print('装法：Tampermonkey → 添加新脚本 → 全选替换 → 粘贴 → Ctrl+S')
    print('保存端点：%s' % args.sink)
    print('匹配规则：%s' % ', '.join(matches))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
