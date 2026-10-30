"""真机验证：指针下的分割段 ID（`viewer.focus.picked` 到底能不能取到）。

用法（仓库根目录，**会弹浏览器窗口**）：
    python3 scripts/verify_mouse_pick.py              # 有头，默认 90 秒
    python3 scripts/verify_mouse_pick.py --seconds 120
    python3 scripts/verify_mouse_pick.py --headless   # 无头对照（预期全 0）

为什么要单独一个脚本：
    无头（SwiftShader）下 `mouseState.pickedValue` 恒为 `0`，扫 30 个点也全是 0，
    `pickedRenderLayer` 恒 null —— 疑与 GPU pick 缓冲读不到有关。
    所以“`picked` 能采到”这件事**只能有头 + 真 GPU 才能证**。

它会做什么：
    1. 开窗口，打开公开 minnie65（EM + 分割，开着色，方便看出哪里是段）；
    2. 右上角叠一个 HUD，实时显示 `pickedValue` / `pickedRenderLayer` / 命中计数；
    3. 你把鼠标移到**彩色分割区域**上，HUD 变绿就说明取到了；
    4. 时间到（或 Ctrl+C）自动输出报告。

如实边界：这是**现场验证工具**，不写任何远端数据；它只读 `mouseState`。
"""

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp  # noqa: E402

from browser_util import launch_browser       # noqa: E402

OUT = ROOT / 'results' / 'mouse_pick'

# 公开 minnie65：EM + 分割，开着色（每段一色，肉眼能看出哪里是段）。
#
# **坐标坑（踩过）**：minnie65 的 precomputed 源带 `voxel_offset`，
# 最高分辨率层 res=[8,8,40]nm、voxel_offset=[13824,13824,14816]，即实际起点是
#   x/y ≥ 13824*8 = 110592 nm，z ≥ 14816*40 = 592640 nm。
# 用 (100000,55000,20000) 会完全落在数据外 —— 画面全黑、`picked` 恒 0，
# 看起来很像“pick 坏了”，其实是自己没在数据里。下面用的是各轴中点。
URL = (
    'https://viz.neurodata.io/#!'
    '{"dimensions":{"x":[4e-9,"m"],"y":[4e-9,"m"],"z":[4e-8,"m"]},'
    '"position":[960000,830000,850000],"crossSectionScale":8,"layout":"xy",'
    '"layers":{'
    '"img":{"type":"image","name":"img",'
    '"source":"precomputed://https://bossdb-open-data.s3.amazonaws.com'
    '/iarpa_microns/minnie/minnie65/em"},'
    '"seg":{"type":"segmentation","name":"seg",'
    '"source":"precomputed://https://bossdb-open-data.s3.amazonaws.com'
    '/iarpa_microns/minnie/minnie65/seg",'
    '"baseSegmentColoring":true'
    '}}}'
)

HUD_JS = """
() => {
  const id = '__pickHud';
  let el = document.getElementById(id);
  if (!el) {
    el = document.createElement('div');
    el.id = id;
    el.style.cssText = [
      'position:fixed', 'right:12px', 'top:12px', 'z-index:2147483647',
      'font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace',
      'background:rgba(0,0,0,.82)', 'color:#eee', 'padding:10px 12px',
      'border-radius:8px', 'pointer-events:none', 'white-space:pre',
      'border:1px solid #444', 'min-width:230px'
    ].join(';');
    document.body.appendChild(el);
  }
  const ms = window.viewer && window.viewer.mouseState;
  let picked = null, miss = false, layer = null, page = null;
  if (ms) {
    try {
      const pv = ms.pickedValue;
      picked = pv == null ? null : String(pv);
      if (picked === '0') { miss = true; picked = null; }
    } catch (e) { picked = 'ERR'; }
    layer = ms.pickedRenderLayer ? (ms.pickedRenderLayer.name || '?') : null;
    page = (ms.pageX != null) ? [Math.round(ms.pageX), Math.round(ms.pageY)] : null;
  }
  const ok = !!(picked && picked !== 'ERR');
  el.style.borderColor = ok ? '#2ecc71' : '#444';
  el.textContent =
    (ok ? '✅ 命中' : (miss ? '· 没压到段' : '· 无值')) + '\\n' +
    'picked  : ' + (picked || (miss ? '0（未命中）' : 'null')) + '\\n' +
    'layer   : ' + (layer || 'null') + '\\n' +
    'page    : ' + (page ? page.join(', ') : '—') + '\\n' +
    '—— 把鼠标移到彩色分割区域上 ——';
  return {picked: picked, miss: miss, layer: layer, page: page};
}
"""


