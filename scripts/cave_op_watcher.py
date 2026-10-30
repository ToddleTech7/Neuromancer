"""cave_op_watcher v0 —— 服务端采集器原型（op tailer）。

契约（见 docs/CAVE_PROCESS_COLLECTION_DESIGN.md §3.2）：
  时间窗 → delta_roots → 变更 root 的 change log → 窗口内 op_ids →
  operation_details → 规范化记录追加到 ops.jsonl（按 op_id 去重）。

本原型已在公开数据栈（minnie65_public）实测；接入自建 CAVE 时仅需改
--server / --datastack（若部署支持直读历史存储，替换 fetch 部分即可）。
只读，不写入远端；失败如实记录不中断。

变更：
- v0.1（2026-09-29）：合并 tabular_change_log 行信息（before/after root、
  is_merge、user_name/affiliation），类型优先取 is_merge。

用法：
  python scripts/cave_op_watcher.py --since "2025-12-08T07:15:00" \
      --until "2025-12-08T07:25:00"
  python scripts/cave_op_watcher.py --since-minutes 15
  python scripts/cave_op_watcher.py --live --interval 300 --lookback 600
"""

import argparse
import datetime as dt
import json
import time
from pathlib import Path

from caveclient import CAVEclient

DEFAULT_OUT = (Path(__file__).resolve().parents[1]
               / 'results' / 'cave_watch' / 'ops.jsonl')


def parse_time(s):
    t = dt.datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc)


def load_seen(out_path):
    seen = set()
    if out_path.exists():
        for line in out_path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                seen.add(int(json.loads(line)['op_id']))
            except Exception:
                continue
    return seen


def derive_type(detail):
    added = detail.get('added_edges') or []
    removed = detail.get('removed_edges') or []
    if removed:
        return 'split'
    if added:
        return 'merge'
    return 'unknown'


def _clean_val(v):
    try:
        if v is None:
            return None
        if isinstance(v, float) and v != v:      # NaN
            return None
        if hasattr(v, 'item'):
            v = v.item()
        if isinstance(v, (int, float, bool, str, list, dict)):
            return v
        return str(v)
    except Exception:
        return str(v)


def normalize(op_id, detail, fetched_at, row_info=None):
    row_info = row_info or {}
    if row_info.get('is_merge') is True:
        op_type = 'merge'
    elif row_info.get('is_merge') is False:
        op_type = 'split'
    else:
        op_type = derive_type(detail)
    return {
        'op_id': int(op_id),
        'type': op_type,
        'user': detail.get('user'),
        'user_name': row_info.get('user_name'),
        'user_affiliation': row_info.get('user_affiliation'),
        'timestamp': detail.get('timestamp') or detail.get('operation_ts'),
        'before_root_ids': row_info.get('before_root_ids'),
        'after_root_ids': row_info.get('after_root_ids'),
        'roots': detail.get('roots'),
        'added_edges': detail.get('added_edges'),
        'removed_edges': detail.get('removed_edges'),
        'source_coords': detail.get('source_coords'),
        'sink_coords': detail.get('sink_coords'),
        'operation_status': detail.get('operation_status'),
        'operation_exception': detail.get('operation_exception'),
        'change_log_row': row_info or None,
        'fetched_at': fetched_at,
        'raw': detail,
    }


