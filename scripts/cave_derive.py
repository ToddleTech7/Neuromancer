"""cave_derive：从 op log 派生"存活/演化"结局（正例质检的 L1 基础）。

契约（设计 docs/CAVE_PROCESS_COLLECTION_DESIGN.md §2.6(3)）：
  正例派生 = 存活过滤（存活 N 天、未被撤销、未被再拆）；
  负例 = 撤销/被拒。本脚本只做公开 API 可观测的部分：

- is_latest_roots(after)    after root 在数据截止时是否仍是最新 root
- get_lineage_graph(after)  谱系图节点自带 {operation_id, timestamp} 属性
  （2025-12-08 公开数据实测校准）：
    operation_id = 该 root 被"消费"（再拆/再合并）的操作号
                   （仍存活时不能据此判断，按 is_latest 分支处理）；
    timestamp    = 该 root 的诞生时刻
  links 方向 source→target=祖先→后代（target 即后继 root）
- get_operation_details(后继 op)（批量）：类别（加边=merge / 删边=split）、
  时间戳、前后 root
- survival_seconds = 首个后继消费操作时刻 − op 时刻；
  无后继 → 右删失（censored=True，生存期截断于 checked_at，
  不是"永久存活"）

如实边界（项目纪律）：
- 存活 ≠ 一定正确；撤销 ≠ 一定错误（可能因新证据变化）。
- 只取"首个后继消费操作"（每个 after root 至多一个）；更后代际不展开。
- "被撤销"（undo）不直接判定：undo 语义需与服务端 undo 链对齐（C0 后），
  本阶段如实标 None，不猜。
- 派生字段可重算：输出附 checked_at 与右删失语义。

用法（仓库根目录）：
    python scripts/cave_derive.py                 # 默认 ops.jsonl 全量
    python scripts/cave_derive.py --limit 10      # 试跑前 N 条
输出（拒绝覆盖，重名追加时间戳）：
    results/cave_watch/derived_outcomes_<ts>.jsonl
    results/cave_watch/derived_summary_<ts>.json
"""

import argparse
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from cave_session_pack import relpath  # noqa: E402
from cave_join import parse_server_ts  # noqa: E402

ROOT = HERE.parent
DEFAULT_OPS = ROOT / 'results' / 'cave_watch' / 'ops.jsonl'
OUT_DIR = ROOT / 'results' / 'cave_watch'


def _clean(v):
    try:
        if v is None:
            return None
        if hasattr(v, 'item'):
            v = v.item()
        if isinstance(v, (int, float, bool, str, list, dict)):
            return v
        return str(v)
    except Exception:
        return str(v)


def with_retry(fn, tries=5, wait=6):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as err:
            last = err
            print(f'  重试 {i + 1}/{tries}: {type(err).__name__} '
                  f'{str(err)[:100]}')
            time.sleep(wait)
    raise last


def op_kind_from_edges(detail):
    """从 operation_details 的边变化判类别：加边=merge、删边=split。"""
    added = detail.get('added_edges') or []
    removed = detail.get('removed_edges') or []
    if removed:
        return 'split'
    if added:
        return 'merge'
    return 'unknown'


def fetch_successors(kg, roots, sleep=0.35, verbose=True):
    """对每个 root 取谱系图，提取"消费操作"与后继 root。

    返回 {root(str): {'death_op': int|None, 'birth_ts_ms': float|None,
                      'child_roots': [str], 'error': str|None}}
    """
    out = {}
    for i, root in enumerate(roots):
        rec = {'death_op': None, 'birth_ts_ms': None, 'child_roots': [],
               'error': None}
        try:
            g = with_retry(lambda r=root: kg.get_lineage_graph(
                int(r), as_nx_graph=False))
            node = None
            for nd in g.get('nodes') or []:
                if str(nd.get('id')) == str(root):
                    node = nd
                    break
            if node is not None:
                rec['birth_ts_ms'] = (
                    float(node['timestamp']) * 1000.0
                    if node.get('timestamp') is not None else None)
                if node.get('operation_id') is not None:
                    rec['death_op'] = int(node['operation_id'])
            for link in g.get('links') or []:
                if str(link.get('source')) == str(root):
                    rec['child_roots'].append(str(link.get('target')))
        except Exception as err:
            rec['error'] = f'{type(err).__name__}: {str(err)[:140]}'
        out[str(root)] = rec
        if verbose and (i + 1) % 10 == 0:
            print(f'  谱系图 {i + 1}/{len(roots)}')
        time.sleep(sleep)
    return out


