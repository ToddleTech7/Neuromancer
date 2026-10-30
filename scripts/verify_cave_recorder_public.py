"""公开站点兼容性检查：在公网 Neuroglancer 站点上注入 recorder。

用法（仓库根目录）：
    python scripts/verify_cave_recorder_public.py [url]

默认站点：https://viz.neurodata.io/（与文档一致；可用环境变量 CAVE_PUBLIC_URL 覆盖）

用途：验证 recorder 在"真实公网站点"（非本地前端）上可加载、检测
viewer、记录会话。与 verify_cave_recorder_e2e.py（本地真实数据 + 深度
交互 + 坐标断言）互补：这是兼容性冒烟，交互仅机制级。

如实边界：demo 页面无真实校对操作；结果单独输出
（results/recorder_verify/summary_public_*.json）。
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_util

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cave_session_pack as csp  # noqa: E402
ROOT = HERE.parent
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')

OUT = ROOT / 'results' / 'recorder_verify'
OUT.mkdir(parents=True, exist_ok=True)
TS = datetime.now().strftime('%Y%m%d_%H%M%S')

TARGET = (sys.argv[1] if len(sys.argv) > 1 else
          (os.environ.get('CAVE_PUBLIC_URL') or 'https://viz.neurodata.io/'))

checks = []


def check(name, cond, detail=''):
    checks.append({'name': name, 'ok': bool(cond),
                   'detail': str(detail)[:200]})
    print(f'  [{"PASS" if cond else "FAIL"}] {name}'
          + (f' — {detail}' if detail else ''))


pack_raw = None
info = {'target': TARGET}
try:
    with sync_playwright() as p:
        b = browser_util.launch_browser(p)
        page = b.new_page(viewport={'width': 1280, 'height': 800})
        page.on('pageerror', lambda e: print('[pageerror]', str(e)[:160]))
        page.goto(TARGET, wait_until='domcontentloaded', timeout=90000)
        print('page loaded:', TARGET)

        t0 = time.time()
        page.wait_for_function('() => !!window.viewer', timeout=60000)
        info['viewer_wait_s'] = round(time.time() - t0, 1)
        print('window.viewer: FOUND (wait %ss)' % info['viewer_wait_s'])

        page.add_script_tag(content=RECORDER)
        page.wait_for_function(
            '() => window.__caveRecorder && window.__caveRecorder.loaded',
            timeout=10000)
        print('recorder: injected')

        sid = page.evaluate('() => window.__caveRecorder.start()')
        print('session:', sid)
        time.sleep(1.0)

        # 机制级交互（不涉及数据写入）
        page.mouse.move(640, 400)
        time.sleep(0.4)
        page.mouse.wheel(0, -240)
        time.sleep(0.4)
        page.mouse.down()
        page.mouse.move(700, 440, steps=8)
        page.mouse.up()
        page.keyboard.press('ArrowRight')
        time.sleep(1.0)

        status = page.evaluate('() => window.__caveRecorder.status()')
        print('status:', status)

        page.evaluate('() => window.__caveRecorder.stop()')
        time.sleep(0.3)
        pack_raw = page.evaluate(
            '() => window.__caveRecorder.exportJSON()')
        b.close()
except Exception as err:
    print('FAILED:', type(err).__name__, str(err)[:240])
    info['error'] = f'{type(err).__name__}: {str(err)[:240]}'

counts = {}
if pack_raw:
    pack = json.loads(pack_raw)
    from collections import Counter
    # recorder 已统一用点号事件名；这里必须用 ctype 归一化，否则会把
    # session.start 当成不存在，测试永远失败。
    counts = dict(Counter(csp.ctype(e) for e in pack['events']))
    pack_path = OUT / f'session_public_{TS}.json'
    pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1),
                         encoding='utf-8')
    info['session_file'] = str(pack_path)
    info['viewer_detected'] = pack.get('viewer', {}).get('detected')
    print()
    print('counts:', counts)

check('页面可打开且 recorder 注入', pack_raw is not None,
      info.get('error', 'ok'))
check('window.viewer 检测', info.get('viewer_detected') is True)
check('会话有事件记录', sum(counts.values()) >= 4, counts)
check('session.start/end 成对', counts.get('session.start') == 1
      and counts.get('session.end') == 1)

summary = {
    'ts': TS, 'target': TARGET, 'info': info,
    'counts': counts, 'checks': checks,
    'pass': all(c['ok'] for c in checks),
}
sum_path = OUT / f'summary_public_{TS}.json'
sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                    encoding='utf-8')
print()
print('summary:', csp.relpath(sum_path))
print('RESULT:', 'PASS' if summary['pass'] else 'FAIL')