def _to_utc(ts):
    if ts is None or (isinstance(ts, float) and ts != ts):
        return None
    if isinstance(ts, str):
        try:
            ts = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except ValueError:
            return None
    if hasattr(ts, 'to_pydatetime'):
        ts = ts.to_pydatetime()
    if isinstance(ts, (int, float)):
        ts = dt.datetime.fromtimestamp(ts / 1000, tz=dt.timezone.utc)
    if not isinstance(ts, dt.datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return ts.astimezone(dt.timezone.utc)


def find_col(df, *keywords):
    for c in df.columns:
        lc = str(c).lower()
        if all(k in lc for k in keywords):
            return c
    return None


def collect_window(cl, t0, t1, seen, debug=False):
    kg = cl.chunkedgraph
    res = {'delta_roots_found': 0, 'op_ids_in_window': 0, 'new_ops': 0,
           'errors': []}
    try:
        past_roots, future_roots = kg.get_delta_roots(t0, t1)
    except Exception as err:
        msg = str(err)
        if 'Inverted or empty timestamp range' in msg:
            res['errors'].append(
                '窗口晚于数据栈最新数据时间（按空数据处理；'
                'minnie65_public 数据截止约 2026-06-27）')
        else:
            res['errors'].append(f'delta_roots: {type(err).__name__}: '
                                 f'{msg[:160]}')
        return res, []
    if debug:
        print('  [debug] delta_roots 返回: past=%d future=%d' %
              (len(past_roots), len(future_roots)))
    roots = sorted({int(x) for x in list(past_roots) + list(future_roots)})
    res['delta_roots_found'] = len(roots)
    if not roots:
        return res, []

    try:
        logs = kg.get_tabular_change_log(roots, filtered=False)
    except Exception as err:
        res['errors'].append(f'tabular_change_log: {type(err).__name__}: '
                             f'{str(err)[:160]}')
        return res, []

    op_rows = {}
    printed = False
    for root, df in logs.items():
        if df is None or len(df) == 0:
            continue
        if debug and not printed:
            print('  [debug] change log 列:', list(df.columns))
            printed = True
        tcol = find_col(df, 'time')
        ocol = (find_col(df, 'operation', 'id')
                or find_col(df, 'operation_id'))
        if not (tcol and ocol):
            res['errors'].append(f'root {root} 缺列: {list(df.columns)[:8]}')
            continue
        for _, row in df.iterrows():
            ts = _to_utc(row[tcol])
            if ts is not None and t0 <= ts < t1:
                op_id = int(row[ocol])
                if op_id not in op_rows:
                    op_rows[op_id] = {str(k): _clean_val(v)
                                      for k, v in row.items()}
    res['op_ids_in_window'] = len(op_rows)

    new_ids = sorted(i for i in op_rows if i not in seen)
    if not new_ids:
        return res, []
    try:
        details = kg.get_operation_details(new_ids)
    except Exception as err:
        res['errors'].append(f'operation_details: {type(err).__name__}: '
                             f'{str(err)[:160]}')
        return res, []

    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    records = []
    for op_id in new_ids:
        detail = details.get(str(op_id))
        if not isinstance(detail, dict):
            res['errors'].append(f'op {op_id}: 详情缺失')
            continue
        records.append(normalize(op_id, detail, fetched_at,
                                 op_rows.get(op_id)))
    res['new_ops'] = len(records)
    return res, records


def main():
    ap = argparse.ArgumentParser(description='cave_op_watcher v0 (op tailer)')
    ap.add_argument('--datastack', default='minnie65_public')
    ap.add_argument('--server', default=None)
    ap.add_argument('--since', help='ISO 时间，如 2025-12-08T07:15:00')
    ap.add_argument('--until', help='ISO 时间；默认现在')
    ap.add_argument('--since-minutes', type=float, help='距现在 N 分钟')
    ap.add_argument('--live', action='store_true', help='持续轮询')
    ap.add_argument('--interval', type=float, default=300,
                    help='live 轮询间隔（秒）')
    ap.add_argument('--lookback', type=float, default=600,
                    help='live 回看窗口（秒）')
    ap.add_argument('--out', default=str(DEFAULT_OUT))
    ap.add_argument('--debug', action='store_true')
    args = ap.parse_args()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    kwargs = {}
    if args.server:
        kwargs['server_address'] = args.server
    cl = CAVEclient(args.datastack, **kwargs)
    print('datastack:', args.datastack, '| server:',
          cl.chunkedgraph.server_address)
    seen = load_seen(out_path)
    print('已存 op 数:', len(seen), '| 输出:', out_path)

    def run_window(t0, t1):
        res, recs = collect_window(cl, t0, t1, seen, debug=args.debug)
        with out_path.open('a', encoding='utf-8') as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False, default=str)
                        + '\n')
                seen.add(r['op_id'])
        print(f"[{t0.isoformat(timespec='seconds')} → "
              f"{t1.isoformat(timespec='seconds')}] "
              f"delta_roots={res['delta_roots_found']} "
              f"窗口内op={res['op_ids_in_window']} "
              f"新增入库={res['new_ops']}")
        for err in res['errors'][:5]:
            print('  [ERR]', err)
        return res['new_ops']

    if args.since_minutes is not None:
        t1 = dt.datetime.now(dt.timezone.utc)
        t0 = t1 - dt.timedelta(minutes=args.since_minutes)
        total = run_window(t0, t1)
        print('完成，本次新增', total, '条 →', out_path)
    elif args.since:
        t0 = parse_time(args.since)
        t1 = (parse_time(args.until) if args.until
              else dt.datetime.now(dt.timezone.utc))
        total = run_window(t0, t1)
        print('完成，本次新增', total, '条 →', out_path)
    elif args.live:
        print(f'live 模式：每 {args.interval}s 轮询，回看 '
              f'{args.lookback}s')
        while True:
            t1 = dt.datetime.now(dt.timezone.utc)
            t0 = t1 - dt.timedelta(seconds=args.lookback)
            try:
                run_window(t0, t1)
            except KeyboardInterrupt:
                print('停止。')
                break
            except Exception as err:
                print('  [ERR] 轮询失败:', type(err).__name__,
                      str(err)[:200])
            time.sleep(args.interval)
    else:
        ap.error('需要 --since / --since-minutes 或 --live')


if __name__ == '__main__':
    main()
