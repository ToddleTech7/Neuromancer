"""动作回放自检：录下**真人输入**（拖拽/滚轮/点击），在新会话里**重放这些输入**，
看能不能把视角带到同一处、画出同一幅画面。

与另外两个的分工（别混）：

  verify_replay_fidelity   重放 **state**（假定 state 已知）→ 只验渲染一致性
  cave_replay_viewer       逐点恢复 state + 写门禁（人看）
  verify_action_replay     重放 **输入事件**（不喂 state）→ 验“动作可复现”

为什么必须单独验：state 回放能对上，只说明“给我最终视角我能画出来”；
真问题是“把同一个人的同一次拖动再放一遍，会不会停在同一个地方”。
两者的失败原因不同（前者是渲染/数据，后者是输入语义/动画/惯性），不能互相代替。

做法：
  1) 录制页：真 neuroglancer（公开 minnie65 EM+seg）+ 真鼠标事件（playwright 打的是
     浏览器级可信事件，不是 JS dispatch），每段动作前后各钉一个 checkpoint（带完整 state）。
  2) 回放页：**全新页面**，先恢复“动作前”的 state，再按原相对时间把记录到的
     pointer/wheel 事件重放一遍；**不喂最终 state**。
  3) 逐段比对：动作后的位置/尺度/朝向/图层 vs 录下来的那一段；最后再比一张像素。

如实边界（写清楚，不当成通过）：
  - 滚轮在 neuroglancer 里是**动画**（有惯性/插值），事件重放不可能位级一致，
    所以尺度给相对容差（默认 3%），并单独列 `wheel_drift`。
  - 只重放 pointer/wheel；键盘快捷键与工具切换未纳入（先验主干动作）。
  - 公开栈的分割是“当前版本”，像素对比高不等于当时那版数据。

用法：
    python3 scripts/verify_action_replay.py
    python3 scripts/verify_action_replay.py --headed --out results/action_replay
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# 本地 demo 服务的输出收进管道按 UTF-8 读，所以让它按 UTF-8 写
# （中文 Windows 缺省是 cp936；只改这个子进程，不动整个进程的环境）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}

import browser_util                     # noqa: E402
import cave_session_pack as csp          # noqa: E402
import replay_state as rsq               # noqa: E402
from verify_replay_fidelity import (     # noqa: E402
    CMP_JS, PAINT_JS, READBACK_JS, POS_JS)

SERVER = HERE / 'cave_local_demo_server.py'
GPU_PATCH = HERE / 'cave_gpu_patch.js'
RECORDER = (HERE / 'cave_recorder.user.js').read_text(encoding='utf-8')

DIMS = (4, 4, 40)                       # minnie65 公开栈的体素尺寸（nm）
EM_SOURCE = ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
             'iarpa_microns/minnie/minnie65/em')
SEG_SOURCE = ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
              'iarpa_microns/minnie/minnie65/seg')
A = (190587, 114102, 21773)             # CB2 样本点（与 demo 同一处）

# 动作回放的容差：拖动/点击应位级一致；滚轮是动画，给相对容差。
POS_TOL_NM = 60.0                       # ≈ 15 体素@4nm，远小于一次拖动的位移(≈1µm)
CLICK_TOL_NM = 5.0                      # 点击不该改视角
SCALE_RTOL = 0.03


# ---------------------------------------------------------------- 录制
def record(page, send):
    """在真页面上做一组真输入，返回 (pack, anchors)。"""
    from demo_capture_episodes import wait_painted   # 复用同一套“等画完”

    send('dims %d %d %d' % DIMS)
    time.sleep(0.4)
    send('img em ' + EM_SOURCE)
    time.sleep(0.4)
    send('seg autoseg ' + SEG_SOURCE)
    time.sleep(0.4)
    send('cross 6')
    send('pos %d %d %d' % tuple(A))
    time.sleep(2.0)
    wait_painted(page, 'record_start')

    page.add_script_tag(content=RECORDER)
    page.wait_for_function(
        '() => window.__caveRecorder && window.__caveRecorder.loaded',
        timeout=10000)
    # 动作回放要的是**完整**输入流：50ms 采样会把拖动抽稀，重放必然对不上。
    page.evaluate('() => window.__caveRecorder.setConfig({move_sample_ms: 0})')
    page.evaluate('() => window.__caveRecorder.setConfig('
                  '{keyframe_on_checkpoint: false})')
    page.evaluate('() => window.__caveRecorder.start()')
    time.sleep(0.6)

    def anchor(reason):
        page.evaluate('(r) => window.__caveRecorder.checkpointNow(r)', reason)
        time.sleep(0.15)

    cx, cy = 350, 225          # 4panel 布局里 xy 面板的中心（避开面板边界）
    anchor('pre')

    # 1) 拖动：12 步（每步 20px），是最能代表“人在找结构”的动作
    page.mouse.move(cx, cy)
    page.mouse.down()
    for i in range(1, 13):
        page.mouse.move(cx + i * 20, cy + i * 8)
        time.sleep(0.04)
    page.mouse.up()
    time.sleep(0.5)
    anchor('g_drag1')

    # 2) 带 Ctrl 的滚轮缩放：实测这个构建里**裸滚轮不动尺度**（crossSectionScale 恒定），
    #    缩放绑定在 ctrl+wheel 上（6 → 0.99）。这正好说明“重放必须重放修饰键”。
    page.mouse.move(cx, cy)
    page.keyboard.down('Control')
    for _ in range(4):
        page.mouse.wheel(0, -120)
        time.sleep(0.20)
    page.keyboard.up('Control')
    time.sleep(0.9)
    anchor('g_wheel')

    # 3) 点击（点选分割）：不该改变视角
    page.mouse.click(cx + 40, cy + 20)
    time.sleep(0.6)
    anchor('g_click')

    # 4) 反向拖动：检验“方向敏感”不会把偏差累积成单调漂移
    page.mouse.move(cx, cy)
    page.mouse.down()
    for i in range(1, 9):
        page.mouse.move(cx - i * 15, cy - i * 6)
        time.sleep(0.04)
    page.mouse.up()
    time.sleep(0.5)
    anchor('g_drag2')

    # 5) 键盘：方向键平移（x±/y±）、逗号句号改切片（z±）、z 吸附。
    #    这些都能在 state.position 上看到，所以能逐位验；而且它们是把“动作”
    #    从“鼠标动作”拓到“键盘语义”的最小集合。
    page.mouse.move(cx, cy)          # 焦点必须落在画布上
    for _ in range(4):
        page.keyboard.press('ArrowRight')
        time.sleep(0.08)
    for _ in range(3):
        page.keyboard.press('Period')
        time.sleep(0.08)
    for _ in range(2):
        page.keyboard.press('ArrowUp')
        time.sleep(0.08)
    page.keyboard.press('KeyZ')      # snap
    time.sleep(0.6)
    anchor('g_keys')

    wait_painted(page, 'record_end')
    page.evaluate("() => window.__caveRecorder.keyframe('action_end')")
    time.sleep(0.3)
    page.evaluate('() => window.__caveRecorder.stop()')
    pack = json.loads(page.evaluate(
        '() => window.__caveRecorder.exportWithImagesJSON()'))
    anchors = {e.get('reason'): e.get('state')
               for e in pack.get('events') or []
               if csp.ctype(e) == 'viewer.checkpoint' and e.get('state')}
    return pack, anchors


# ---------------------------------------------------------------- 重放
def inputs_of(pack):
    """按顺序取出要重放的输入事件（pointer / wheel / key）。

    纯修饰键（Control/Shift/...）的 keydown 不算独立动作：它们的语义已经
    包含在 wheel/pointer 事件的 `modifiers` 里，单独重放会把开关按乱。
    """
    MOD_ONLY = {'Control', 'Shift', 'Alt', 'Meta'}
    out = []
    for e in pack.get('events') or []:
        t = csp.ctype(e)
        if t == 'input.key' and (e.get('key') in MOD_ONLY
                                 or e.get('code') in MOD_ONLY):
            continue
        if t in ('input.pointer', 'input.wheel', 'input.key'):
            out.append(e)
    return out


def groups_of(pack, anchors):
    """把输入事件按“两个 checkpoint 之间”切段，便于逐段比对。"""
    ev = pack.get('events') or []
    marks = [(e.get('t_wall'), e.get('reason'))
             for e in ev if csp.ctype(e) == 'viewer.checkpoint'
             and e.get('reason') in anchors]
    marks.sort(key=lambda x: x[0] or 0)
    seq = inputs_of(pack)
    groups, prev = [], marks[0][0] if marks else 0
    for k in range(1, len(marks)):
        t0, t1 = marks[k - 1][0], marks[k][0]
        groups.append({'reason': marks[k][1],
                       'from': t0, 'to': t1,
                       'n_events': sum(1 for e in seq if t0 <= (e.get('t_wall') or 0) <= t1),
                       'events': [e for e in seq if t0 <= (e.get('t_wall') or 0) <= t1]})
        prev = t1
    return groups


# 回放时在页面上画一个跟随的圆点：Playwright 的鼠标是虚拟的，
# 只调 page.mouse.move() 屏幕上看不到，人也不知道回放到哪一步了。
_CURSOR_INJECT = """
() => {
  if (document.getElementById('__replay_cursor')) return;
  const d = document.createElement('div');
  d.id = '__replay_cursor';
  d.style.cssText = 'position:fixed;left:-50px;top:-50px;width:13px;height:13px;'
    + 'border-radius:50%;background:rgba(255,64,64,.92);'
    + 'border:2px solid #fff;box-shadow:0 0 6px rgba(0,0,0,.7);'
    + 'transform:translate(-50%,-50%);pointer-events:none;z-index:2147483647';
  document.body.appendChild(d);
  window.__replayCursor = function (x, y) {
    d.style.left = x + 'px'; d.style.top = y + 'px';
  };
}
"""

_CURSOR_MOVE = "([x,y]) => window.__replayCursor && window.__replayCursor(x, y)"


def play_events(page, events, on_segments=None, tolerant=False, label='',
                on_reanchor=None):
    """按原相对时间重放一段时间线。返回实际投递的输入事件数。

    `on_segments`：`segments.sync` 的回调（选中集变化）。为什么要单独重放它：
    选中集不在 pointer/wheel/key 里，也不在起点 state 快照里；漏了它，回放端就
    “什么都没选中”，而 NG 恰好按“选中为空”渲染：显示全部分割（全彩），
    3D 不画 mesh——与录制端“EM 灰底 + 只显示选中段 + 有 mesh”正好相反。
    `selection.change` 是**悬停的段**（随鼠标走），不是选中集，这里跳过。

    `tolerant=True`：单条动作超时/失败不中断整段，只跳过它并继续。为什么需要：
    回放端一旦有选中段，NG 去抳 3D mesh，页面忙的时候一次 `mouse.move` 会卡到
    Playwright 的默认超时（30 s）——几百条输入就能把整次回放拖成几十分钟。
    """
    MOD = {'ctrl': 'Control', 'shift': 'Shift', 'alt': 'Alt', 'meta': 'Meta'}
    n = 0
    skipped = 0
    last = None
    held = set()
    if not getattr(play_events, '_cursor', False):
        try:
            page.evaluate(_CURSOR_INJECT)
            play_events._cursor = True
        except Exception:
            pass

    def move_to(x, y):
        try:
            page.mouse.move(x, y)
        except Exception:
            raise
        finally:
            # 指针标记失败不影响回放本身
            try:
                page.evaluate(_CURSOR_MOVE, [x, y])
            except Exception:
                pass
    for e in events:
        t = e.get('t_wall') or 0
        if last is not None:
            gap = min(max((t - last) / 1000.0, 0.004), 0.2)
            time.sleep(gap)
        last = t
        if e.get('type') == 'segments.sync':
            if on_segments is not None:
                on_segments(e)
            continue
        if e.get('type') == 'gesture.reanchor':
            if on_reanchor is not None:
                on_reanchor(e)
            continue
        if csp.ctype(e) == 'selection.change':
            continue
        x, y = int(e.get('x') or 0), int(e.get('y') or 0)
        # 修饰键是“动作的一部分”：ctrl+wheel 才是缩放，裸 wheel 什么都不做。
        want = {MOD[m] for m in (e.get('modifiers') or []) if m in MOD}
        try:
            for k in want - held:
                page.keyboard.down(k)
            for k in held - want:
                page.keyboard.up(k)
            held = want
            if csp.ctype(e) == 'input.wheel':
                move_to(x, y)
                page.mouse.wheel(int(e.get('dx') or 0), int(e.get('dy') or 0))
            elif csp.ctype(e) == 'input.key':
                k = e.get('code') or e.get('key') or ''
                if k:
                    page.keyboard.press(k)
            else:
                ph = e.get('phase') or ''
                if ph in ('pointerdown', 'mousedown', 'pointermove', 'mousemove'):
                    move_to(x, y)
                if ph in ('pointerdown', 'mousedown'):
                    page.mouse.down(button='left')
                elif ph in ('pointerup', 'mouseup'):
                    page.mouse.up(button='left')
            n += 1
        except Exception:
            if not tolerant:
                raise
            skipped += 1
        if label and n and n % 50 == 0:
            print('    … %s 已投递 %d 条（跳过 %d）' % (label, n, skipped),
                  flush=True)
    for k in held:
        try:
            page.keyboard.up(k)
        except Exception:
            pass
    return n


def settle(page, timeout=8.0, min_colored=0.05):
    """等到画面真有内容（粗筛）+ 稳定，避免把半渲染当结果。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = page.evaluate(PAINT_JS) or {}
        if (r.get('colored') or 0) >= min_colored:
            time.sleep(0.35)
            return r.get('colored')
        time.sleep(0.25)
    return (page.evaluate(PAINT_JS) or {}).get('colored')


