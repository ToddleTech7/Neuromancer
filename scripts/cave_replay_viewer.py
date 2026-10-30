"""cave_replay_viewer：会话包 → 只读状态回放（R1）+ 写请求闸门 + 回放清单。

它做的三件事（对应 v0.4 清单 Q09/FV01、FV08、FV12、QF13）：
  1. 用 recorder 录到的 state_checkpoint 在真实 Neuroglancer 实例里**逐点恢复**
     位置/尺度/朝向/图层/选中，并截图留证（R1 状态回放，可 seek）；
  2. 注入只读闸门：回放态下所有 CAVE `/api/` 的非 GET 请求被拒绝并记录，
     证明"回放不会二次执行 Merge/Split"（FV12 强门禁）；
  3. 写 `derived/replay_manifest.json`：readonly_replay_flag、每个 checkpoint 的
     恢复结果、`visual_replay_status` 与不可恢复项的原因。

如实边界（必须保留）：
- 公开栈没有历史分割快照：回放用的是**当前** seg 源，所以分割归属可能已变，
  `visual_replay_status` 一律标 `partial`（FV03/BV02 未满足），不得写成 complete；
- EM 源是版本无关底图，重取可复现（BV03 部分满足）；
- 未记录 state_checkpoint 的时段无法恢复，标 `unavailable`，不用最新状态凑。

用法（仓库根目录）：
    python scripts/cave_replay_viewer.py results/sessions/<sid>
    python scripts/cave_replay_viewer.py <session.json> --headed --hold 3
    python scripts/cave_replay_viewer.py <pack> --max-checkpoints 5
"""

import argparse
import json
import re
import subprocess
import threading
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp  # noqa: E402
import replay_state as rsq       # noqa: E402

SERVER = HERE / 'cave_local_demo_server.py'

GUARD_JS = r"""
() => {
  window.__replayGuard = {active: true, blocked: []};
  const isWrite = (m) => {
    m = String(m || 'GET').toUpperCase();
    return m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS';
  };
  const isCaveApi = (u) => /\/api\//.test(String(u || ''));
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      const u = (typeof input === 'string') ? input : (input && input.url) || '';
      const m = (init && init.method) || (input && input.method) || 'GET';
      if (isWrite(m) && isCaveApi(u)) {
        window.__replayGuard.blocked.push({url: String(u).slice(0, 200),
                                           method: String(m).toUpperCase()});
        return Promise.reject(new Error('replay_readonly_blocked'));
      }
    } catch (e) { /* ignore */ }
    return origFetch.apply(this, arguments);
  };
  const XO = window.XMLHttpRequest.prototype;
  const oOpen = XO.open, oSend = XO.send;
  XO.open = function (method, url) {
    this.__rg = {m: method, u: url};
    return oOpen.apply(this, arguments);
  };
  XO.send = function () {
    try {
      if (this.__rg && isWrite(this.__rg.m) && isCaveApi(this.__rg.u)) {
        window.__replayGuard.blocked.push({
          url: String(this.__rg.u).slice(0, 200),
          method: String(this.__rg.m).toUpperCase()});
        throw new Error('replay_readonly_blocked');
      }
    } catch (e) { throw e; }
    return oSend.apply(this, arguments);
  };
  return window.__replayGuard.active;
}
"""


def collect_checkpoints(pack, limit=None):
    cps = []
    for e in pack.get('events') or []:
        if csp.ctype(e) != 'viewer.checkpoint':
            continue
        st = e.get('state')
        if not isinstance(st, dict):
            continue
        cps.append({'state_id': e.get('state_id'),
                    't_wall': e.get('t_wall'),
                    'reason': e.get('reason'), 'state': st,
                    'viewpoint': e.get('viewpoint')})
    if not cps and isinstance(pack.get('initial_checkpoint_state'), dict):
        cps.append({'state_id': None, 't_wall': None,
                    'reason': 'initial_only',
                    'state': pack['initial_checkpoint_state']})
    if limit:
        cps = cps[:limit]
    return cps