def fetch_op_details(kg, op_ids, chunk=20, sleep=0.4):
    """批量取操作详情 → {op_id(int): {'ts_ms','kind','before','after'}}。"""
    out = {}
    op_ids = sorted({int(x) for x in op_ids if x is not None})
    for i in range(0, len(op_ids), chunk):
        part = op_ids[i:i + chunk]
        det = with_retry(lambda p=part: kg.get_operation_details(p))
        for k, d in (det or {}).items():
            if not isinstance(d, dict):
                continue
            ts = parse_server_ts(d.get('timestamp') or d.get('operation_ts'))
            out[int(k)] = {
                'ts_ms': ts,
                'kind': op_kind_from_edges(d),
                'before': d.get('before_root_ids'),
                'after': d.get('after_root_ids') or d.get('roots'),
                'user': d.get('user'),
            }
        time.sleep(sleep)
    return out


def derive_one(op, latest_map, succ_map, detail_map, checked_at_ms):
    after = [str(x) for x in (op.get('after_root_ids')
                              or op.get('roots') or [])]
    op_ts = parse_server_ts(op.get('timestamp'))
    rec = {
        'op_id': op.get('op_id'),
        'op_type': op.get('type'),
        'op_ts_utc': op.get('timestamp'),
        'user': op.get('user'),
        'after_root_ids': after,
    }
    if not after:
        rec.update({'state': 'no_after_roots', 'is_latest_now': None,
                    'successor_ops': [], 'n_successor_ops': 0,
                    'survival_seconds': None, 'censored': None,
                    'undo_detected': None})
        return rec

    latest_vals = [latest_map.get(r) for r in after]
    all_latest = (all(v is True for v in latest_vals)
                  if all(v is not None for v in latest_vals) else None)

    # 收集各 after root 的首个后继消费操作（带诞生时间合理性校验）
    successors = []
    for r in after:
        if latest_map.get(r) is True:
            continue   # 仍存活
        s = succ_map.get(r) or {}
        death_op = s.get('death_op')
        if death_op is None:
            continue
        d = detail_map.get(int(death_op)) or {}
        ts = d.get('ts_ms')
        birth = s.get('birth_ts_ms')
        if ts is None:
            continue
        if birth is not None and ts <= birth:
            # 语义漂移防护：消费操作不能早于诞生
            continue
        successors.append({
            'root': r, 'op_id': int(death_op), 'ts_ms': ts,
            'kind': d.get('kind'), 'before_root_ids': d.get('before'),
            'after_root_ids': d.get('after'),
            'child_roots': s.get('child_roots') or [],
        })

    if not successors:
        state = 'latest' if all_latest else 'not_latest_no_successor'
        survival_s = ((checked_at_ms - op_ts) / 1000.0
                      if op_ts is not None else None)
        censored = True
        next_chg = None
    else:
        successors.sort(key=lambda x: x['ts_ms'])
        first = successors[0]
        state = ('later_merged' if first['kind'] == 'merge'
                 else 'later_split' if first['kind'] == 'split'
                 else 'later_other')
        survival_s = ((first['ts_ms'] - op_ts) / 1000.0
                      if op_ts is not None else None)
        censored = False
        next_chg = first
        for s in successors:
            s.pop('ts_ms', None)

    rec.update({
        'state': state,
        'is_latest_now': all_latest,
        'next_change': next_chg,
        'successor_ops': successors,
        'n_successor_ops': len(successors),
        'survival_seconds': (round(survival_s, 1)
                             if survival_s is not None else None),
        'censored': censored,
        'undo_detected': None,
        'notes': ['存活≠一定正确，撤销≠一定错误（设计 §2.6(3)）；'
                  'censored=True 表示生存期截断于 checked_at；'
                  '仅首个后继操作，更后代际未展开'],
    })
    return rec


