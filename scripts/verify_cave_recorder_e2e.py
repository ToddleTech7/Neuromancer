"""E2E 自动化验证：recorder 在真实 Neuroglancer + 真实公开数据上的采集能力。

用法（仓库根目录）：
    python scripts/verify_cave_recorder_e2e.py

架构：
- 子进程起本地 Neuroglancer（scripts/cave_local_demo_server.py，真实前端）
- Python 端经 txn 事务驱动：坐标系 / 视角（voxel 坐标）/ 加载真实数据层
  （MICrONS minnie65 EM，GCS 公开 bucket；须用 precomputed://https:// 形式）
- Playwright chromium(headless；优先自带浏览器，回退系统 Chrome) 打开
  页面，注入 cave_recorder.user.js
- 驱动序列：CB2 样本点定位 + 真实鼠标（滚轮/拖拽）+ 键盘 + 截图 + 备注
- stop 后导出会话包 → results/recorder_verify/

坐标语义注意：Neuroglancer position = voxel 坐标（以 dims.scales 为单位）；
CB2 interface_point_nm (762348,456408,870920) ÷ (4,4,40) = (190587,114102,21773)。

如实边界：真实前端、真实公开数据、真实输入事件；但仍是"机制验证"，
不代表真实校对会话（那属于人工实测阶段）。已知环境限制：headless
SwiftShader 下 canvas readback 可能为空（页面截图不受影响）。
"""

import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import browser_util
import cave_session_pack as csp   # ctype：事件名对齐 RF01–RF15

HERE = Path(__file__).resolve().parent     # scripts/
ROOT = HERE.parent                         # 仓库根
SERVER = HERE / 'cave_local_demo_server.py'
# 本地 demo 服务的输出收进管道按 UTF-8 读，所以让它按 UTF-8 写
# （中文 Windows 缺省是 cp936；只改这个子进程，不动整个进程的环境）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')

OUT = ROOT / 'results' / 'recorder_verify'
SHOTS = OUT / 'shots'
OUT.mkdir(parents=True, exist_ok=True)
SHOTS.mkdir(parents=True, exist_ok=True)
TS = datetime.now().strftime('%Y%m%d_%H%M%S')

EM_SOURCE = ('precomputed://https://storage.googleapis.com/'
             'iarpa_microns/minnie/minnie65/em')
CB2_VOXEL = [190587.0, 114102.0, 21773.0]     # = CB2 nm /(4,4,40)
SECOND_VOXEL = [191000.0, 114800.0, 21800.0]

# 渲染检测区域：左上切片面板内部（避开 UI/3D 面板/高亮框）。
# 实测判别力（2026-09-29）：EM 纹理 std≈25；纯黑帧 0.0；
# 灰底（数据未到）≈1.8；整图 std 会被 UI 元素污染，不可用。
SLICE_CROP = (160, 90, 600, 400)