def collect_states(pack, limit=None, source='auto'):
    """可恢复的时间点（复刻单位：一个时间点 = 一份完整 state）。

    来源三种，按可信度排（实现只有一份：replay_state.collect_points）：
      state       事件流里 viewer.state-change 自带的完整 state（主路）
      shot_state  快门自带完整 state（L1：拍时钉的，不是事后按时间猜的）
      checkpoint  30s 剖点 / 旧包兜底（source 字段如实标）
    """
    if source == 'checkpoint':
        pts = [dict(c, source='checkpoint')
               for c in collect_checkpoints(pack, None)]
    else:
        points, _specs, st = rsq.collect_points(pack.get('events') or [],
                                               pack.get('shots') or [])
        pts = []
        for p in points:
            src = p.get('source')
            pts.append({
                'state_id': p.get('state_id'),
                't_wall': p.get('t_wall'),
                'reason': ('state_change' if src == 'state'
                           else ('shot_keyframe' if src == 'shot_state'
                                 else 'checkpoint')),
                'source': src if src in ('state', 'shot_state') else 'checkpoint',
                'state': p.get('state'),
                'viewpoint': p.get('viewpoint'),
            })
        if source == 'state':
            pts = [x for x in pts if x['source'] in ('state', 'shot_state')]
        if not pts:
            pts = [dict(c, source='checkpoint')
                   for c in collect_checkpoints(pack, None)]
        if st.get('n_partial'):
            print(f"[info] 跳过 {st['n_partial']} 个残缺 state"
                  '（state_partial，刚开页那一瞬）')
        if st.get('n_from_shot_state'):
            print(f"[info] 另有 {st['n_from_shot_state']} 个快门自带 state"
                  '（未入事件流，但可精确恢复）')
    if limit:
        pts = pts[:limit]
    return pts