def derive_all(ops, kg, checked_at_ms=None, verbose=True):
    if checked_at_ms is None:
        checked_at_ms = datetime.now(timezone.utc).timestamp() * 1000.0
    all_roots = []
    for op in ops:
        for r in (op.get('after_root_ids') or op.get('roots') or []):
            r = str(r)
            if r not in all_roots:
                all_roots.append(r)
    errors = []
    latest_map = {}
    if all_roots:
        try:
            vals = with_retry(lambda: kg.is_latest_roots(all_roots))
            for r, v in zip(all_roots, list(vals)):
                latest_map[r] = bool(v)
        except Exception as err:
            errors.append(f'is_latest_roots: {type(err).__name__}: '
                          f'{str(err)[:160]}')
    need = [r for r in all_roots if latest_map.get(r) is False]
    succ_map = {}
    if need:
        if verbose:
            print(f'  非存活 root {len(need)}/{len(all_roots)}，取谱系图…')
        succ_map = fetch_successors(kg, need, verbose=verbose)
        for r, s in succ_map.items():
            if s.get('error'):
                errors.append(f'lineage {r}: {s["error"]}')
    death_ops = [s['death_op'] for s in succ_map.values()
                 if s.get('death_op') is not None]
    detail_map = {}
    if death_ops:
        if verbose:
            print(f'  后继操作详情 {len(set(death_ops))} 个…')
        try:
            detail_map = fetch_op_details(kg, death_ops)
        except Exception as err:
            errors.append(f'op_details: {type(err).__name__}: '
                          f'{str(err)[:160]}')
    records = [derive_one(op, latest_map, succ_map, detail_map, checked_at_ms)
               for op in ops]
    return records, errors, checked_at_ms


def summarize(records, errors, checked_at_ms):
    states = {}
    surv = []
    for r in records:
        states[r['state']] = states.get(r['state'], 0) + 1
        if not r['censored'] and r['survival_seconds'] is not None:
            surv.append(r['survival_seconds'])
    return {
        'checked_at': datetime.fromtimestamp(
            checked_at_ms / 1000.0, tz=timezone.utc).isoformat(),
        'n_ops': len(records),
        'states': states,
        'n_censored': sum(1 for r in records if r['censored'] is True),
        'median_survival_s_changed': (round(statistics.median(surv), 1)
                                      if surv else None),
        'errors': errors,
        'notes': [
            'state: latest/not_latest_no_successor/later_merged/'
            'later_split/later_other/no_after_roots',
            'survival 仅在发生后续消费操作时有值（否则右删失于 checked_at）',
            'undo_detected 恒为 None：undo 链对齐待 C0（不猜测）',
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--ops', default=str(DEFAULT_OPS))
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--out-dir', default=str(OUT_DIR))
    args = ap.parse_args()

    from caveclient import CAVEclient
    cl = CAVEclient('minnie65_public')
    ops = [json.loads(l) for l in
           Path(args.ops).read_text(encoding='utf-8').splitlines()
           if l.strip()]
    if args.limit:
        ops = ops[:args.limit]
    print(f'ops 载入 {len(ops)} 条；查询 is_latest + 谱系图 + 操作详情…')
    records, errors, checked_at = derive_all(ops, cl.chunkedgraph)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jl = out_dir / f'derived_outcomes_{ts}.jsonl'
    with jl.open('w', encoding='utf-8') as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')
    summary = summarize(records, errors, checked_at)
    sm = out_dir / f'derived_summary_{ts}.json'
    sm.write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                  encoding='utf-8')

    print('states:', summary['states'])
    print(f'右删失 {summary["n_censored"]}/{summary["n_ops"]}；'
          f'发生后继者生存期中位数 = '
          f'{summary["median_survival_s_changed"]}s')
    for e in errors[:5]:
        print('  [ERR]', e)
    print('outcomes:', jl)
    print('summary:', relpath(sm))


if __name__ == '__main__':
    main()
