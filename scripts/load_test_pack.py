#!/usr/bin/env python3
"""合成负载测试：不为真实采集，只验证“大会话包”的落盘/校验路径。

    python3 scripts/load_test_pack.py --hours 1
    python3 scripts/load_test_pack.py --events 20000 --shots 20 --shot-kb 200

默认写到临时目录；session_id 以 `sess_synthetic_load_` 开头，绝不写进
results/manual_sessions。产物不能当研究数据使用。
"""

import argparse
import base64
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp   # noqa: E402
import inspect_pack as ip         # noqa: E402


def make_pack(n_events, n_shots, shot_bytes):
    t0 = int(time.time() * 1000)
    events = [{'seq': 1, 't_wall': t0, 'type': 'session.start',
               'session_id': 'sess_synthetic_load_%d' % t0,
               'page': {'url': 'synthetic://load-test'},
               'collector_version': 'load-test'}]
    for i in range(n_events):
        events.append({
            'seq': i + 2, 't_wall': t0 + i * 100,
            't_mono': i * 100.0, 'type': 'input.pointer',
            'phase': 'pointermove', 'x': 600 + (i % 20),
            'y': 400 + (i % 20), 'button': -1,
        })
    events.append({'seq': len(events) + 1, 't_wall': t0 + n_events * 100,
                   'type': 'session.end', 'counts': {}})
    payload = b'\x89PNG\r\n\x1a\n' + b'\0' * max(0, shot_bytes - 8)
    url = 'data:image/png;base64,' + base64.b64encode(payload).decode()
    shots = [{'shot_id': 'shot_%d' % i, 't_wall': t0 + i * 1000,
              'w': 1280, 'h': 740, 'ok': True,
              'capture_status': 'captured', 'moment': 'checkpoint',
              'state_id': i, 'state_sha': 'synthetic%04d' % i,
              'dataURL': url} for i in range(1, n_shots + 1)]
    return {
        'session_id': 'sess_synthetic_load_%d' % t0,
        'schema': 'cave_recorder.session.v0.2',
        'collector_version': 'load-test',
        'page': {'url': 'synthetic://load-test'},
        'time': {'start_wall_ms': t0, 'end_wall_ms': t0 + n_events * 100},
        'config': {'move_sample_ms': 0, 'note': 'synthetic load test'},
        'counts': {'input.pointer': n_events},
        'events': events, 'notes': [], 'shots': shots,
        'collector': {'version': 'load-test'},
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description='合成大会话包负载测试')
    ap.add_argument('--hours', type=float, default=None,
                    help='按 1 小时 ≈ 90000 事件 / 120 张 600KB 图折算')
    ap.add_argument('--events', type=int, default=20000)
    ap.add_argument('--shots', type=int, default=20)
    ap.add_argument('--shot-kb', type=int, default=200)
    ap.add_argument('--out', default=None, help='输出目录（默认临时目录）')
    ap.add_argument('--keep', action='store_true', help='保留临时产物')
    args = ap.parse_args(argv)
    if args.hours:
        args.events = int(90000 * args.hours)
        args.shots = max(1, int(120 * args.hours))
        args.shot_kb = 600
    pack = make_pack(args.events, args.shots, args.shot_kb * 1024)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(
        prefix='neuromancer_load_'))
    t0 = time.time()
    d, man = csp.pack_session(pack, out, with_html=False)
    t_pack = time.time() - t0
    t1 = time.time()
    v = ip.verify_pack(d)
    t_verify = time.time() - t1
    total = sum(p.stat().st_size for p in Path(d).rglob('*') if p.is_file())
    print('SYNTHETIC LOAD — 不是真实采集，不能当研究数据')
    print('  事件 %d / 图 %d × %d KB' % (args.events, args.shots, args.shot_kb))
    print('  包 %s（%.1f MB）' % (d, total / 1024 / 1024))
    print('  pack %.1fs / verify %.1fs / verify=%s'
          % (t_pack, t_verify, 'PASS' if v['ok'] else 'FAIL'))
    if not v['ok']:
        print('  errors:', v['errors'][:5])
    if not args.keep and not args.out:
        shutil.rmtree(out, ignore_errors=True)
    return 0 if v['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