def _vec(v):
    if v is None:
        return None
    try:
        if hasattr(v, 'length'):
            return [round(float(v[i]), 3) for i in range(3)]
        return [round(float(v[i]), 3) for i in range(3)]
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description='只读状态回放 + 写闸门')
    ap.add_argument('session', help='会话包目录或 JSON')
    ap.add_argument('--headed', action='store_true')
    ap.add_argument('--hold', type=float, default=0.0,
                    help='每个 checkpoint 额外停留秒数（人工观看）')
    ap.add_argument('--max-checkpoints', type=int, default=0,
                    help='最多恢复多少个时间点（别名 --max-points）')
    ap.add_argument('--max-points', type=int, default=0)
    ap.add_argument('--source', choices=('auto', 'state', 'checkpoint'),
                    default='auto',
                    help='auto=优先用每个状态变化的完整 state；'
                         'checkpoint=只用 30s 剖点（旧包/调试用）')
    ap.add_argument('--out-dir', default=None,
                    help='默认写到 <pack>/derived/replay（JSON 包则写 results/replay_viewer）')
    args = ap.parse_args(argv)

    pack = csp.load_session(args.session)
    sid = pack.get('session_id') or 'session'
    pack_dir = Path(pack['pack_dir']) if pack.get('pack_dir') else None
    out_dir = (Path(args.out_dir) if args.out_dir
               else (pack_dir / 'derived' / 'replay' if pack_dir
                     else ROOT / 'results' / 'replay_viewer' / sid))
    out_dir.mkdir(parents=True, exist_ok=True)

    limit = args.max_points or args.max_checkpoints or None
    cps = collect_states(pack, limit, args.source)
    if not cps:
        print('[warn] 这个包既没有带 state 的 state-change，也没有 state_checkpoint；'
              '无法做状态回放（recorder 请开 full_state_on_change）')

    from playwright.sync_api import sync_playwright
    import browser_util

    proc = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', bufsize=1, cwd=str(ROOT))
    url = None
    t0 = time.time()
    while time.time() - t0 < 40:
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

    def drain():
        """把 NG 侧的 ERR 日志转出来，否则 _apply_state 失败会静默。"""
        for line in proc.stdout:
            s = line.strip()
            if s.startswith('ERR'):
                print(f'    [ng] {s[:200]}')

    threading.Thread(target=drain, daemon=True).start()

    rows = []
    guard = {'active': False, 'blocked': [], 'self_test': None}
    try:
        with sync_playwright() as p:
            b = browser_util.launch_browser(p, headless=not args.headed)
            page = b.new_page(viewport={'width': 1280, 'height': 800})
            page.on('pageerror', lambda e: None)
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_function('() => !!window.viewer', timeout=30000)
            guard['active'] = bool(page.evaluate(GUARD_JS))

            for i, cp in enumerate(cps, 1):
                send('state ' + json.dumps(cp['state'], ensure_ascii=False))
                want = None
                try:
                    raw_pos = cp['state'].get('position') or []
                    if len(raw_pos) == 3 and all(
                            isinstance(x, (int, float)) for x in raw_pos):
                        want = [float(x) for x in raw_pos]
                except Exception:
                    want = None
                applied = False
                deadline = time.time() + 12
                while time.time() < deadline:
                    # 注：viewer.position 是 Watchable nD（.value 为字符串），
                    # 取当前坐标用 state.toJSON().position（数组）。
                    got = page.evaluate(
                        '''() => { const v = window.viewer;
                             if (!v) return null;
                             try {
                               const p = v.state.toJSON().position;
                               return p ? [p[0], p[1], p[2]] : null;
                             } catch (e) { return null; } }''')
                    if want and got and all(
                            isinstance(a, (int, float)) and abs(a - b) < 1.0
                            for a, b in zip(got, want)):
                        applied = True
                        break
                    time.sleep(0.4)
                if args.hold:
                    time.sleep(args.hold)
                shot = out_dir / f'ckpt_{i:03d}.png'
                try:
                    raw = page.screenshot()
                    shot.write_bytes(raw)
                except Exception:
                    shot = None
                pos = page.evaluate(
                    '''() => { const v = window.viewer;
                         if (!v) return null;
                         try {
                           const p = v.state.toJSON().position;
                           return p ? [p[0], p[1], p[2]] : null;
                         } catch (e) { return null; } }''')
                # 位置对上只是必要条件；尺度/朝向/图层/坐标系也要对，
                # 否则“恢复成功”会静默地画出另一张图（与保真度自检同一判据）。
                live_fp = page.evaluate(rsq.LIVE_FP_JS)
                cmp_fp = (rsq.compare(rsq.fingerprint(cp['state'],
                                                      cp.get('viewpoint')),
                                      rsq.fingerprint(state=live_fp))
                          if live_fp else
                          {'ok': False, 'failed': ['<no_live_state>'],
                           'fields': {}})
                rows.append({
                    'idx': i, 'state_id': cp.get('state_id'),
                    't_wall': cp.get('t_wall'), 'reason': cp.get('reason'),
                    'source': cp.get('source') or 'checkpoint',
                    'expected_position': want,
                    'applied_position': _vec(pos),
                    'state_applied': applied,
                    'state_match': bool(cmp_fp.get('ok')),
                    'state_failed_fields': cmp_fp.get('failed') or [],
                    'state_fields': {k: bool(v.get('ok')) for k, v in
                                     (cmp_fp.get('fields') or {}).items()},
                    'screenshot': (str(shot.relative_to(out_dir))
                                   if shot else None),
                    'visual_replay_status': 'partial' if applied else 'unavailable',
                })
                print(f'  [{i}/{len(cps)}] state_id={cp.get("state_id")} '
                      f'applied={applied} pos={_vec(pos)} '
                      f'fields={"OK" if cmp_fp.get("ok") else cmp_fp.get("failed")}')

            # 写闸门自测：故意发一个 CAVE 写请求，必须被拒并留痕
            try:
                selftest = page.evaluate(
                    """async () => {
                      try {
                        await fetch('http://127.0.0.1:9/segmentation/api/v1/do_merge',
                                    {method: 'POST', body: '{}'});
                        return 'NOT_BLOCKED';
                      } catch (e) { return String(e.message || e); }
                    }""")
            except Exception as err:
                selftest = f'eval_failed:{type(err).__name__}'
            guard['self_test'] = selftest
            guard['blocked'] = page.evaluate(
                '() => (window.__replayGuard || {}).blocked || []')
            b.close()
    finally:
        proc.kill()

    blocked_ok = bool(guard['blocked'])
    n_applied = sum(1 for r in rows if r['state_applied'])
    n_match = sum(1 for r in rows if r.get('state_match'))
    per_field = {k: 0 for k in rsq.FIELDS}
    for r in rows:
        for k, ok_v in (r.get('state_fields') or {}).items():
            if ok_v:
                per_field[k] = per_field.get(k, 0) + 1
    failed_fields = sorted({k for r in rows
                            for k in (r.get('state_failed_fields') or [])})
    src_refs = []
    if cps:
        for l in (cps[0]['state'].get('layers') or []):
            s = l.get('source')
            if isinstance(s, list):
                s = s[0] if s else None
            src_refs.append({'layer': l.get('name'), 'source': s,
                             'visible': l.get('visible')})
    manifest = {
        'schema': 'neuromancer.replay_manifest.v1',
        'session_id': sid,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'readonly_replay_flag': True,
        'viewer': {'url': url, 'build': 'stock neuroglancer (pip)',
                   'headless': not args.headed},
        'guard': {
            'active': guard['active'],
            'blocked_attempts': guard['blocked'],
            'self_test': guard['self_test'],
            'enforces': '回放态拒绝所有 CAVE /api/ 非 GET 请求（FV12）',
            'verified': blocked_ok,
        },
        'checkpoints': rows,
        'n_checkpoints': len(rows),
        'replay_source': args.source,
        'n_from_state_change': sum(1 for r in rows
                                   if r.get('source') == 'state'),
        'n_from_shot_state': sum(1 for r in rows
                                 if r.get('source') == 'shot_state'),
        'n_state_applied': n_applied,
        'n_state_match': n_match,
        'per_field_ok': per_field,
        'state_failed_fields': failed_fields,
        'state_match_judge': '位置/尺度/朝向/布局/图层/坐标系 逐项比对（replay_state.compare）',
        'visual_replay_status': 'partial' if n_applied else 'unavailable',
        'visual_gaps': [
            {'reason': 'segmentation_not_versioned',
             'detail': '公开栈无历史 seg 快照；回放用当前源，分割归属可能已变'
                       '（FV03/BV02 未满足）'},
            {'reason': 'mesh_not_restored',
             'detail': '本回放只恢复 2D/相机状态与图层，未重建历史 mesh（FV04）'},
            {'reason': 'chunk_state_unknown',
             'detail': '未记录 V_loaded/rendered，无法证明“当时是否已加载”（FV09）'},
        ],
        'source_refs': src_refs,
    }
    mp = out_dir / 'replay_manifest.json'
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                  encoding='utf-8')
    print(f'\n回放清单: {mp}')
    print(f'  checkpoint 位置恢复 {n_applied}/{len(rows)}；'
          f'字段全对 {n_match}/{len(rows)}'
          + (f'（失败字段 {failed_fields}）' if failed_fields else '') +
          f'；只读闸门拦截 {len(guard["blocked"])} 次；自测={guard["self_test"]}')
    if rows:
        print(f"  恢复点来源：state-change {sum(1 for r in rows if r.get('source') == 'state')}"
              f" + 快门自带 state {sum(1 for r in rows if r.get('source') == 'shot_state')}"
              f" + checkpoint {sum(1 for r in rows if r.get('source') == 'checkpoint')}")
    print('  visual_replay_status =', manifest['visual_replay_status'],
          '（分割版本不可回溯，如实标 partial）')
    return 0 if (n_applied or not cps) else 1


if __name__ == '__main__':
    sys.exit(main())