def state_of(page):
    fp = page.evaluate(rsq.LIVE_FP_JS)
    return rsq.fingerprint(state=fp) if fp else None


def _pos_delta(exp, got):
    a = (exp or {}).get('position') or []
    b = (got or {}).get('position') or []
    if len(a) != 3 or len(b) != 3:
        return None
    return max(abs(float(x) - float(y)) for x, y in zip(a, b))


def action_effects(anchors):
    """动作有没有真的改变视角（前置条件）。

    没这道门，“重放误差 0”可能是两边都没动——那就变成假绿。
    每段给出：位置位移、尺度相对变化、是否算“有动作”。
    """
    order = ['pre', 'g_drag1', 'g_wheel', 'g_click', 'g_drag2', 'g_keys']
    out = {}
    for k in range(1, len(order)):
        a = rsq.fingerprint(anchors.get(order[k - 1]))
        b = rsq.fingerprint(anchors.get(order[k]))
        d = _pos_delta(a, b)
        sc = None
        try:
            x, y = float((a or {}).get('crossSectionScale')), \
                float((b or {}).get('crossSectionScale'))
            sc = round(abs(y - x) / x, 4) if x else None
        except Exception:
            sc = None
        if order[k] == 'g_wheel':
            eff = sc is not None and sc >= 0.001
        elif order[k] == 'g_click':
            eff = True           # 点击的预期就是“不变”（真断言在重放侧）
        else:
            # 拖动“有作用”只需位移确实不为 0；阈值不能写 100nm：
            # 缩放后同样 15px/步对应的 nm 会小很多（实测放大后只剩 65nm）。
            eff = d is not None and d > 1.0
        out[order[k]] = {'pos_delta_nm': None if d is None else round(d, 2),
                         'scale_rel': sc, 'effective': eff}
    return out


