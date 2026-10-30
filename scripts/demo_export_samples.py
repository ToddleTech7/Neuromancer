"""样本导出演示：真实公开数据 → 可训练样本卡（端到端实际跑通）。

对 `results/cave_watch/ops.jsonl` 中选定的真实 op（公开栈采集）实际执行：
  1. EM 三视图重建（CB2 实测参数：S=3584nm、中心 = interface+(16,-32,Δz)、mip1）
  2. 分割成员 mask（autoseg + get_roots(t−1s)；before 两段 → G/B 通道）
  3. 结局标签（derived_outcomes：later_merged / later_split / latest + 生存期）
  4. 合成样本卡：{em_xy, em_xz, em_yz, overlay_xz}.png + sample.json
产物：results/export_demo/（sample_<op>/ 目录 + index.jsonl + contact_sheet.png）

用法（仓库根目录）：
  python3 scripts/demo_export_samples.py                # 自动选 3 条（各演化状态 1 条）
  python3 scripts/demo_export_samples.py --ops 1956846 1956803
  python3 scripts/demo_export_samples.py --all --out results/dataset_v0
                                                        # 全量导出可命中样本（数据集 v0）

如实边界：
- 过程列（会话/视角/备注/关键帧）来自 recorder 会话包；公开 ops 与本地录制会话
  时间不相交时为 null（结构保留）。传 --join 且命中时，process 列由 join 填充。
- G/B 语义按 segment1/2 = before reversed 映射（原 cb2_align 规则；待发布方确认）。
- 五段 provenance（service_facts / visual_assets / client_process /
  submit_execution / join）逐条写给样本；未声明即 unknown，不允许留空冒充 real。

口径（用户 2026-10-26 确认）：公开栈只读，submit_execution 只能 simulated；
其余四段可以是 real（真 op 记录 / 钉住版本重渲染 / 真录制 / 真 join）。
"""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from cloudvolume import CloudVolume
from caveclient import CAVEclient


ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / 'results' / 'cave_watch' / 'ops.jsonl'
WATCH = ROOT / 'results' / 'cave_watch'
OUT = ROOT / 'results' / 'export_demo'
EM_URL = ('precomputed://https://storage.googleapis.com/'
          'iarpa_microns/minnie/minnie65/em')
AUTOSEG_URL = ('precomputed://https://storage.googleapis.com/'
               'iarpa_microns/minnie/minnie65/autoseg')
S = 3584.0
API_CHUNK = 2000
API_SLEEP = 0.15


def with_retry(fn, tries=6, wait=6):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as err:
            last = err
            print(f'    重试 {i + 1}/{tries}: {type(err).__name__}')
            time.sleep(wait)
    raise last


