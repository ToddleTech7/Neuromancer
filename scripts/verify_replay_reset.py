"""回放复位自检：脏状态 → 干净状态，必须回到"记录里那帧"，不能沿用上一个点。

守的是什么
----------
`state` 里**没有**的键 = 当时是默认值。回放时如果只"有就设、没有就跳过"，
上一个回放点的尺度/朝向/布局就会留在画面上——位置对得上、画面却是错的，
而且任何只看 position 的判据都发现不了。

这个脚本完全本地（不碰公网数据），只验证复位语义：
  1. 先灌一个"脏"状态（尺度 7/3、两个朝向都转了、布局 xy）；
  2. 再灌一个记录里只有 position 的"干净"状态；
  3. 读回当前视图指纹，要求它 == 干净状态的指纹（用 replay_state.compare）；
  4. 负向对照：脏指纹 vs 干净指纹必须判不一致（证明判据本身有区分力）。

用法：
    python3 scripts/verify_replay_reset.py            # 无头
    python3 scripts/verify_replay_reset.py --headed
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
# 本地 demo 服务的输出收进管道按 UTF-8 读，所以让它按 UTF-8 写
# （中文 Windows 缺省是 cp936；只改这个子进程，不动整个进程的环境）
CHILD_ENV = {**os.environ, 'PYTHONUTF8': '1'}

import cave_session_pack as csp  # noqa: E402

import replay_state as rs  # noqa: E402

SERVER = HERE / 'cave_local_demo_server.py'
OUT = ROOT / 'results' / 'replay_reset'

DIMS = {'x': [4e-09, 'm'], 'y': [4e-09, 'm'], 'z': [4e-08, 'm']}

# 脏：三个"缺省即默认"的字段都被显式改过
DIRTY = {
    'position': [10, 20, 30],
    'crossSectionScale': 7,
    'projectionScale': 3,
    'crossSectionOrientation': [0.7071068, 0.0, 0.0, 0.7071068],
    'projectionOrientation': [0.7071068, 0.0, 0.0, 0.7071068],
    'layout': 'xy',
    'dimensions': DIMS,
}
# 干净：同一份记录里只有 position（其余键缺席 = 当时用默认）
CLEAN = {
    'position': [40, 50, 60],
    'dimensions': DIMS,
}


def main(argv=None):
    ap = argparse.ArgumentParser(description='回放状态复位自检（本地，无公网）')
    ap.add_argument('--headed', action='store_true')
    args = ap.parse_args(argv)

    import browser_util
    from playwright.sync_api import sync_playwright

    proc = subprocess.Popen(
        [sys.executable, '-u', str(SERVER)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding='utf-8', errors='replace', bufsize=1, cwd=str(ROOT),
        env=CHILD_ENV)
    url, errs = None, []
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

    def send(state):
        proc.stdin.write('state ' + json.dumps(state, ensure_ascii=False) + '\n')
        proc.stdin.flush()

    def read(page, tag):
        """等位置到位后读回视图指纹（位置是唯一异步收敛的字段）。

        LIVE_FP_JS 返回的是**当前**视图；再经 rs.fingerprint() 归一，
        才能和“记录侧”的指纹同坐标系比较。
        """
        want = json.loads(json.dumps(state_wanted.get(tag) or {}))
        pos = want.get('position')
        deadline = time.time() + 12
        got = None
        while time.time() < deadline:
            got = page.evaluate(rs.LIVE_FP_JS)
            if got and pos and got.get('position') and all(
                    abs(a - b) < 1.0 for a, b in zip(got['position'], pos)):
                break
            time.sleep(0.3)
        time.sleep(0.3)
        raw = page.evaluate(rs.LIVE_FP_JS) or got
        return rs.fingerprint(state=raw)

    state_wanted = {}
    rows, checks = [], []

    def check(name, cond, detail=''):
        checks.append({'name': name, 'ok': bool(cond),
                       'detail': str(detail)[:300]})
        print(f'  [{"PASS" if cond else "FAIL"}] {name}'
              + (f' — {detail}' if detail else ''))

    try:
        with sync_playwright() as p:
            b = browser_util.launch_browser(p, headless=not args.headed)
            page = b.new_page(viewport={'width': 900, 'height': 700})
            page.on('pageerror', lambda e: errs.append(str(e)[:120]))
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_function('() => !!window.viewer', timeout=30000)

            # 1) 脏
            state_wanted['dirty'] = DIRTY
            send(DIRTY)
            dirty = read(page, 'dirty')
            rows.append({'step': 'dirty', 'fp': dirty})

            # 2) 脏 → 干净（这只是"换个回放点"，不是新会话）
            state_wanted['clean'] = CLEAN
            send(CLEAN)
            clean = read(page, 'clean')
            rows.append({'step': 'clean', 'fp': clean})

            # 3) 再回到脏点：反向也要成立（不是单向巧合）
            state_wanted['dirty2'] = DIRTY
            send(DIRTY)
            dirty2 = read(page, 'dirty2')
            rows.append({'step': 'dirty2', 'fp': dirty2})
            b.close()
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass

    exp_clean = rs.fingerprint(CLEAN)
    exp_dirty = rs.fingerprint(DIRTY)
    r_clean = rs.compare(exp_clean, clean)
    r_dirty = rs.compare(exp_dirty, dirty)
    r_dirty2 = rs.compare(exp_dirty, dirty2)

    print('\n[记录 → 回放] 逐字段：')
    for tag, r in (('dirty', r_dirty), ('clean', r_clean),
                   ('dirty2', r_dirty2)):
        bad = [k for k in rs.FIELDS if not r['fields'].get(k, {}).get('ok')]
        print(f"  {tag:7s} ok={r['ok']} failed={bad or '-'}")
        for k in bad:
            f = r['fields'][k]
            print(f"      {k}: expected={f.get('expected')!r} got={f.get('got')!r}"
                  + (f" broken={f.get('broken')}" if f.get('broken') else ''))

    print()
    check('脏状态本身被正确恢复（判据不是恒假）', r_dirty['ok'],
          r_dirty['failed'])
    check('脏 → 干净：复位到记录里的默认值（无泄漏）', r_clean['ok'],
          r_clean['failed'])
    check('干净 → 脏：反向也成立', r_dirty2['ok'], r_dirty2['failed'])
    # 负向对照：如果把"脏"和"干净"直接比，必须判不一致
    neg = rs.compare(exp_clean, dirty)
    check('负向对照：脏指纹 ≠ 干净指纹（判据有区分力）', not neg['ok'],
          neg['failed'])

    n_pass = sum(1 for c in checks if c['ok'])
    result = 'PASS' if n_pass == len(checks) else 'FAIL'
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out = OUT / ts
    out.mkdir(parents=True, exist_ok=True)
    (out / 'report.json').write_text(json.dumps({
        'result': result, 'ts': ts,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(),
        'checks': checks, 'n_pass': n_pass, 'n_checks': len(checks),
        'rows': rows, 'errors': errs,
        'compare': {'dirty': r_dirty['failed'], 'clean': r_clean['failed'],
                    'dirty2': r_dirty2['failed']},
    }, ensure_ascii=False, indent=1), encoding='utf-8')

    print(f'\n{n_pass}/{len(checks)} 项通过')
    print('RESULT:', result)
    print('报告:', csp.relpath(out / 'report.json'))
    return 0 if result == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