def replay(send, page, pack, anchors, groups):
    """在全新页面里“只靠输入”把动作重放一遍，逐段比对。"""
    send('dims %d %d %d' % DIMS)
    time.sleep(0.3)
    send('img em ' + EM_SOURCE)
    time.sleep(0.3)
    send('seg autoseg ' + SEG_SOURCE)
    time.sleep(0.3)
    send('cross 6')
    send('pos %d %d %d' % tuple(A))
    time.sleep(1.5)

    pre = anchors.get('pre')
    if not pre:
        raise SystemExit('录制里没有 pre checkpoint，无法确定重放起点')
    send('state ' + json.dumps(pre))
    t0 = time.time()
    while time.time() - t0 < 20:
        got = page.evaluate(POS_JS)
        want = pre.get('position') or []
        if got and len(want) == 3 and all(abs(a - b) < 1.0
                                          for a, b in zip(got, want)):
            break
        time.sleep(0.3)
    settle(page)

    base = state_of(page)
    rows = []
    for g in groups:
        n = play_events(page, g['events'])
        time.sleep(0.5)
        settle(page, timeout=4.0)
        got = state_of(page)
        exp = rsq.fingerprint(anchors.get(g['reason']))
        cmp = rsq.compare(exp, got, pos_tol=POS_TOL_NM,
                          scale_rtol=SCALE_RTOL)
        rows.append({
            'group': g['reason'],
            'n_events_played': n,
            'n_events_recorded': len(g['events']),
            'pos_delta_nm': (None if _pos_delta(exp, got) is None
                             else round(_pos_delta(exp, got), 2)),
            'scale_rec': (exp or {}).get('crossSectionScale'),
            'scale_got': (got or {}).get('crossSectionScale'),
            'scale_rel': _rel((exp or {}).get('crossSectionScale'),
                              (got or {}).get('crossSectionScale')),
            'fields_ok': cmp['ok'],
            'failed': cmp['failed'],
        })
    return base, rows


