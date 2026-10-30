#!/usr/bin/env python3
"""检查一个会话包：存了什么、健康度如何、锚点和关键帧能不能用来回放。

    python3 scripts/inspect_pack.py                     # 最新的一个会话包
    python3 scripts/inspect_pack.py results/manual_sessions/<id>
    python3 scripts/inspect_pack.py <包目录> --json      # 给脚本用

只读；不修改任何文件。字段含义见 docs/数据格式.md。
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import cave_session_pack as csp       # noqa: E402

SHOTS_DIR = 'client/shots'


def pack_root():
    return Path(os.environ.get('NM_SESSIONS')
                or (ROOT / 'results' / 'manual_sessions')).expanduser()


def latest_pack():
    base = pack_root()
    packs = [p for p in base.glob('*')
             if p.is_dir() and not p.name.startswith(('.', '_'))
             and (p / 'manifest.json').exists()] if base.exists() else []
    return max(packs, key=lambda p: p.stat().st_mtime) if packs else None


def dir_size(p):
    total = 0
    for f in Path(p).rglob('*'):
        if f.is_file():
            total += f.stat().st_size
    return total


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit) if unit != 'B' else '%d B' % n
        n /= 1024.0


def summarize(path):
    pack = csp.load_session(path)
    pdir = Path(pack.get('pack_dir') or path)
    events = pack.get('events') or []
    shots = pack.get('shots') or []
    notes = pack.get('notes') or []
    manifest = pack.get('manifest') or {}

    counts = {}
    for e in events:
        counts[e.get('type') or '?'] = counts.get(e.get('type') or '?', 0) + 1

    # 锚点：只认带 position 的 checkpoint（第一个自动检查点可能只有 layout）
    anchors, dropped = [], []
    for e in events:
        if csp.ctype(e) != 'viewer.checkpoint':
            continue
        st = e.get('state') if isinstance(e.get('state'), dict) else {}
        row = {'reason': e.get('reason') or '?',
               't_wall': e.get('t_wall'),
               'state_id': e.get('state_id'),
               'state_sha': e.get('state_sha'),
               'has_position': bool(st.get('position'))}
        (anchors if row['has_position'] else dropped).append(row)

    shot_rows = []
    for s in shots:
        ref = s.get('image_ref')
        f = pdir / SHOTS_DIR / Path(ref).name if ref else None
        shot_rows.append({
            'shot_id': s.get('shot_id'),
            'capture_status': s.get('capture_status'),
            'usable': s.get('capture_status') == 'captured',
            'px': [s.get('w'), s.get('h')],
            'bytes': f.stat().st_size if f and f.exists() else None,
            'bound_by': ('state_sha' if s.get('state_sha') else
                         ('state' if s.get('state') else
                          ('state_id' if s.get('state_id') is not None else None))),
            'state_id': s.get('state_id'),
            'colored': (s.get('content') or {}).get('colored'),
            'paint_settled': s.get('paint_settled'),
        })

    n_state_full = sum(1 for e in events
                       if csp.ctype(e) == 'viewer.state-change'
                       and isinstance(e.get('state'), dict) and e['state'].get('position'))
    t0 = next((e.get('t_wall') for e in events if e.get('t_wall')), None)
    t1 = next((e.get('t_wall') for e in reversed(events) if e.get('t_wall')), None)

    return {
        'pack_dir': str(pdir),
        'session_id': pack.get('session_id'),
        'collector_version': pack.get('collector_version'),
        'bytes': dir_size(pdir),
        'n_events': len(events),
        'n_state_full': n_state_full,
        'n_notes': len(notes),
        'n_shots': len(shots),
        'n_shots_usable': sum(1 for r in shot_rows if r['usable']),
        'duration_s': round((t1 - t0) / 1000.0, 1) if t0 and t1 else None,
        'counts': dict(sorted(counts.items(), key=lambda kv: -kv[1])),
        'config': (manifest.get('config') or {}),
        'health': pack.get('health') or manifest.get('health'),
        'integrity': 'present' if manifest.get('integrity') else 'missing',
        'anchors': anchors,
        'anchors_dropped': dropped,
        'shots': shot_rows,
    }



def _lines_sha256(lines):
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln.encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def verify_pack(path):
    """校验 manifest.integrity：事件/备注/索引的文本哈希 + PNG 本体哈希。

    只读。返回 {ok, checks, errors, pack_dir}；不抛异常（调用方决定退出码）。
    """
    pack = csp.load_session(path)
    pdir = Path(pack.get('pack_dir') or path)
    m = pack.get('manifest') or {}
    integ = m.get('integrity') or {}
    checks, errors = [], []

    def add(name, ok, detail=None):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            errors.append('%s%s' % (name, '' if detail is None else '：%s' % detail))

    def read_lines(rel):
        f = pdir / rel
        if not f.exists():
            return None
        return [x for x in f.read_text('utf-8').splitlines() if x.strip()]

    ev = read_lines('client/events.jsonl')
    nt = read_lines('client/notes.jsonl')
    sh = read_lines('client/shots/shots.jsonl')
    add('events.jsonl 存在', ev is not None, str(pdir / 'client/events.jsonl'))
    add('notes.jsonl 存在', nt is not None)
    add('shots.jsonl 存在', sh is not None)
    if ev is not None and integ.get('events_sha256'):
        add('events.jsonl SHA256', _lines_sha256(ev) == integ['events_sha256'])
    if nt is not None and integ.get('notes_sha256'):
        add('notes.jsonl SHA256', _lines_sha256(nt) == integ['notes_sha256'])
    if sh is not None and integ.get('shots_sha256'):
        add('shots.jsonl SHA256', _lines_sha256(sh) == integ['shots_sha256'])
    if ev is not None and integ.get('n_events') is not None:
        add('事件数', len(ev) == integ['n_events'],
            '%s vs %s' % (len(ev), integ['n_events']))
    if sh is not None and integ.get('n_shots') is not None:
        add('关键帧索引数', len(sh) == integ['n_shots'],
            '%s vs %s' % (len(sh), integ['n_shots']))

    png_hashes = integ.get('png_sha256') or {}
    n_png = 0
    for row in pack.get('shots') or []:
        ref = row.get('image_ref')
        if not ref:
            continue
        f = pdir / 'client' / ref
        if not f.is_file():
            add('PNG 存在 %s' % ref, False)
            continue
        n_png += 1
        raw = f.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        expect = row.get('png_sha256') or png_hashes.get(ref)
        if expect:
            add('PNG SHA256 %s' % ref, sha == expect)
        else:
            add('PNG 可读 %s' % ref, True, '旧包无 PNG 哈希，只验存在')
    if integ.get('n_png') is not None:
        add('PNG 文件数', n_png == integ['n_png'],
            '%s vs %s' % (n_png, integ['n_png']))
    add('没有错误', not errors, errors[:3] if errors else None)
    return {'ok': not errors, 'pack_dir': str(pdir),
            'checks': checks, 'errors': errors}


def report(s):
    print('包：%s' % csp.relpath(s['pack_dir']))
    print('会话：%s ｜ 采集器 %s ｜ %s ｜ %s 事件'
          % (s['session_id'], s['collector_version'], human(s['bytes']), s['n_events']))
    if s['duration_s'] is not None:
        print('时长：%ss ｜ 完整 state 变化 %d 次 ｜ 备注 %d 条 ｜ integrity %s'
              % (s['duration_s'], s['n_state_full'], s['n_notes'], s['integrity']))
    h = s.get('health') or {}
    dropped = h.get('dropped') or 0
    ck_dropped = h.get('checkpoint_dropped') or 0
    gaps = h.get('gaps') or []
    if dropped or ck_dropped or gaps:
        print('完整性告警（这段可能没记全）：')
        if dropped:
            ov = (h.get('overflow') or {}).get('events') or {}
            print('  [丢事件] %d 条（上限 %s，首次丢弃 t_wall=%s）'
                  % (dropped, ov.get('limit') or '?',
                     ov.get('first_t_wall') or '?'))
        if ck_dropped:
            print('  [丢检查点] %d 个（上限 400）' % ck_dropped)
        if gaps:
            print('  [缺口] %d 次，已闭合总时长 %.1f s，未闭合 %d 次'
                  % (len(gaps), (h.get('gaps_total_ms') or 0) / 1000.0,
                     h.get('gaps_open') or 0))
            for g in gaps[:6]:
                t1 = g.get('t_end')
                print('    %-16s %s → %s'
                      % (g.get('reason'), g.get('t_wall'),
                         t1 if t1 is not None else '（未闭合）'))
    print()
    print('事件构成：')
    for k, v in s['counts'].items():
        print('  %-22s %d' % (k, v))
    print()
    print('锚点（能用于回放的）：%d' % len(s['anchors']))
    for a in s['anchors']:
        print('  [ok] %-14s state_id=%-4s%s'
              % (a['reason'], a['state_id'],
                 (' sha=%s' % a['state_sha']) if a['state_sha'] else ''))
    if s['anchors_dropped']:
        print('  被丢弃的 checkpoint（没有完整 state，例如只有 layout）：%d' %
              len(s['anchors_dropped']))
        for a in s['anchors_dropped']:
            print('  [skip] %-12s state_id=%s' % (a['reason'], a['state_id']))
    print()
    print('关键帧：%d 张（可用于像素比对 %d 张）'
          % (s['n_shots'], s['n_shots_usable']))
    print('  %-8s %-16s %-11s %-9s %-10s %s' %
          ('shot', 'capture_status', 'px', 'bytes', 'colored', 'bound_by'))
    for r in s['shots']:
        print('  %-8s %-16s %-11s %-9s %-10s %s'
              % (r['shot_id'], r['capture_status'],
                 '%sx%s' % tuple(r['px']) if r['px'][0] else '?',
                 human(r['bytes']) if r['bytes'] else '?',
                 ('%.3f' % r['colored']) if r['colored'] is not None else '?',
                 r['bound_by'] or '?'))
    print()
    print('怎么用：')
    print('  看包结构    ls -R %s' % s['pack_dir'])
    print('  逐段回放    python3 scripts/replay_manual_session.py --pack %s --headed'
          % s['pack_dir'])
    print('  脚本读取    import cave_session_pack as csp; '
          'csp.load_session(%r)' % s['pack_dir'])


def main(argv=None):
    ap = argparse.ArgumentParser(description='检查会话包：内容 / 健康度 / 锚点 / 关键帧')
    ap.add_argument('pack', nargs='?', help='会话包目录或导出的 JSON（默认取最新）')
    ap.add_argument('--json', action='store_true', help='输出 JSON（给脚本用）')
    ap.add_argument('--verify', action='store_true',
                    help='校验 manifest.integrity 与 PNG 本体哈希（不写文件）')
    args = ap.parse_args(argv)

    path = Path(args.pack) if args.pack else latest_pack()
    if path is None:
        print('[错误] 找不到会话包：%s' % csp.relpath(pack_root()),
              file=sys.stderr)
        return 2
    if not path.exists():
        print('[错误] 路径不存在：%s' % path, file=sys.stderr)
        return 2

    if args.verify:
        v = verify_pack(path)
        if args.json:
            print(json.dumps(v, ensure_ascii=False, indent=2))
        else:
            print('包：%s' % csp.relpath(v['pack_dir']))
            for c in v['checks']:
                print('  [%s] %s%s' % ('PASS' if c['ok'] else 'FAIL', c['name'],
                                       ('' if c['detail'] is None
                                        else ' — %s' % (c['detail'],))))
            print('RESULT: %s' % ('PASS' if v['ok'] else 'FAIL'))
        return 0 if v['ok'] else 1

    s = summarize(path)
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        report(s)
    if not s['n_shots_usable']:
        print('\n[提示] 没有 captured 关键帧：回放会跳过像素比对，只验位置/尺度。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