proc = subprocess.Popen(
    [sys.executable, '-u', str(SERVER)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True, encoding='utf-8', errors='replace', bufsize=1,
    cwd=str(ROOT), env=CHILD_ENV)


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
    print('[server]', line.rstrip())
    m = re.match(r'VIEWER_URL=(\S+)', line.strip())
    if m:
        url = m.group(1)
        break
if not url:
    proc.kill()
    raise SystemExit('未能取到 VIEWER_URL')

checks = []
env_notes = []


def check(name, cond, detail=''):
    checks.append({'name': name, 'ok': bool(cond),
                   'detail': str(detail)[:200]})
    print(f'  [{"PASS" if cond else "FAIL"}] {name}'
          + (f' — {detail}' if detail else ''))


def png_std(png_bytes):
    try:
        import io as _io

        import numpy as np
        from PIL import Image
        im = Image.open(_io.BytesIO(png_bytes)).convert('L')
        arr = np.asarray(im.crop(SLICE_CROP))
        return float(arr.std())
    except Exception:
        return -1.0


def wait_rendered(page, tag, timeout=60, interval=3.0):
    """轮询页面截图，直到切片面板出现 EM 纹理（std 高）或超时。"""
    t0 = time.time()
    best = 0.0
    snap = SHOTS / f'.render_{tag}.png'
    while time.time() - t0 < timeout:
        page.screenshot(path=str(snap))
        raw = snap.read_bytes()
        st = png_std(raw)
        best = max(best, st)
        print(f'    [render {tag}] waited={time.time()-t0:.0f}s std={st:.1f}')
        if st > 8.0:
            return {'ok': True, 'std': round(st, 1),
                    'waited_s': round(time.time() - t0, 1)}
        time.sleep(interval)
    return {'ok': False, 'std': round(best, 1),
            'waited_s': round(time.time() - t0, 1)}


def grab_shot(page, tag):
    res = page.evaluate("""async () => {
        const rec = window.__caveRecorder;
        const meta = await rec.captureShot();
        const last = rec.shots[rec.shots.length - 1] || {};
        return {meta: meta, dataURL: last.dataURL || null,
                note: last.note || ''};
    }""")
    info = {'meta': res['meta'], 'note': res['note']}
    du = res['dataURL']
    if du and du.startswith('data:image/png;base64,'):
        import base64
        raw = base64.b64decode(du.split(',', 1)[1])
        fn = SHOTS / f'recorder_{tag}_{TS}.png'
        fn.write_bytes(raw)
        info['file'] = str(fn)
        info['std'] = round(png_std(raw), 1)
    return info


shots_info = {}
render_info = {}
pack_raw = None
try:
    with sync_playwright() as p:
        b = browser_util.launch_browser(p)
        page = b.new_page(viewport={'width': 1280, 'height': 800})
        page.on('pageerror', lambda e: print('[pageerror]', str(e)[:180]))
        page.on('dialog',
                lambda d: d.accept('e2e自动化备注：定位到样本点'))
        page.goto(url, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_function('() => !!window.viewer', timeout=30000)
        print('window.viewer: FOUND')

        page.add_script_tag(content=RECORDER)
        page.wait_for_function(
            '() => window.__caveRecorder && window.__caveRecorder.loaded',
            timeout=10000)
        print('recorder: injected')

        sid = page.evaluate('() => window.__caveRecorder.start()')
        print('session:', sid)
        time.sleep(0.8)

        # 1) 坐标系 + 真实数据层 + CB2 样本点（voxel 坐标）
        send('dims 4 4 40')
        time.sleep(0.6)
        send(f'img em {EM_SOURCE}')
        time.sleep(0.5)
        send('pos 190587 114102 21773')
        render_info['pos1'] = wait_rendered(page, 'pos1')
        print('render pos1:', render_info['pos1'])

        shots_info['page1'] = str(SHOTS / f'page_pos1_{TS}.png')
        page.screenshot(path=shots_info['page1'])
        shots_info['recorder1'] = grab_shot(page, 'pos1')
        print('shot1:', json.dumps(shots_info['recorder1'],
                                   ensure_ascii=False)[:240])

        # 2) 真实鼠标：滚轮缩放
        page.mouse.move(640, 400)
        page.mouse.wheel(0, -480)
        time.sleep(1.0)

        # 3) 真实鼠标：拖拽
        page.mouse.down()
        page.mouse.move(760, 470, steps=14)
        page.mouse.up()
        time.sleep(1.0)

        # 4) 真实键盘
        for key in ('ArrowRight', 'ArrowLeft', 'KeyZ'):
            page.keyboard.press(key)
            time.sleep(0.2)
        time.sleep(1.0)

        # 5) 截图（交互后）
        shots_info['page2'] = str(SHOTS / f'page_after_{TS}.png')
        page.screenshot(path=shots_info['page2'])
        shots_info['recorder2'] = grab_shot(page, 'after')
        print('shot2:', json.dumps(shots_info['recorder2'],
                                   ensure_ascii=False)[:240])

        # 6) 备注（dialog 自动 accept）
        page.evaluate('() => window.__caveRecorder.addNote()')
        time.sleep(0.6)

        # 7) 第二次程序化视角
        send('pos 191000 114800 21800')
        send('cross 2.0')
        time.sleep(2.0)

        status = page.evaluate('() => window.__caveRecorder.status()')
        print('status:', status)

        # 7.5) D10 采样开关：同一毫秒内连发 200 个 pointermove。
        # 默认 50ms 采样 → 只应落 1 条；改成 0（全量）→ 应落 200 条。
        # 这是“粒度可配”这个功能本身的验收，不依赖人手速度。
        BURST = """() => { const c = document.querySelector('canvas');
          for (let i = 0; i < 200; i++) {
            c.dispatchEvent(new PointerEvent('pointermove', {
              bubbles: true, clientX: 600 + (i % 10), clientY: 400,
              pointerId: 1, pointerType: 'mouse'}));
          }
          return window.__caveRecorder.events
            .filter(e => e.type === 'input.pointer').length; }"""
        sampling = {}
        sampling['n0'] = page.evaluate(BURST)
        time.sleep(0.3)
        sampling['n50ms'] = page.evaluate(BURST) - sampling['n0']
        time.sleep(0.3)
        page.evaluate('() => window.__caveRecorder.setConfig({move_sample_ms: 0})')
        n_before = page.evaluate(BURST)
        time.sleep(0.3)
        sampling['nfull'] = page.evaluate(BURST) - n_before
        print('sampling:', sampling)

        page.evaluate('() => window.__caveRecorder.stop()')
        time.sleep(0.4)
        pack_raw = page.evaluate(
            '() => window.__caveRecorder.exportJSON()')
        b.close()
finally:
    proc.kill()

pack = json.loads(pack_raw)
pack_path = OUT / f'session_e2e_{TS}.json'
pack_path.write_text(json.dumps(pack, ensure_ascii=False, indent=1),
                     encoding='utf-8')

ev = pack['events']
# recorder v0.7 起事件名对齐需求清单（点号命名），旧名靠 legacy_type 兼容。
# 这里统一用 ctype 归一化，否则旧包/新包要写两套断言。
types = [csp.ctype(e) for e in ev]
cnt = Counter(types)
print()
print('events:', len(ev), '| counts:', dict(cnt))

# ---- 断言 ----
check('会话包 schema', pack.get('schema') in (
    'cave_recorder.session.v0.1', 'cave_recorder.session.v0.2'),
      pack.get('schema'))
check('viewer 检测', pack.get('viewer', {}).get('detected') is True)
check('首事件为 session.start',
      bool(ev) and csp.ctype(ev[0]) == 'session.start'
      and ev[0].get('viewer_detected') is True)
# D10：采样粒度必须写进 session.start，且**默认值不能被"没给参数"改成 0**
# （Number(null)===0 这个坑实测踩过一次，会把 50ms 默默变全量）
_cfg = (ev[0].get('config') if ev else None) or {}
check('session.start 带采样配置（D10，默认 50ms）',
      _cfg.get('move_sample_ms') == 50
      and _cfg.get('move_mode') == 'sampled'
      and _cfg.get('wheel_burst_gap_ms') == 400
      and _cfg.get('endpoints') == 'full', _cfg)
# D10 粒度可配：同一毫秒 200 连发 → 50ms 采样应只落 1 条，全量应落 200 条
check('D10 采样生效：50ms 下 200 连发只落 1 条',
      sampling.get('n50ms') == 1, sampling)
check('D10 全量模式：200 连发全落',
      sampling.get('nfull') == 200, sampling)
check('运行中改粒度留痕（collector.status.config_change）',
      any(csp.ctype(e) == 'collector.status' and e.get('config_change')
          for e in ev))

check('viewer.state-change >= 5', cnt.get('viewer.state-change', 0) >= 5,
      cnt.get('viewer.state-change', 0))
# 复刻的前提：每个状态变化都带完整 state（不是摘要），否则只能恢复到 checkpoint。
_sc = [e for e in ev if csp.ctype(e) == 'viewer.state-change']
_with = [e for e in _sc if isinstance(e.get('state'), dict)]
check('每个 state-change 都带完整 state（可任意时刻恢复）',
      len(_sc) >= 5 and len(_with) == len(_sc),
      f'{len(_with)}/{len(_sc)}')
check('完整 state 有去重指纹与字节数',
      all(e.get('state_sha') for e in _with)
      and all((e.get('state_bytes') or 0) > 0 for e in _with),
      sorted({e.get('state_bytes') for e in _with})[:3])
_full = [e for e in _with if not e.get('state_partial')]
_partial = [e for e in _with if e.get('state_partial')]
check('可直接恢复的 state（position+layers+dimensions+layout）>= 5',
      len(_full) >= 5, f'{len(_full)} 完整 / {len(_partial)} 不完整')
# 不完整的必须**打标**，不能默默当成可用；且只应出现在刚开页那一瞬
check('不完整的 state 都带 state_partial 去因',
      all(isinstance(e.get('state_partial'), str) and e['state_partial']
          for e in _partial),
      [e.get('state_partial') for e in _partial][:3])
check('input.pointer >= 2', cnt.get('input.pointer', 0) >= 2,
      cnt.get('input.pointer', 0))
check('input.wheel >= 1', cnt.get('input.wheel', 0) >= 1,
      cnt.get('input.wheel', 0))
check('input.key >= 3', cnt.get('input.key', 0) >= 3,
      cnt.get('input.key', 0))
# 守“字段采了但永远是空”：inputEventBindings 是
# {global, sliceView, perspectiveView} 三个 Map，键形如 at:arrowright；
# 写成 binds.get(...) 会永远拿不到 action。
_key_ev = [e for e in ev if csp.ctype(e) == 'input.key']
check('input.key 带绑定语义（action 非空）',
      any((e.get('action') or '') for e in _key_ev),
      [(e.get('key'), e.get('action'), e.get('action_section'))
       for e in _key_ev][:6])
check('viewport.gesture >= 1（RF02/PP02 机械切分）',
      cnt.get('viewport.gesture', 0) >= 1,
      cnt.get('viewport.gesture', 0))
check('evidence.bookmark >= 1', cnt.get('evidence.bookmark', 0) >= 1)
check('session.end 恰好一次', types.count('session.end') == 1)
check('真实数据渲染出现（切片面板纹理）',
      render_info.get('pos1', {}).get('ok') is True, render_info.get('pos1'))

samples = [e['viewpoint'] for e in ev
           if csp.ctype(e) == 'viewer.state-change']
positions = [s.get('position') for s in samples
             if s.get('position')
             and all(isinstance(v, (int, float))
                     for v in s.get('position'))]


def near(p, target, tol=0.5):
    try:
        return bool(p) and all(abs(float(a) - b) <= tol
                               for a, b in zip(p, target))
    except (TypeError, ValueError):
        return False


check('CB2 样本点 voxel [190587,114102,21773] 入档',
      any(near(p, CB2_VOXEL) for p in positions))
check('第二视角 [191000,114800,21800] 入档',
      any(near(p, SECOND_VOXEL) for p in positions))

crosses = [s.get('crossSectionScale') for s in samples
           if s.get('crossSectionScale') is not None]
check('缩放值出现多次变化（滚轮/交互生效）',
      len({round(c, 4) for c in crosses}) >= 2,
      sorted({round(c, 4) for c in crosses})[:10])

notes = pack.get('notes') or []
check('备注文本匹配',
      bool(notes) and 'e2e自动化备注' in notes[0].get('text', ''))
check('recorder errors 为空', len(pack.get('errors') or []) == 0,
      pack.get('errors'))
rec1 = shots_info.get('recorder1', {})
# v0.6：captureShot 返回 capture_status；headless 下 readback 会返回纯白，
# 正确行为是如实标 blank_readback（而不是把空白当成功）。
_status = (rec1.get('meta') or {}).get('status')
# painted_partial 也是合法状态：图拍到了，但那一刻画面还没画完（无头下常见）。
# 它必须被如实标出，而不是伪装成 captured——这条断言本来就是查“有没有如实标”。
check('recorder 截图接口状态如实（captured/blank_readback/painted_partial）',
      _status in ('captured', 'blank_readback', 'too_small', 'no_canvas',
                  'painted_partial'),
      _status)
if _status == 'captured':
    check('recorder 截图为真实图像', bool((rec1.get('meta') or {}).get('ok')))
if rec1.get('std') is not None and rec1['std'] < 1.0:
    env_notes.append('headless SwiftShader 下 recorder canvas readback '
                     '为空白（页面截图正常，已标 blank_readback）；'
                     '真实浏览器待人工验证')

timeline = []
t0 = ev[0]['t_wall'] if ev else 0
for e in ev:
    if csp.ctype(e) != 'viewer.state-change':
        continue
    vp = e['viewpoint']
    timeline.append({
        't_rel_s': round((e['t_wall'] - t0) / 1000.0, 2),
        'position': vp.get('position'),
        'cross': vp.get('crossSectionScale'),
    })

summary = {
    'ts': TS, 'url': url, 'session_id': pack.get('session_id'),
    'mode': 'local-real-frontend + MICrONS minnie65 EM (GCS https)',
    'data_source': EM_SOURCE,
    'cb2_voxel': CB2_VOXEL,
    'render_wait': render_info,
    'events': len(ev), 'counts': dict(cnt),
    'timeline_state_samples': timeline,
    'shots': shots_info,
    'files': {'session': str(pack_path)},
    'checks': checks,
    'pass': all(c['ok'] for c in checks),
    'env_notes': env_notes,
    'recorder_errors': pack.get('errors'),
}
sum_path = OUT / f'summary_e2e_{TS}.json'
sum_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                    encoding='utf-8')
print()
print('session pack:', pack_path)
print('summary:', csp.relpath(sum_path))
print('RESULT:', 'PASS' if summary['pass'] else 'FAIL')