def _rel(a, b):
    try:
        a, b = float(a), float(b)
        if a == 0:
            return None
        return round(abs(a - b) / abs(a), 4)
    except Exception:
        return None


# ---------------------------------------------------------------- 主流程
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=str(ROOT / 'results' / 'action_replay'))
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args(argv)

    out = Path(args.out) / time.strftime('%Y%m%d_%H%M%S')
    out.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding='utf-8',
        errors='replace', bufsize=1, cwd=str(ROOT), env=CHILD_ENV)

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

    from playwright.sync_api import sync_playwright

    pack, anchors, groups, report = None, {}, [], {}
    try:
        with sync_playwright() as p:
            # ---------- 1) 录制 ----------
            b1 = browser_util.launch_browser(p, headless=not args.headed)
            pg1 = b1.new_page(viewport={'width': 1400, 'height': 900})
            pg1.on('pageerror', lambda e: None)
            pg1.add_init_script(path=str(GPU_PATCH))   # document-start
            pg1.goto(url, wait_until='domcontentloaded', timeout=60000)
            pg1.wait_for_function('() => !!window.viewer', timeout=30000)
            print('[record] 拖动 / 滚轮 / 点击 / 反向拖动')
            pack, anchors = record(pg1, send)
            b1.close()

            groups = groups_of(pack, anchors)
            seq = inputs_of(pack)
            n_inputs = len(seq)
            n_pointer = sum(1 for e in seq if csp.ctype(e) == 'input.pointer')
            n_wheel = sum(1 for e in seq if csp.ctype(e) == 'input.wheel')
            n_key_all = sum(1 for e in seq if csp.ctype(e) == 'input.key')
            print(f'[record] 输入事件 {n_inputs}'
                  f'（pointer {n_pointer} / wheel {n_wheel} / key {n_key_all}）'
                  f'，锚点 {sorted(anchors)}')

            # ---------- 2) 重放 ----------
            b2 = browser_util.launch_browser(p, headless=not args.headed)
            pg2 = b2.new_page(viewport={'width': 1400, 'height': 900})
            pg2.on('pageerror', lambda e: None)
            pg2.add_init_script(path=str(GPU_PATCH))
            pg2.goto(url, wait_until='domcontentloaded', timeout=60000)
            pg2.wait_for_function('() => !!window.viewer', timeout=30000)
            print('[replay] 全新页面，先恢复动作前 state，再只喂输入事件')
            base, rows = replay(send, pg2, pack, anchors, groups)

            # 结束态像素：与录制时那张关键帧比。和保真度一样用**闭环**：
            # 读一帧比一帧，不够好就再等再读（不靠“等几秒”猜）。
            settle(pg2, timeout=6.0)
            shot = next((s for s in (pack.get('shots') or [])
                         if s.get('moment') == 'action_end'
                         and s.get('dataURL')), None)
            ncc, attempts, live, cmp = None, 0, {}, {}
            for _ in range(10):
                live = pg2.evaluate(READBACK_JS) or {}
                if not (shot and live.get('url')):
                    break
                attempts += 1
                cmp = pg2.evaluate(CMP_JS, [shot['dataURL'], live['url']]) or {}
                ncc = cmp.get('ncc')
                if ncc is not None and ncc >= 0.95:
                    break
                time.sleep(0.6)
            rec_png = out / 'recorded_action_end.png'
            live_png = out / 'replayed_action_end.png'
            if shot and live.get('url'):
                rec_png.write_bytes(
                    base64.b64decode(shot['dataURL'].split(',', 1)[1]))
                live_png.write_bytes(
                    base64.b64decode(live['url'].split(',', 1)[1]))
            b2.close()

        # ---------- 3) 断言 ----------
        checks = []

        def check(name, ok, detail=''):
            checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
                  + (f' — {detail}' if detail != '' else ''))

        n_down = sum(1 for e in inputs_of(pack)
                     if csp.ctype(e) == 'input.pointer'
                     and (e.get('phase') or '') in ('pointerdown', 'mousedown'))
        n_up = sum(1 for e in inputs_of(pack)
                   if csp.ctype(e) == 'input.pointer'
                   and (e.get('phase') or '') in ('pointerup', 'mouseup'))
        check('拖动被记为完整输入流（down/up 各 ≥2）',
              n_down >= 2 and n_up >= 2, f'down={n_down} up={n_up}')
        check('滚轮被记下（≥3 档）', n_wheel >= 3, n_wheel)
        check('滚轮记下了修饰键（ctrl：这个构建里缩放靠它）',
              any('ctrl' in (e.get('modifiers') or [])
                  for e in inputs_of(pack)
                  if csp.ctype(e) == 'input.wheel'),
              [e.get('modifiers') for e in inputs_of(pack)
               if csp.ctype(e) == 'input.wheel'])
        check('每段动作后都有可恢复 state（锚点齐全）',
              all(anchors.get(k) for k in ('pre', 'g_drag1', 'g_wheel',
                                           'g_click', 'g_drag2', 'g_keys')),
              sorted(anchors))
        n_key = sum(1 for e in inputs_of(pack)
                    if csp.ctype(e) == 'input.key')
        check('键盘动作被记下（≥6 次非修饰键）', n_key >= 6, n_key)
        # 这一项守的是“字段看着采了、其实是空”那类缺陷：
        # 之前 inputEventBindings 的查找路径写错，action 永远为 null。
        keys = [e for e in inputs_of(pack) if csp.ctype(e) == 'input.key']
        n_mapped = sum(1 for e in keys if e.get('action'))
        check('键盘动作带绑定语义（action 非空，如 x+/z+）',
              n_mapped >= max(1, len(keys) - 1),
              [(e.get('key'), e.get('code'), e.get('action'),
                e.get('action_section')) for e in keys])
        played = sum(r['n_events_played'] for r in rows)
        check('重放消费的事件数 == 录制数（无丢步）',
              played == n_inputs, f'{played}/{n_inputs}')

        # —— 前置门：动作本身得真的改变了视角，否则“重放误差 0”是假绿 ——
        eff = action_effects(anchors)
        check('每段动作都真的起了作用（非空跑）',
              all(v['effective'] for v in eff.values()),
              {k: {'Δnm': eff[k]['pos_delta_nm'],
                   'scale_rel': eff[k]['scale_rel']} for k in eff})
        # 还要保证“整段动作”不是原地转圈：起点 → 终点必须真的换了视角
        _pre = rsq.fingerprint(anchors.get('pre'))
        _end = rsq.fingerprint(anchors.get('g_keys'))
        _sc = action_effects(anchors)['g_wheel']['scale_rel'] or 0
        check('从起点到终点的视角真的变了（累计）',
              (_pos_delta(_pre, _end) or 0) >= 100.0 or _sc >= 0.01,
              {'total_pos_nm': _pos_delta(_pre, _end), 'wheel_rel': _sc})

        by = {r['group']: r for r in rows}
        d1 = by.get('g_drag1', {})
        check(f'拖拽后位置偏差 ≤ {POS_TOL_NM:.0f}nm',
              d1.get('pos_delta_nm') is not None
              and d1['pos_delta_nm'] <= POS_TOL_NM,
              f"Δ={d1.get('pos_delta_nm')}nm")
        w = by.get('g_wheel', {})
        check(f'滚轮后尺度相对误差 ≤ {SCALE_RTOL * 100:.0f}%（动画，给容差）',
              w.get('scale_rel') is not None and w['scale_rel'] <= SCALE_RTOL,
              f"rec={w.get('scale_rec')} got={w.get('scale_got')} "
              f"rel={w.get('scale_rel')}")
        c = by.get('g_click', {})
        check(f'点击不改变视角（≤ {CLICK_TOL_NM:.0f}nm）',
              c.get('pos_delta_nm') is not None
              and c['pos_delta_nm'] <= CLICK_TOL_NM,
              f"Δ={c.get('pos_delta_nm')}nm")
        d2 = by.get('g_drag2', {})
        check(f'反向拖拽回到原位（≤ {POS_TOL_NM:.0f}nm，检验偏差累积）',
              d2.get('pos_delta_nm') is not None
              and d2['pos_delta_nm'] <= POS_TOL_NM,
              f"Δ={d2.get('pos_delta_nm')}nm")
        kk = by.get('g_keys', {})
        check(f'键盘动作重放后位置偏差 ≤ {POS_TOL_NM:.0f}nm',
              kk.get('pos_delta_nm') is not None
              and kk['pos_delta_nm'] <= POS_TOL_NM,
              {'Δnm': kk.get('pos_delta_nm'),
               'fields_ok': kk.get('fields_ok'),
               'failed': kk.get('failed')})
        bad = [r['group'] for r in rows
               if r['group'] in ('g_click', 'g_drag2') and not r['fields_ok']]
        check('非位置字段（朝向/布局/图层/坐标系）全对', not bad, bad or 'OK')
        check('重放结束画面与录制帧 NCC ≥ 0.95',
              ncc is not None and ncc >= 0.95,
              {'ncc': ncc, 'attempts': attempts,
               'colored_rec': cmp.get('colored_a'),
               'colored_live': cmp.get('colored_b')})

        n_pass = sum(1 for c_ in checks if c_['ok'])
        report = {
            'n_pass': n_pass, 'n_checks': len(checks), 'checks': checks,
            'n_inputs': n_inputs, 'n_pointer': n_pointer, 'n_wheel': n_wheel,
            'n_key': n_key,
            'anchors': sorted(anchors), 'groups': rows,
            'action_effects': eff,
            'final_ncc': ncc,
            'final_ncc_attempts': attempts,
            'final_colored': {'rec': cmp.get('colored_a'),
                              'live': cmp.get('colored_b')},
            'tolerances': {'pos_nm': POS_TOL_NM, 'click_nm': CLICK_TOL_NM,
                           'scale_rtol': SCALE_RTOL},
            'panes': {'recorded': str(rec_png), 'replayed': str(live_png)},
            'session_pack': str(out / 'pack.json'),
            'note': '只重放 pointer/wheel；滚轮是动画，尺度给相对容差；'
                    '公开栈分割为当前版本，像素高不代表当时那版',
        }
        (out / 'pack.json').write_text(
            json.dumps(pack, ensure_ascii=False), encoding='utf-8')
        (out / 'report.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'\n{n_pass}/{len(checks)} 项通过')
        print('RESULT:', 'PASS' if n_pass == len(checks) else 'FAIL')
        print('报告:', csp.relpath(out / 'report.json'))
        return 0 if n_pass == len(checks) else 1
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
