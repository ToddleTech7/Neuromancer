"""把 /draft 落下的草稿转成正式会话包（崩溃/误关恢复用）。

草稿是采集器每 30 秒增量 POST 到 `results/manual_sessions/_draft_<session>/` 的
`events.jsonl` + `shots/`（图也增量传）+ `draft.json`，崩溃最多丢最后 30 秒。
事件、状态快照、缺口标记、关键帧都在，回放/派生/像素比对都能做。

用法：
    python3 scripts/recover_draft.py                 # 最新草稿
    python3 scripts/recover_draft.py --list          # 看有哪些草稿
    python3 scripts/recover_draft.py --draft <目录>
"""

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp            # noqa: E402

SESSIONS = Path(os.environ.get('NM_SESSIONS')
                or (ROOT / 'results' / 'manual_sessions')).expanduser()


def drafts():
    if not SESSIONS.is_dir():
        return []
    ds = [d for d in SESSIONS.iterdir()
          if d.is_dir() and d.name.startswith('_draft_')]
    ds.sort(key=lambda d: (d / 'draft.json').stat().st_mtime
            if (d / 'draft.json').exists() else d.stat().st_mtime,
            reverse=True)
    return ds


def load_draft(d):
    meta_p = d / 'draft.json'
    if not meta_p.exists():
        raise SystemExit('不是草稿目录（缺 draft.json）：%s' % d)
    meta = json.loads(meta_p.read_text('utf-8'))
    ev_p = d / 'events.jsonl'
    events = []
    if ev_p.exists():
        for line in ev_p.read_text('utf-8').splitlines():
            line = line.strip()
            if line:
                events.append(json.loads(line))
    notes = []
    nt_p = d / 'notes.jsonl'
    if nt_p.exists():
        for line in nt_p.read_text('utf-8').splitlines():
            line = line.strip()
            if line:
                notes.append(json.loads(line))
    shots = []
    idx = d / 'shots' / 'shots.jsonl'
    if idx.exists():
        for line in idx.read_text('utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            png = d / 'shots' / (row.get('image_ref') or '')
            if not png.is_file():
                continue
            import base64
            row['dataURL'] = ('data:image/png;base64,'
                              + base64.b64encode(png.read_bytes()).decode())
            shots.append(row)
    sid = str(meta.get('session_id') or d.name)
    return {
        'schema': 'cave_recorder.session.v0.2',
        'collector_version': meta.get('collector_version') or 'unknown',
        'session_id': sid + '_recovered',
        'page': meta.get('page') or {},
        'time': meta.get('time') or {},
        'config': meta.get('config') or {},
        'counts': meta.get('counts') or {},
        'collector': {
            'version': meta.get('collector_version') or 'unknown',
            'gaps': [],
            'pending_writes': [],
            'capabilities': {},
            'recovered_from': str(d),
        },
        'events': events,
        'notes': notes,
        'shots': shots,
        'errors': [],
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--draft', default=None, help='草稿目录（默认最新）')
    ap.add_argument('--out', default=str(SESSIONS))
    ap.add_argument('--list', action='store_true')
    args = ap.parse_args(argv)

    ds = drafts()
    if args.list:
        if not ds:
            print('没有草稿')
            return 0
        for d in ds:
            meta = {}
            try:
                meta = json.loads((d / 'draft.json').read_text('utf-8'))
            except Exception:
                pass
            print('%-60s 事件 %-7s 更新 %s' % (
                d.name, meta.get('last_seq'), meta.get('updated_wall_ms')))
        return 0
    if args.draft:
        d = Path(args.draft)
    elif ds:
        d = ds[0]
    else:
        raise SystemExit('没有可恢复的草稿')
    pack = load_draft(d)
    out, man = csp.pack_session(
        pack, Path(args.out).expanduser(),
        legs={'client_process': 'real'},
        reasons={'client_process': '由真实采集的增量草稿恢复'})
    h = man.get('health') or {}
    print('已恢复：%s' % out)
    print('事件 %d（seq %s..%s）｜缺口 %d 次（未闭合 %d）｜丢弃 %d'
          % (h.get('n_events'), h.get('seq_first'), h.get('seq_last'),
             len(h.get('gaps') or []), h.get('gaps_open') or 0,
             h.get('dropped') or 0))
    n_shots = len(pack.get('shots') or [])
    if n_shots:
        print('关键帧：%d 张（草稿增量落盘的图）' % n_shots)
    else:
        print('注意：这份草稿没有关键帧 PNG，像素比对会跳过；位置/选中集/缺口都在。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
