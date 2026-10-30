"""cave_session_pack：会话包 JSON → 目录契约（append-only、可直接进研究库）。

为什么需要这一步：recorder 在浏览器里只能导出一个 JSON 文件，而研究/交接要求
的是可增量、可核对、截图与事件分开的目录结构（主项目与 v0.4 清单 F27/FS01–FS09）：

    sessions/<session_id>/
      manifest.json            # 会话元信息、健康状态、能力声明、五段 provenance、完整性摘要
      client/events.jsonl      # 原始事件流（一行一事件；append-only）
      client/notes.jsonl       # 显式备注（人工表达，独立于事件流）
      client/shots/*.png       # 关键帧（截图=所见）
      client/shots/shots.jsonl # 关键帧索引（shot_id/moment/capture_status/state_id/request_id）
      derived/                 # join / 回放索引 / 派生语义（可重算，不覆盖原始）

五段 provenance（本次 demo 口径，用户 2026-10-26 确认）：
    service_facts          服务端权威操作事实（public CAVE op log）
    visual_assets          版本化视觉资产（EM / 分割 mask 的钉住版本重渲染）
    client_process         客户端过程（本会话真实录制）
    submit_execution       编辑是否被真实服务端执行（公开栈只读 → simulated）
    join                   两端关联方式（simulated-ack / exact-anchor / window …）
每段取值 real / simulated / unknown，并必须带一句 reason；不填即 unknown，
不允许留空冒充 real。

用法：
    python scripts/cave_session_pack.py <session.json>
    python scripts/cave_session_pack.py <session.json> --out results/sessions \
        --set submit_execution=simulated \
        --reason submit_execution="公开 minnie65_public 只读，回执由本地网关给出"
    python scripts/cave_session_pack.py <session_dir> --print   # 读目录包摘要
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def relpath(p, base=None):
    """给人看的路径：在 base（缺省仓库根）下给相对，否则给绝对。

    代码内部照旧用绝对路径（不依赖 cwd），只在**打印/回传**时相对化——
    别人 clone 到自己的目录后，输出里不该出现原作者机器上的路径。
    """
    if p is None:
        return ''
    p = Path(p)
    root = Path(base) if base else ROOT
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(p)
# 和 cave_manual_server / 工作台一致（曾经写成 results/sessions，落盘落到别处）
DEFAULT_OUT = ROOT / 'results' / 'manual_sessions'
PACK_SCHEMA = 'neuromancer.session_pack.v1'
LEGS = ('service_facts', 'visual_assets', 'client_process',
        'submit_execution', 'join')
LEG_VALUES = ('real', 'simulated', 'unknown')


# ---------------------------------------------------------------- 读写
def empty_pack():
    """空包：一个会话都没有时的占位。

    让工作台能"先起界面、再采数据"——首页负责采集，回放/看包/派生显示占位。
    结构对齐 load_session() 的返回，下游按同一套字段读。
    """
    return {
        'schema': PACK_SCHEMA, 'source_schema': None,
        'collector_version': None, 'session_id': None,
        'page': {}, 'viewer': {}, 'config': {}, 'counts': {},
        'events': [], 'notes': [], 'shots': [], 'errors': [],
        'time': {}, 'collector': {}, 'health': {}, 'provenance': {},
        'pack_dir': None, 'manifest': {},
    }


def _normalize_focus_events(events):
    """兼容旧 recorder 的 viewer.focus 嵌套结构。

    旧实现 emit 的是 `position: {position, page, picked...}`；下游按
    `position` 列表 + `page`/`picked` 顶层字段读，会把 page/picked 全丢掉。
    新包写顶层；读旧包时在这里展开，不修改 events.jsonl 文件本身。
    """
    for e in events:
        if not isinstance(e, dict):
            continue
        if e.get('type') not in ('viewer.focus', 'mouse_sample'):
            continue
        p = e.get('position')
        if not isinstance(p, dict):
            continue
        if not any(k in p for k in ('position', 'page', 'picked', 'picked_miss')):
            continue
        for k in ('position', 'page', 'picked', 'picked_miss', 'picked_layer'):
            if k in p:
                e[k] = p[k]
        if not isinstance(e.get('position'), (list, tuple)):
            e['position'] = []
    return events


def load_session(path):
    """读会话包：支持 recorder 导出的 JSON，也支持已落盘的目录包。"""
    p = Path(path)
    if p.is_dir():
        manifest = json.loads((p / 'manifest.json').read_text('utf-8'))
        events = []
        ev_path = p / 'client' / 'events.jsonl'
        if ev_path.exists():
            for line in ev_path.read_text('utf-8').splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        notes = []
        nt_path = p / 'client' / 'notes.jsonl'
        if nt_path.exists():
            for line in nt_path.read_text('utf-8').splitlines():
                line = line.strip()
                if line:
                    notes.append(json.loads(line))
        shots = []
        sh_path = p / 'client' / 'shots' / 'shots.jsonl'
        if sh_path.exists():
            for line in sh_path.read_text('utf-8').splitlines():
                line = line.strip()
                if line:
                    shots.append(json.loads(line))
        _normalize_focus_events(events)
        pack = {
            'schema': manifest.get('pack_schema', PACK_SCHEMA),
            'source_schema': manifest.get('source_schema'),
            'collector_version': manifest.get('collector_version'),
            'session_id': manifest.get('session_id'),
            'page': manifest.get('page') or {},
            'viewer': manifest.get('viewer') or {},
            'time': manifest.get('time') or {},
            'collector': manifest.get('collector') or {},
            'counts': manifest.get('counts') or {},
            'events': events,
            'notes': notes,
            'shots': shots,
            'errors': [],
            'manifest': manifest,
            'pack_dir': str(p),
            'initial_checkpoint_state': manifest.get('initial_checkpoint_state'),
        }
        return pack
    pack = json.loads(p.read_text('utf-8'))
    if not isinstance(pack, dict) or 'events' not in pack:
        raise SystemExit(f'不是有效的会话包: {path}')
    _normalize_focus_events(pack.get('events') or [])
    return pack


def _unique_dir(base: Path, name: str, session_id=None) -> Path:
    """同名目录已存在时怎么办。

    - **同一个会话**再存一次（`session_id` 相同）→ 直接覆盖那一个目录。
      否则一次会话会变成 `sess_x` + `sess_x__20261029_184032` 两份，
      后一份永远比前一份多几条事件，看着像"莫名其妙多出来一个"。
    - 别的会话占了同名 → 追加时间戳，不覆盖。
    """
    target = base / name
    if not target.exists():
        return target
    mf = target / 'manifest.json'
    if session_id and mf.exists():
        try:
            old = json.loads(mf.read_text('utf-8')).get('session_id')
        except Exception:
            old = None
        if old and old == session_id:
            return target          # 同一个会话：覆盖
    ts = dt.datetime.now().strftime('%Y%m%d_%H%M%S')
    return base / f'{name}__{ts}'


def _sha256_text(lines):
    h = hashlib.sha256()
    for ln in lines:
        h.update(ln.encode('utf-8'))
        h.update(b'\n')
    return h.hexdigest()


def _safe_name(s, fallback='shot'):
    s = re.sub(r'[^0-9A-Za-z_.-]+', '_', str(s or ''))
    return s or fallback


def _safe_dirname(s, fallback='session'):
    """会话目录名：保留中文等可见字符，只挡路径分隔符、控制字符与空白。

    与 `_safe_name` 分开：后者用于 shot 文件名，那里保持纯 ASCII 更省事。
    """
    s = re.sub(r'[/\\:*?"<>|#\x00-\x1f]+', '_', str(s or ''))
    s = re.sub(r'\s+', '_', s).strip('._')
    return s[:120] or fallback


# ---------------------------------------------------------------- 事件名兼容
# recorder v0.7 起事件名对齐《CAVE_前端_Neuroglancer_需求清单 v0.4》RF01–RF15
# （点号命名）。旧包用下划线名，旧名同时保留在事件的 legacy_type 里。
# 下游统一用 ctype() 读，**不要直接比较 e['type']**——否则新旧包要写两套。
EVENT_ALIASES = {
    'session_start': 'session.start',
    'session_stop': 'session.end',
    'viewer_attached': 'viewer.attach',
    'state_sample': 'viewer.state-change',
    'state_checkpoint': 'viewer.checkpoint',
    'selection_sample': 'selection.change',
    'pointer': 'input.pointer',
    'wheel': 'input.wheel',
    'key': 'input.key',
    'mouse_sample': 'viewer.focus',
    'multicut_change': 'draft.update',
    'gesture': 'viewport.gesture',
    'keyframe': 'keyframe.capture',
    'note': 'evidence.bookmark',
    'collector_status': 'collector.status',
    'heartbeat': 'collector.heartbeat',
    'api_request': 'api.request',
    'api_response': 'api.ack',
    'api_operation_id': 'edit.operation',
    'api_capability': 'api.capability',
    'ui_state': 'ui.state',
    'private_journal': 'private.journal',
    'ext_snapshot': 'ext.snapshot',
    'api_error': 'api.error',
}
# 写请求的细分名（v0.7 起由 recorder 直接写，旧包没有这两条）
EDIT_TYPES = ('edit.request', 'edit.ack')
# 写请求的完整集合（不分新旧）
WRITE_REQUEST_TYPES = ('edit.request', 'api.request')


def ctype(ev):
    """规范化事件类型：新名原样返回，旧名映射到新名。"""
    t = ev.get('type') if isinstance(ev, dict) else None
    return EVENT_ALIASES.get(t, t)


# ---------------------------------------------------------------- 健康
# 注意：心跳的用途是**发现节流与断采**，不是“估算思考时长”。
# 需求清单 F26/RF14 要求这个字段；但清单自己也写了“长时间无操作不能被视为
# 在思考”。所以这里只报“有多少条 + 间隔中位数”（变长 = 页面被节流），
# 不产生任何耗时类派生指标。采集器是否活着另看 seq 连续性与 collector.status。
def health_summary(events, gaps_hint=None):
    hb = [e for e in events if ctype(e) == 'collector.heartbeat']
    status = [e for e in events if ctype(e) == 'collector.status']
    seqs = [e.get('seq') for e in events if isinstance(e.get('seq'), int)]
    t_walls = [e.get('t_wall') for e in events
               if isinstance(e.get('t_wall'), (int, float))]
    end = next((e for e in reversed(events) if ctype(e) == 'session.end'), None) or {}
    counts = end.get('counts') or {}
    # session.end 里的 gaps 是 stop 那一刻的快照（可能已闭合），比 dump 时的 hint 更完整
    gaps_all = list(end.get('gaps') or gaps_hint or [])
    gaps_ms, gaps_open = 0, 0
    for g in gaps_all:
        t0, t1 = g.get('t_wall'), g.get('t_end')
        if t1 is None:
            gaps_open += 1
        elif isinstance(t0, (int, float)) and isinstance(t1, (int, float)):
            gaps_ms += max(0, t1 - t0)
    out = {
        'n_events': len(events),
        'seq_first': seqs[0] if seqs else None,
        'seq_last': seqs[-1] if seqs else None,
        'seq_gaps': [],
        'n_heartbeats': len(hb),
        'heartbeat_median_ms': None,
        'gaps': gaps_all,
        'gaps_total_ms': gaps_ms,
        'gaps_open': gaps_open,
        'dropped': int(counts.get('dropped') or 0),
        'checkpoint_dropped': int(counts.get('checkpoint_dropped') or 0),
        'overflow': end.get('overflow') or {},
        'status_changes': len(status),
        'span_ms': (max(t_walls) - min(t_walls)) if t_walls else None,
    }
    prev = None
    for s in seqs:
        if prev is not None and s - prev > 1:
            out['seq_gaps'].append([prev + 1, s - 1])
        prev = s
    hb_t = [e.get('t_wall') for e in hb
            if isinstance(e.get('t_wall'), (int, float))]
    if len(hb_t) >= 2:
        deltas = sorted(b - a for a, b in zip(hb_t, hb_t[1:]))
        out['heartbeat_median_ms'] = deltas[len(deltas) // 2]
    return out


# ---------------------------------------------------------------- 写包
def _backup_path(final):
    """给旧包找不冲突的备份名；同秒多次重存也不会撞名。"""
    stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base = final.with_name('.old_%s_%s' % (final.name, stamp))
    p = base
    n = 1
    while p.exists():
        p = base.with_name(base.name + '_%d' % n)
        n += 1
    return p


def pack_session(pack, out_dir=DEFAULT_OUT, legs=None, reasons=None,
                 extra_manifest=None, extra_shots=None, with_html=True):
    """把会话包原子地落成目录。

    先在 `out_dir` 下的隐藏临时目录写完并校验，再整体切换到正式目录。
    同一 session 重存时，旧包先改名为 `.old_*` 备份；临时目录写失败时
    正式目录原样保留，读者不会看到半包。提交阶段的 rename 窗口很短；
    若崩溃，旧包仍在 `.old_*`，临时目录可按前缀清理。

    extra_shots：宿主侧截图（例如 Playwright page.screenshot）。
    它**不是** recorder 的 canvas readback，必须用 source 字段标明，
    不得冒充“页面内自采”。每项：{png_bytes, moment, request_id,
    state_id, source, note}。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sid = pack.get('session_id') or 'session'
    final = _unique_dir(out_dir, _safe_dirname(sid, 'session'), session_id=sid)
    backup = _backup_path(final) if final.exists() else None
    stage = Path(tempfile.mkdtemp(
        prefix='.tmp_%s_' % final.name, dir=str(out_dir)))
    # mkdtemp 默认 0700；包目录一直按普通目录共享，保持原行为
    stage.chmod(0o755)
    try:
        d, manifest = _pack_session_into(
            stage, backup, sid, pack, legs, reasons,
            extra_manifest, extra_shots, with_html)
        # 提交前再确认一次：如果期间有别的进程创建了正式目录，
        # 也先备份，避免把它当“不存在”直接 rename。
        if final.exists() and backup is None:
            backup = _backup_path(final)
            manifest['previous_pack'] = str(backup)
            (stage / 'manifest.json').write_text(
                json.dumps(manifest, ensure_ascii=False, indent=1),
                encoding='utf-8')
        moved_old = False
        try:
            if final.exists():
                final.rename(backup)
                moved_old = True
            stage.rename(final)
        except BaseException:
            if moved_old and not final.exists() and backup.exists():
                backup.rename(final)
            raise
        return final, manifest
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _pack_session_into(d, backup, sid, pack, legs, reasons,
                       extra_manifest, extra_shots, with_html):
    """把完整包写进 d（临时目录）；不负责提交/备份。"""
    (d / 'client' / 'shots').mkdir(parents=True, exist_ok=True)
    (d / 'derived').mkdir(parents=True, exist_ok=True)

    events = pack.get('events') or []
    notes = pack.get('notes') or []
    shots = pack.get('shots') or []

    # 1) 事件流 + 备注（一行一条，append-only 语义；重跑进新目录）
    ev_lines = [json.dumps(e, ensure_ascii=False, sort_keys=True)
                for e in events]
    (d / 'client' / 'events.jsonl').write_text(
        '\n'.join(ev_lines) + ('\n' if ev_lines else ''), encoding='utf-8')
    nt_lines = [json.dumps(n, ensure_ascii=False, sort_keys=True)
                for n in notes]
    (d / 'client' / 'notes.jsonl').write_text(
        '\n'.join(nt_lines) + ('\n' if nt_lines else ''), encoding='utf-8')

    # 2) 截图：dataURL → PNG 落盘；索引独立成 jsonl
    shot_rows = []
    png_sha256 = {}
    n_png = 0
    for s in shots:
        # 白名单落盘。L1 新字段（state 绑定 / 内容指纹 / 等待结果）必须在这里放行，
        # 否则采集器写了也会在落包时被静默丢掉——那正是“字段看起来有、其实没有”。
        row = {k: s.get(k) for k in
               ('shot_id', 't_wall', 'w', 'h', 'ok', 'note',
                'capture_status', 'readback_via', 'moment', 'request_id',
                'state_id', 'state_sha', 'state_in_stream', 'state_age_ms',
                'state_partial', 'state_truncated', 'paint_settled', 'paint_stable',
                'paint_changed', 'paint_waited_ms', 'paint_timed_out',
                'content', 'state')}
        row['source'] = s.get('source') or 'recorder_canvas_readback'
        data_url = s.get('dataURL')
        rel = None
        if data_url and ',' in data_url:
            try:
                import base64
                raw = base64.b64decode(data_url.split(',', 1)[1])
                name = _safe_name(row.get('shot_id'), 'shot') + '.png'
                (d / 'client' / 'shots' / name).write_bytes(raw)
                rel = f'shots/{name}'
                row['png_sha256'] = hashlib.sha256(raw).hexdigest()
                row['png_bytes'] = len(raw)
                png_sha256[rel] = row['png_sha256']
                n_png += 1
            except Exception as err:      # 图像坏了不能当成功
                row['note'] = (row.get('note') or '') + \
                    f' | decode_failed:{type(err).__name__}'
                row['capture_status'] = 'decode_failed'
        row['image_ref'] = rel
        shot_rows.append(row)

    # 宿主侧截图（由调用方渲染环境提供；source 必须写明）
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    for i, s in enumerate(extra_shots or [], 1):
        raw = s.get('png_bytes')
        if not raw:
            continue
        sid_ = s.get('shot_id') or f'host_shot_{i}'
        name = _safe_name(sid_) + '.png'
        (d / 'client' / 'shots' / name).write_bytes(raw)
        n_png += 1
        rel2 = f'shots/{name}'
        png_sha256[rel2] = hashlib.sha256(raw).hexdigest()
        shot_rows.append({
            'shot_id': sid_, 't_wall': s.get('t_wall') or now_ms,
            'w': s.get('w'), 'h': s.get('h'), 'ok': True,
            'note': s.get('note') or '宿主侧截图（非页面内 canvas readback）',
            'capture_status': 'captured',
            'moment': s.get('moment'), 'request_id': s.get('request_id'),
            'state_id': s.get('state_id'),
            'source': s.get('source') or 'host_page_screenshot',
            'image_ref': rel2,
            'png_sha256': png_sha256[rel2],
            'png_bytes': len(raw),
        })
    sh_lines = [json.dumps(r, ensure_ascii=False, sort_keys=True)
                for r in shot_rows]
    (d / 'client' / 'shots' / 'shots.jsonl').write_text(
        '\n'.join(sh_lines) + ('\n' if sh_lines else ''), encoding='utf-8')

    # 3) provenance（五段；未声明即 unknown，不允许留空）
    legs = dict(legs or {})
    reasons = dict(reasons or {})
    prov = {}
    for leg in LEGS:
        val = str(legs.get(leg, 'unknown')).lower()
        if val not in LEG_VALUES:
            raise SystemExit(f'provenance {leg}={val} 非法（{LEG_VALUES}）')
        prov[leg] = {'status': val, 'reason': reasons.get(leg) or None}
    collector = dict(pack.get('collector') or {})
    t = pack.get('time') or {}
    health = health_summary(events, collector.get('gaps'))

    manifest = {
        'pack_schema': PACK_SCHEMA,
        'source_schema': pack.get('schema'),
        'collector_version': pack.get('collector_version')
            or collector.get('version'),
        'session_id': sid,
        'page': pack.get('page') or {},
        'viewer': pack.get('viewer') or {},
        'time': t,
        # D10 日志粒度：采样配置必须随包走，否则下游只能靠默认值猜
        # （猜错就会把 50ms 的数据当成全量轨迹做运动学分析）。
        'config': pack.get('config') or {},
        'counts': pack.get('counts') or {},
        'collector': collector,
        'health': health,
        'provenance': prov,
        'integrity': {
            'events_sha256': _sha256_text(ev_lines),
            'notes_sha256': _sha256_text(nt_lines),
            'shots_sha256': _sha256_text(sh_lines),
            'n_events': len(events), 'n_notes': len(notes),
            'n_shots': len(shot_rows), 'n_png': n_png,
            # PNG 本体单独哈希：只 hash 索引抓不到“图片被替换/截断”。
            'png_sha256': png_sha256,
        },
        'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if backup is not None:
        manifest['previous_pack'] = str(backup)
    if pack.get('initial_checkpoint_state') is not None:
        manifest['initial_checkpoint_state'] = pack['initial_checkpoint_state']
    if extra_manifest:
        manifest.update(extra_manifest)
    (d / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')

    # 双击入口：包里生成一份自包含回放页（无 JS 依赖，双击即开）。
    # 失败不影响保存（fail-open）——包本身已经完整落盘了。
    # 延迟 import：cave_session_replay 顶部又 import 本模块，放模块级会循环。
    if with_html:
        try:
            import cave_session_replay as _csr
            _csr.replay_one(d, out_path=d / '打开.html', quiet=True)
        except Exception as err:      # noqa: BLE001
            print('[warn] 打开.html 未生成：%s: %s'
                  % (type(err).__name__, str(err)[:120]), flush=True)
    return d, manifest



# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description='会话包 JSON → 目录契约')
    ap.add_argument('session', help='recorder 导出的会话 JSON，或已有目录包')
    ap.add_argument('--out', default=str(DEFAULT_OUT), help='输出根目录')
    ap.add_argument('--set', action='append', default=[], metavar='LEG=VALUE',
                    help='设置某段 provenance（real/simulated/unknown）')
    ap.add_argument('--reason', action='append', default=[],
                    metavar='LEG=TEXT', help='对应 provenance 的理由')
    ap.add_argument('--print', action='store_true', help='只打印摘要，不写包')
    args = ap.parse_args(argv)

    pack = load_session(args.session)
    if args.print:
        m = pack.get('manifest') or {}
        print(json.dumps({
            'session_id': pack.get('session_id'),
            'pack_dir': pack.get('pack_dir'),
            'n_events': len(pack.get('events') or []),
            'counts': pack.get('counts'),
            'health': m.get('health'),
            'provenance': m.get('provenance'),
        }, ensure_ascii=False, indent=1))
        return 0

    legs, reasons = {}, {}
    for kv in args.set:
        if '=' not in kv:
            raise SystemExit(f'--set 需要 LEG=VALUE：{kv}')
        k, v = kv.split('=', 1)
        legs[k] = v
    for kv in args.reason:
        if '=' not in kv:
            raise SystemExit(f'--reason 需要 LEG=TEXT：{kv}')
        k, v = kv.split('=', 1)
        reasons[k] = v

    d, manifest = pack_session(pack, args.out, legs, reasons)
    h = manifest['health']
    print(f"会话包目录: {relpath(d)}")
    print(f"  事件 {h['n_events']} 条 / seq {h['seq_first']}–{h['seq_last']}"
          f" / 状态变化 {h['status_changes']}"
          f" / span {h['span_ms']} ms")
    print(f"  缺口: {h['gaps'] or '无'}  seq 断档: {h['seq_gaps'] or '无'}")
    print(f"  截图 {manifest['integrity']['n_png']} 张落盘")
    print('  provenance: ' + ', '.join(
        f'{k}={v["status"]}' for k, v in manifest['provenance'].items()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
