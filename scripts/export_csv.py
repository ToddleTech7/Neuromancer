#!/usr/bin/env python3
"""把一个会话包导出成三张 CSV：events / episodes / hover。

    python3 scripts/export_csv.py <包> [--out <目录>] [--no-events]
    nx.py export-csv <包> [--out <目录>]

默认写到 <包>/derived/csv/。只读原始包；派生用 cave_episodes 的同一套规则。
"""

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cave_episodes as cep       # noqa: E402
import cave_session_pack as csp   # noqa: E402


def _json(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _center(view):
    c = (view or {}).get('center_of_mass_nm')
    return _json(c) if c else ''


def export(packdir, outdir=None, with_events=True):
    pack = csp.load_session(packdir)
    pdir = Path(pack.get('pack_dir') or packdir)
    out = Path(outdir) if outdir else (pdir / 'derived' / 'csv')
    out.mkdir(parents=True, exist_ok=True)

    if with_events:
        evp = out / 'events.csv'
        with evp.open('w', encoding='utf-8', newline='') as f:
            w = csv.writer(f)
            w.writerow(['seq', 't_wall', 't_mono', 'type', 'state_id',
                        'phase', 'x', 'y', 'key', 'button', 'text',
                        'state_sha', 'state_bytes', 'data_json'])
            for e in pack.get('events') or []:
                data = {k: v for k, v in e.items() if k != 'state'}
                w.writerow([
                    e.get('seq'), e.get('t_wall'), e.get('t_mono'),
                    csp.ctype(e), e.get('state_id'), e.get('phase'),
                    e.get('x'), e.get('y'), e.get('key') or e.get('code'),
                    e.get('button'), e.get('text'), e.get('state_sha'),
                    e.get('state_bytes'), _json(data),
                ])

    derived = cep.derive_episodes(pack)
    ep = out / 'episodes.csv'
    with ep.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['episode_id', 't_start_ms', 't_end_ms', 'duration_ms',
                    'n_events', 'no_edit', 'disposition', 'trigger_reason',
                    'view_center_nm', 'path_length_nm',
                    'net_displacement_nm', 'hover_status', 'n_hover_segments',
                    'idle_segments', 'policy_version'])
        for e in derived.get('episodes') or []:
            v = e.get('view') or {}
            h = e.get('hover') or {}
            w.writerow([
                e.get('episode_id'), e.get('t_start_ms'), e.get('t_end_ms'),
                e.get('duration_ms'), e.get('n_events'), e.get('no_edit'),
                e.get('disposition'), (e.get('trigger') or {}).get('reason'),
                _center({'center_of_mass_nm': v.get('center_of_mass_nm')}),
                v.get('path_length_nm'), v.get('net_displacement_nm'),
                h.get('status'), h.get('n_segments'),
                _json(e.get('idle_segments') or []), e.get('policy_version'),
            ])

    hov = out / 'hover.csv'
    with hov.open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['status', 'id', 'layers', 'n_spans', 'total_ms',
                    'max_span_ms', 'first_t_wall', 'last_t_wall'])
        h = derived.get('hover') or {}
        for s in h.get('segments') or []:
            w.writerow([h.get('status'), s.get('id'),
                        _json(s.get('layers') or []), s.get('n_spans'),
                        s.get('total_ms'), s.get('max_span_ms'),
                        s.get('first_t_wall'), s.get('last_t_wall')])

    summary = {
        'pack': str(pdir), 'out': str(out),
        'n_events': len(pack.get('events') or []),
        'n_episodes': len(derived.get('episodes') or []),
        'hover_status': (derived.get('hover') or {}).get('status'),
        'files': [p.name for p in (ep, hov) if p.exists()] +
                 (['events.csv'] if with_events else []),
    }
    (out / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding='utf-8')
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description='会话包 → CSV')
    ap.add_argument('pack', nargs='?', help='会话包目录（或 recorder JSON）')
    ap.add_argument('--pack', dest='pack_opt', default=None,
                    help='同位置参数，便于 nx.py 透传')
    ap.add_argument('--out', default=None, help='输出目录（默认 <包>/derived/csv）')
    ap.add_argument('--no-events', action='store_true', help='不导出 events.csv')
    args = ap.parse_args(argv)
    pack = args.pack or args.pack_opt
    if not pack:
        raise SystemExit('需要会话包路径')
    s = export(pack, args.out, with_events=not args.no_events)
    print('CSV:', s['out'])
    print('  事件 %(n_events)d ｜ episode %(n_episodes)d ｜ hover %(hover_status)s'
          % s)
    print('  文件:', ', '.join(s['files']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