def load_join(path):
    """读 join.jsonl（cave_join.py 产出），按 op_id 建索引。"""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        print(f'[warn] join 文件不存在: {p}')
        return {}
    out = {}
    for line in p.read_text('utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get('op_id') is not None:
            out[int(r['op_id'])] = r
    return out


def load_pack(pack_dir):
    """读会话包目录：返回 (manifest, shots)。"""
    if not pack_dir:
        return None, []
    d = Path(pack_dir)
    man = json.loads((d / 'manifest.json').read_text('utf-8'))
    shots = []
    sp = d / 'client' / 'shots' / 'shots.jsonl'
    if sp.exists():
        for line in sp.read_text('utf-8').splitlines():
            line = line.strip()
            if line:
                shots.append(json.loads(line))
    return man, shots


def default_provenance():
    return {k: {'status': 'unknown', 'reason': '未提供来源标注'}
            for k in ('service_facts', 'visual_assets', 'client_process',
                      'submit_execution', 'join')}


def build_process(op_id, join_map, provenance, shots):
    """把 join 记录变成样本的 process 列；无匹配会话时返回 null 占位。"""
    rec = join_map.get(int(op_id))
    if not rec:
        return {
            'session_id': None, 'human_decision': None, 'human_note': None,
            'note': '本样本无匹配会话；process 列保留结构，不推断填补',
            'process_ref': None, 'viewpoint_before': None, 'window': None,
            'n_notes': None, 'n_multicuts': None, 'keyframes': [],
        }
    ctx = rec.get('context') or {}
    notes = [n.get('text') for n in (ctx.get('notes') or []) if n.get('text')]
    anchor = rec.get('anchor') or {}
    req_id = anchor.get('request_id')
    kfs = [s for s in (shots or [])
           if s.get('request_id') and s.get('request_id') == req_id]
    return {
        'session_id': rec.get('session_id'),
        'human_decision': 'accept',
        'human_note': notes[0] if notes else None,
        'note': (f"join={rec.get('match_method')}；"
                 f"时钟偏移 {rec.get('clock_offset_ms')}ms"
                 f"±{rec.get('clock_uncertainty_ms')}ms"
                 + ('（提交腿为仿真的本地网关回执）'
                    if (provenance or {}).get('submit_execution', {})
                    .get('status') == 'simulated' else '')),
        'process_ref': {
            'join_record_op_id': rec.get('op_id'),
            'session_id': rec.get('session_id'),
            'match_method': rec.get('match_method'),
            'client_request_id': req_id,
            'clock_offset_ms': rec.get('clock_offset_ms'),
            'clock_uncertainty_ms': rec.get('clock_uncertainty_ms'),
        },
        'viewpoint_before': ctx.get('viewpoint_before'),
        'window': ctx.get('window'),
        'n_notes': len(notes),
        'n_multicuts': len(ctx.get('multicuts') or []),
        'keyframes': [{'shot_id': s.get('shot_id'),
                       'moment': s.get('moment'),
                       'image_ref': s.get('image_ref'),
                       'capture_status': s.get('capture_status'),
                       'source': s.get('source')}
                      for s in kfs],
    }


def load_ops(path):
    ops = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            ops[int(r['op_id'])] = r
        except Exception:
            continue
    return ops


def load_latest_outcomes():
    files = sorted(WATCH.glob('derived_outcomes_*.jsonl'))
    out = {}
    if not files:
        return out
    for line in files[-1].read_text(encoding='utf-8').splitlines():
        try:
            r = json.loads(line)
            out[int(r['op_id'])] = r
        except Exception:
            continue
    return out


def pick_default(outcomes, ops, n=3):
    picks = []
    for state in ('later_split', 'later_merged', 'latest'):
        for oid, r in outcomes.items():
            if r.get('state') == state and oid in ops:
                picks.append(oid)
                break
    for oid in sorted(ops):
        if len(picks) >= n:
            break
        if oid not in picks:
            picks.append(oid)
    return picks[:n]


def em_view(vol, I, slot, res):
    """EM 单视图重建；返回与 CB2 真值同方向的 224×224。"""
    IX, IY, IZ = I
    rx, ry, rz = res
    p = round(S / rx)
    pz = round(S / rz)
    if slot == 'em_xz':
        a0 = round((IX + 16 - S / 2) / rx)
        b0 = round((IY - 32) / ry)
        z0 = round((IZ + 80 - S / 2) / rz)
        arr = np.asarray(with_retry(
            lambda: vol[a0:a0 + p, b0, z0:z0 + pz]),
            dtype=np.float32).squeeze().T
    elif slot == 'em_yz':
        b0 = round((IX + 32) / rx)
        a0 = round((IY - 40 - S / 2) / ry)
        z0 = round((IZ + 80 - S / 2) / rz)
        arr = np.asarray(with_retry(
            lambda: vol[b0, a0:a0 + p, z0:z0 + pz]),
            dtype=np.float32).squeeze().T
    else:  # em_xy
        a0 = round((IX + 16 - S / 2) / rx)
        b0 = round((IY - 32 - S / 2) / ry)
        z0 = round((IZ + 40) / rz)
        arr = np.asarray(with_retry(
            lambda: vol[a0:a0 + p, b0:b0 + p, z0]),
            dtype=np.float32).squeeze().T
    img = Image.fromarray(arr.astype(np.uint8)).resize(
        (224, 224), Image.BILINEAR)
    return np.asarray(img, dtype=np.float32).T


def read_autoseg_block(cv, I):
    IX, IY, IZ = I
    x0 = round((IX + 16 - S / 2) / 8)
    y0 = round((IY - 32 - S / 2) / 8)
    z0 = round((IZ + 80 - S / 2) / 40)
    blk = np.asarray(with_retry(
        lambda: cv[x0:x0 + 448, y0:y0 + 448, z0:z0 + 90])).squeeze(-1)
    return blk


def roots_of_block(kg, blk, t_mask):
    uniq, inv = np.unique(blk, return_inverse=True)
    nz = uniq[uniq > 0]
    roots_nz = np.zeros(nz.shape, dtype=np.uint64)
    for i in range(0, len(nz), API_CHUNK):
        part = [int(x) for x in nz[i:i + API_CHUNK]]
        roots_nz[i:i + len(part)] = with_retry(
            lambda p=part: kg.get_roots(p, timestamp=t_mask))
        time.sleep(API_SLEEP)
    lut = np.zeros(len(uniq), dtype=np.uint64)
    lut[uniq > 0] = roots_nz
    return lut[inv].reshape(blk.shape)


def to224(mm):
    t = mm.T
    img = Image.fromarray((t * 255).astype(np.uint8))
    return np.asarray(img.resize((224, 224), Image.NEAREST)).T


def view_mask(M3d, slot):
    if slot == 'em_xy':
        return to224(M3d[:, :, 44].astype(np.uint8))
    if slot == 'em_xz':
        return to224(M3d[:, 224, :].astype(np.uint8))
    return to224(M3d[224, :, :].astype(np.uint8))


def make_overlay(em, g_mask, b_mask):
    e = em.astype(np.float32)
    if e.max() > e.min():
        e = (e - e.min()) / (e.max() - e.min()) * 255.0
    rgb = np.stack([e * 0.85, e * 0.45, e * 0.45], axis=-1)
    if g_mask is not None:
        rgb[..., 1] = np.where(g_mask, 235, rgb[..., 1])
        rgb[..., 0] = np.where(g_mask, rgb[..., 0] * 0.35, rgb[..., 0])
    if b_mask is not None:
        rgb[..., 2] = np.where(b_mask, 235, rgb[..., 2])
        rgb[..., 0] = np.where(b_mask, rgb[..., 0] * 0.35, rgb[..., 0])
    return rgb.clip(0, 255).astype(np.uint8)



# ---------------------------------------------------------------- op 几何
# 从归档的 cb2_align 里保留的两个函数：样本卡只需要"落点中点"和"秒级时间戳"，
# 不需要整套 CB2 列映射（那套已随 CB2 线归档）。
def op_interface_point_nm(op, base_resolution):
    """source 与 sink 落点的中点（× 基分辨率）。

    2026-09-29 实测（op 1956846）：重建值与 CB2 真值精确相等（零误差）。
    注意是两落点的**中点**，不是任一个点。
    """
    src = op.get('source_coords') or []
    snk = op.get('sink_coords') or []
    if not src or not snk:
        return None
    s = [float(x) for x in src[0]]
    k = [float(x) for x in snk[0]]
    return [round((s[i] + k[i]) / 2.0 * base_resolution[i], 3)
            for i in range(3)]


def op_timestamp_seconds(op):
    """epoch 秒（CB2 的 timestamp 是真值整秒截断）。"""
    ts = op.get('timestamp')
    if isinstance(ts, str):
        return int(datetime.fromisoformat(ts).timestamp())
    if isinstance(ts, (int, float)):
        return int(ts)
    return None


def main():
    ap = argparse.ArgumentParser(description='样本导出演示（公开数据）')
    ap.add_argument('--ops', nargs='*', type=int, default=None)
    ap.add_argument('--out', default=None, help='输出目录（默认 results/export_demo）')
    ap.add_argument('--all', action='store_true',
                    help='全量遍历候选（导出所有可命中样本）')
    ap.add_argument('--join', default=None,
                    help='join.jsonl（cave_join 产出）：命中时填充 process 列')
    ap.add_argument('--session-pack', default=None,
                    help='会话包目录（cave_session_pack 产出）：取关键帧与 provenance')
    args = ap.parse_args()

    join_map = load_join(args.join)
    pack_man, pack_shots = load_pack(args.session_pack)
    provenance = (pack_man or {}).get('provenance') or {
        'service_facts': {'status': 'real',
                          'reason': 'minnie65_public 真实 op 记录（op_watcher）'},
        'visual_assets': {'status': 'real',
                          'reason': '按 op 时刻钉住版本重渲染 EM/mask（get_roots(t-1s)）'},
        'client_process': {
            'status': 'real' if pack_man else 'unknown',
            'reason': 'recorder 会话包' if pack_man else '未提供会话包'},
        'submit_execution': {'status': 'unknown',
                             'reason': '本导出不涉及编辑提交'},
        'join': {'status': 'real' if join_map else 'unknown',
                 'reason': 'cave_join 三级匹配' if join_map else '未提供 join'},
    }
    if join_map or pack_man:
        print(f'join 记录 {len(join_map)} 条；会话包 '
              f'{(pack_man or {}).get("session_id")}；'
              f'关键帧 {len(pack_shots)} 张')

    out_dir = Path(args.out) if args.out else OUT
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    ops = load_ops(OPS)
    outcomes = load_latest_outcomes()
    if args.ops:
        candidates = list(args.ops)
        target_n = len(args.ops)
    else:
        pref = pick_default(outcomes, ops, n=3)
        rest = [oid for oid in sorted(ops) if oid not in pref]
        candidates = pref + rest
        target_n = len(candidates) if args.all else 3
    print(f'ops 总数 {len(ops)}；目标样本 {target_n}；候选顺序 {candidates[:8]}...')

    kg = CAVEclient('minnie65_public').chunkedgraph
    vol_em = with_retry(lambda: CloudVolume(EM_URL, mip=1, progress=False,
                                            fill_missing=True))
    vol_auto = with_retry(lambda: CloudVolume(AUTOSEG_URL, mip=0,
                                              progress=False,
                                              fill_missing=True))
    res = [float(x) for x in vol_em.info['scales'][1]['resolution']]
    print(f'EM mip1 res={res}')

    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / 'index.jsonl'
    skipped_path = out_dir / 'skipped.jsonl'
    index_path.write_text('', encoding='utf-8')
    skipped_path.write_text('', encoding='utf-8')

    def emit_row(path, row):
        """增量落盘：导出中途中断也能保留已完成样本的索引。"""
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    index = []
    skipped = []
    tiles = []
    for op_id in candidates:
        if len(index) >= target_n:
            break
        op = ops.get(op_id)
        if op is None or op_interface_point_nm(op, [8, 8, 40]) is None:
            print(f'[skip] op {op_id}: 缺失或无坐标')
            continue
        I = op_interface_point_nm(op, [8, 8, 40])
        t_ref = op_timestamp_seconds(op)
        t_mask = datetime.fromtimestamp(t_ref - 1, tz=timezone.utc)
        print(f'\n== op {op_id} ({op.get("type")}) interface={I} '
              f't_ref={datetime.fromtimestamp(t_ref, tz=timezone.utc).isoformat()}')

        od = outcomes.get(op_id, {})
        proc = build_process(op_id, join_map, provenance, pack_shots)

        # 先做 mask 命中检查：未命中不读 EM、不建目录（省网络时间）
        blk = read_autoseg_block(vol_auto, I)
        roots_blk = roots_of_block(kg, blk, t_mask)
        before = [int(x) for x in (op.get('before_root_ids') or [])]
        seg1 = before[1] if len(before) > 1 else None   # cb2_align 映射
        seg2 = before[0] if before else None
        hit_px = 0
        for rid in (seg1, seg2):
            if rid is not None:
                n = int((roots_blk == np.uint64(rid)).sum())
                hit_px += n
                print(f'  mask root={rid}: px_in_window={n}')
        if hit_px == 0:
            row = {'op_id': op_id, 'state': od.get('state'),
                   'reason': '窗口内 before 段体素为 0（点/段空间关系待发布方确认）'}
            skipped.append(row)
            emit_row(skipped_path, row)
            print('  [skip] 窗口内 mask 未命中，不作为样本')
            continue

        d = out_dir / f'sample_{op_id}'
        d.mkdir(parents=True, exist_ok=True)
        views = {}
        for slot in ('em_xy', 'em_xz', 'em_yz'):
            views[slot] = em_view(vol_em, I, slot, res)
            print(f'  {slot} EM: mean={views[slot].mean():.0f}')
            Image.fromarray(views[slot].astype(np.uint8)).save(d / f'{slot}.png')

        masks = {}
        for name, rid in (('seg1', seg1), ('seg2', seg2)):
            if rid is None:
                masks[name] = None
                continue
            M = roots_blk == np.uint64(rid)
            masks[name] = {s: view_mask(M, s) for s in views}

        ov = make_overlay(views['em_xz'], masks['seg1']['em_xz'] if masks['seg1'] else None,
                          masks['seg2']['em_xz'] if masks['seg2'] else None)
        Image.fromarray(ov).save(d / 'overlay_xz.png')

        sample = {
            'schema': 'neuromancer.sample.v0.1',
            'source': {
                'datastack': 'minnie65_public',
                'op_id': op_id, 'op_type': op.get('type'),
                'user': op.get('user'), 'timestamp_utc': op.get('timestamp'),
            },
            'observation': {
                'render_params': {'S_nm': S, 'mip': 1},
                'images': {'em_xy': 'em_xy.png', 'em_xz': 'em_xz.png',
                           'em_yz': 'em_yz.png', 'overlay_xz': 'overlay_xz.png'},
                'interface_point_nm': I,
                'viewpoint': (proc.get('viewpoint_before')
                              if proc.get('viewpoint_before') else None),
                'note': ('视角/关键帧来自 recorder 会话包（join 命中）'
                         if proc.get('session_id')
                         else '本次导出无匹配会话；视角列为 null'),
            },
            'action': {
                'type': op.get('type'),
                'before_root_ids': op.get('before_root_ids'),
                'after_root_ids': op.get('after_root_ids'),
                'source_coords': op.get('source_coords'),
                'sink_coords': op.get('sink_coords'),
            },
            'segments': {'segment1_id': seg1, 'segment2_id': seg2,
                         'semantics': 'segment1/2 = before reversed（待发布方确认）'},
            'outcome': {
                'state': od.get('state'),
                'survival_seconds': od.get('survival_seconds'),
                'next_change': od.get('next_change'),
                'checked_at': od.get('checked_at'),
            },
            'process': proc,
            'labels': {
                'action': op.get('type'),
                'outcome_state': od.get('state'),
                'usage_hint': {
                    'later_split': '轨迹 SFT 的 action 侧 + 结局条件（被拆分，不宜直接作正例）',
                    'later_merged': '轨迹 SFT 的 action 侧 + 结局条件（被合并改写）',
                    'latest': '存活正例候选（仍需稳定性窗口过滤）',
                }.get(od.get('state'), '轨迹 SFT 的 action 侧'),
            },
            'provenance': provenance,
        }
        (d / 'sample.json').write_text(
            json.dumps(sample, ensure_ascii=False, indent=1), encoding='utf-8')
        irow = {'op_id': op_id, 'state': od.get('state'),
                'mask_hit_px': hit_px}
        try:
            irow['dir'] = str(d.relative_to(ROOT))
        except ValueError:
            irow['dir'] = str(d)
        index.append(irow)
        emit_row(index_path, irow)

        trow = [np.full((224, 224), 255, np.uint8) for _ in range(4)]
        for j, slot in enumerate(('em_xy', 'em_xz', 'em_yz')):
            trow[j] = views[slot].astype(np.uint8)
        trow[3] = ov
        tiles.append((op_id, trow))

    if tiles:
        pad, title_h = 8, 18
        w = 4 * 224 + 5 * pad
        h = len(tiles) * (224 + title_h) + (len(tiles) + 1) * pad
        sheet = Image.new('RGB', (w, h), (245, 246, 248))
        dr = ImageDraw.Draw(sheet)
        for ti, (op_id, row) in enumerate(tiles):
            y = pad + ti * (224 + title_h + pad)
            label = f'op {op_id}   [em_xy | em_xz | em_yz | overlay_xz]'
            dr.text((pad + 2, y), label, fill=(40, 44, 52))
            for j, img in enumerate(row):
                x = pad + j * (224 + pad)
                sheet.paste(Image.fromarray(img), (x, y + title_h))
        sheet.save(out_dir / 'contact_sheet.png')
        print(f'\ncontact sheet: {out_dir / "contact_sheet.png"}')

    state_counts = {}
    for r in index:
        state_counts[r['state']] = state_counts.get(r['state'], 0) + 1
    if skipped:
        print('skipped:', [(r['op_id'], r['reason'][:14]) for r in skipped])
    print('index:', index_path, f'({len(index)} 条样本；增量落盘)')
    print('样本统计（按结局状态）:', state_counts)
    print('RESULT: OK' if index else 'RESULT: NO SAMPLES')


if __name__ == '__main__':
    main()