def main():
    ap = argparse.ArgumentParser(description='真机验证 picked（指针下的段 ID）')
    ap.add_argument('--seconds', type=int, default=90, help='采集多少秒')
    ap.add_argument('--headless', action='store_true',
                    help='无头对照（预期全是 0 / null）')
    ap.add_argument('--auto', action='store_true',
                    help='自动在画布上扫点（不想手动移鼠标时用）')
    ap.add_argument('--url', default=URL, help='要打开的 NG 链接')
    args = ap.parse_args()

    samples = []
    hits = []
    t_end = time.time() + args.seconds

    print('=' * 62)
    print('模式：%s' % ('无头（对照）' if args.headless else '有头（真窗口）'))
    print('窗口会打开并加载公开 minnie65（EM + 分割，开着色）。')
    print('请把鼠标**移到彩色的分割区域**上，右上角 HUD 变绿就是取到了。')
    print('采集 %d 秒，Ctrl+C 可提前结束。' % args.seconds)
    print('=' * 62)

    with sync_playwright() as pw:
        b = launch_browser(pw, headless=args.headless)
        ctx = b.new_context(viewport={'width': 1280, 'height': 820})
        pg = ctx.new_page()
        pg.goto(args.url, wait_until='load', timeout=90000)
        pg.wait_for_function('() => window.viewer && window.viewer.state',
                            timeout=90000)
        print('[ok] viewer 已就绪，等 8s 让瓦片/分割加载…')
        pg.wait_for_timeout(8000)
        # 先确认画面真的画出东西了——否则“全 0”只能说明环境没加载，
        # 不能归因给 pick（这个错误我们犯过一次）。
        try:
            drawn = pg.evaluate("""() => {
              const cs = [...document.querySelectorAll('canvas')]
                .filter(c => c.width > 100 && c.height > 100);
              return cs.length;
            }""")
            if not drawn:
                print('[warn] 找不到 canvas，结果不可信')
        except Exception:
            pass

        try:
            if args.auto:
                # 自动扫格：只为了证明“picked 能不能拿到”，不依赖人操作
                box = pg.evaluate("""() => {
                  const c = document.querySelector('canvas');
                  const r = c ? c.getBoundingClientRect() : null;
                  return r ? [r.x, r.y, r.width, r.height] : null;
                }""")
                if not box:
                    print('[warn] 找不到 canvas，改为读取当前指针状态')
                else:
                    x0, y0, w, h = box
                    for gi in range(10):
                        for gj in range(7):
                            if time.time() >= t_end:
                                break
                            px = x0 + w * (0.06 + gi * 0.098)
                            py = y0 + h * (0.08 + gj * 0.13)
                            pg.mouse.move(px, py)
                            pg.wait_for_timeout(320)
                            r = pg.evaluate(HUD_JS)
                            if r is None:
                                break
                            samples.append(r)
                            if r.get('picked') and r.get('picked') != 'ERR':
                                hits.append(r)
                                print('  \u2705 picked=%s layer=%s page=%s'
                                      % (r['picked'], r.get('layer'),
                                         r.get('page')))
            while time.time() < t_end:
                try:
                    r = pg.evaluate(HUD_JS)
                except Exception as err:
                    print('[warn] 页面可能已关闭：%s' % str(err)[:80])
                    break
                if r is None:
                    break
                samples.append(r)
                if r.get('picked') and r.get('picked') != 'ERR':
                    hits.append(r)
                    print('  ✅ 命中 picked=%s layer=%s page=%s'
                          % (r['picked'], r.get('layer'), r.get('page')))
                pg.wait_for_timeout(250)
        except KeyboardInterrupt:
            print('\n[..] 手动结束')

        n_layers = 0
        try:
            n_layers = pg.evaluate(
                '() => (window.viewer.state.toJSON().layers || []).length')
        except Exception:
            pass
        b.close()

    print()
    print('样本 %d 个，命中 %d 个，层数 %d' % (len(samples), len(hits), n_layers))
    if hits:
        uniq = {}
        for h in hits:
            uniq[h['picked']] = uniq.get(h['picked'], 0) + 1
        print('命中的段（前 8 个）：')
        for pid, n in sorted(uniq.items(), key=lambda x: -x[1])[:8]:
            print('  %s  ×%d' % (pid, n))
        verdict = 'PASS'
        note = ('picked 在真机可取到：%d 个不同段' % len(uniq))
    else:
        verdict = 'NO_EVIDENCE'
        note = ('没有命中。若是无头模式，这与已知一致（pickedValue 恒 0）；'
                '若是**有头**模式，可能是：指针一直在细胞外空白处 / '
                '分割层还没画完 / 本构建没有开 pick')
    print()
    print('判定：%s —— %s' % (verdict, note))

    OUT.mkdir(parents=True, exist_ok=True)
    summary = {
        'ts': time.strftime('%Y%m%d_%H%M%S'),
        'headless': bool(args.headless),
        'url': args.url,
        'n_samples': len(samples),
        'n_hits': len(hits),
        'n_layers': n_layers,
        'unique_segments': sorted({h['picked'] for h in hits}),
        'verdict': verdict, 'note': note,
        'samples_by_page': [s.get('page') for s in samples[:200]],
    }
    (OUT / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    print('summary: %s' % csp.relpath(OUT / 'summary.json'))
    return 0 if verdict == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
