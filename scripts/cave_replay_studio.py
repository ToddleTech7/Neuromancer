#!/usr/bin/env python3
"""cave_replay_studio：把会话包做成**可拖的界面级回放**（真 Neuroglancer 页面）。

和另外两个回放的区别（三个都要，不互相替代）：

    replay_manual_session.py   逐条喂输入（pointer/wheel/key），验"动作能不能复现"
    cave_replay_viewer.py      逐点恢复 state + 截图，验"版本能不能重建"
    cave_replay_studio.py      ← 本文件：真 NG 界面 + 时间轴，拖到哪看到哪

为什么需要它：逐条重放只能从头看到尾，想回看第 60 秒就得再等 60 秒；
这里直接按 **state 快照** 跳，拖动是秒级响应，并且右侧实时显示该时刻的
数据层（position / 缩放 / 选中集 / 布局），底部标出关键帧与备注。

它怎么做到的（不依赖任何新前端）：

    ┌─────────── 浏览器（本服务）────────────┐
    │  外壳页：iframe(NG) + 时间轴 + 数据面板  │
    │        │ POST /api/seek {t}             │
    └────────┼────────────────────────────────┘
             ▼
       Python：找 t 之前最近的一份完整 state
             │ 只发**变化了**的字段（pos/cross/proj/orient/sel）
             ▼
       cave_local_demo_server.py（stdin 指令）→ 真 NG 页面重绘

如实边界（必须保留）：
- 拖动是"跳快照"，不是"连续重放输入"：两份快照之间（平均 ~2.4 s）看不到过程；
  要看过程用 `--precise`（按输入重放到目标时刻，慢，但连续）。
- 像素清晰度（瓦片 LOD）可能与录制端不同：界面级回放不承诺逐像素一致，
  逐像素比对仍交给 `replay_manual_session.py`。
- 只读：本服务不改包、不写服务端；NG 侧只发 state/pos/sel 这类读指令。

用法：
    python3 scripts/cave_replay_studio.py                  # 最新包
    python3 scripts/cave_replay_studio.py results/manual_sessions/<id>
    python3 scripts/cave_replay_studio.py <包> --port 8911 --no-open
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cave_session_pack as csp  # noqa: E402
import cave_manual_server as cms  # noqa: E402  # 复用采集端的失败补救文案
import py_select as ps  # noqa: E402  # 子进程解释器选择（采集窗口能不能开）


_PY_PICK = {}


def _utf8_env():
    """给“我们把它的输出读进管道”的子进程：两端得是同一个编码。

    只改这个子进程的环境，不改整个工作台进程的：直接往终端写的中文要按本机
    控制台编码走，否则中文 PowerShell 会拿 cp936 去解 UTF-8，满屏乱码。
    """
    return {**os.environ, 'PYTHONUTF8': '1'}


def _pick_child():
    """挑采集/回放子进程要用的解释器，只挑一次。

    `nx.py` 选过解释器会把它放进 NM_PYTHON，直接沿用；直接跑本脚本时没这个
    变量，就自己按“能不能起采集窗口”找一个（当前解释器 → 项目 `.venv` → PATH
    里的 python），全都不可用时退回当前解释器——那时首页会显示缺什么、跑哪条命令。
    """
    global _PY_PICK
    if not _PY_PICK:
        v = os.environ.get('NM_PYTHON')
        _PY_PICK = ({'exe': v, 'why': 'env:NM_PYTHON', 'fixes': []}
                    if v and Path(v).exists()
                    else ps.pick(current=sys.executable))
    return _PY_PICK


def _child_py():
    """采集/回放子进程用哪个解释器。"""
    return _pick_child()['exe'] or sys.executable


# 启动时在后台问一次“子进程要用的解释器能不能开采集窗口”，结果给首页提示用
_PY_CHECK = {'ok': None, 'why': '', 'exe': sys.executable, 'fixes': []}


def _check_py():
    exe = _child_py()
    ok, why = ps.probe(exe)
    why = str(why)
    _PY_CHECK.update({'ok': ok, 'why': why[:200], 'exe': exe,
                      'fixes': [] if ok else ps.fix_commands(exe, why)})
    if not ok:
        print('[studio] 提醒：%s 开不了采集窗口（%s）' % (exe, why[:120]), flush=True)
        for c in _PY_CHECK['fixes']:
            print('[studio]       ' + c, flush=True)
        print('[studio]       或者用 `python3 scripts/nx.py` 起（它会自己挑能用的解释器）',
              flush=True)


STATE_TYPES = ('viewer.state-change', 'viewer.checkpoint')

# 数据源目录：工作台里可切的不只是包，还有“看的是哪份数据”。
# 默认 `pack` = 跟随包里录到的 source（最保真）；其余都是显式指定，
# 本地/远程都可以（precomputed:// 或 graphene://）。
DATASETS = [
    {'id': 'pack', 'label': '跟随回放包（推荐）',
     'note': '用包里 state 录到的 source'},
    {'id': 'minnie65-bossdb', 'label': 'minnie65 · BossDB',
     'em': ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
            'iarpa_microns/minnie/minnie65/em'),
     'seg': ('precomputed://https://bossdb-open-data.s3.amazonaws.com/'
             'iarpa_microns/minnie/minnie65/seg')},
    {'id': 'minnie65-gcs', 'label': 'minnie65 · GCS 镜像',
     'em': ('precomputed://https://storage.googleapis.com/'
            'iarpa_microns/minnie/minnie65/em'),
     'seg': ('precomputed://https://storage.googleapis.com/'
             'iarpa_microns/minnie/minnie65/seg')},
    {'id': 'custom', 'label': '自定义 URL（我们的数据）',
     'note': '两个 URL 都要填：EM 与分割'},
]
# “我们自己的数据”：没写死在代码里，用环境变量注入（不冒充已接通 CAVE）。
if os.environ.get('NEURO_EM_URL') and os.environ.get('NEURO_SEG_URL'):
    DATASETS.append({'id': 'ours', 'label': '我们的数据（环境变量）',
                     'em': os.environ['NEURO_EM_URL'],
                     'seg': os.environ['NEURO_SEG_URL']})
# 时间轴上要单独标出来的事件（移动事件太多，全画反而看不清）
EVENT_TYPES = ('input.key', 'selection.change', 'viewer.checkpoint',
               'evidence.bookmark', 'session.start', 'session.end')


# ---------------------------------------------------------------- 包 → 时间轴

def rel(t, t0):
    return round((t - t0) / 1000.0, 2)


def segs_of(state):
    """{层名: [id...]}：只取 segmentation 层，去掉 NG 的 `!` 前缀。"""
    out = {}
    for lay in (state.get('layers') or []):
        name = lay.get('name')
        if not name or 'segmentation' not in str(lay.get('type') or '').lower():
            continue
        out[name] = [re.sub(r'^!', '', str(x)) for x in (lay.get('segments') or [])]
    return out


def snapshots_of(pack):
    """按时间排好的完整 state 快照（同一 state_sha 只留第一次）。"""
    out, seen = [], set()
    for e in pack.get('events') or []:
        if csp.ctype(e) not in STATE_TYPES:
            continue
        st = e.get('state') or {}
        if not st.get('position'):
            continue
        sha = e.get('state_sha')
        if sha and sha in seen:
            continue
        if sha:
            seen.add(sha)
        out.append({'t_wall': e.get('t_wall') or 0, 'seq': e.get('seq'),
                    'state': st, 'source': csp.ctype(e)})
    out.sort(key=lambda x: x['t_wall'])
    return out


def shots_of(pack, packdir):
    """关键帧索引：只留真的能打开的图。"""
    out = []
    for s in (pack.get('shots') or []):
        sid = s.get('shot_id')
        if not sid:
            continue
        p = Path(packdir) / 'client' / 'shots' / (sid + '.png')
        if not p.exists():
            continue
        out.append({'id': sid, 't_wall': s.get('t_wall') or 0,
                    'status': s.get('capture_status'),
                    'url': '/shots/%s.png' % sid})
    out.sort(key=lambda x: x['t_wall'])
    return out


def build_timeline(pack, packdir):
    snaps = snapshots_of(pack)
    shots = shots_of(pack, packdir)
    notes = [{'t_wall': n.get('t_wall') or 0, 'text': n.get('text') or ''}
             for n in (pack.get('notes') or [])]
    events = []
    for e in pack.get('events') or []:
        t = csp.ctype(e)
        if t not in EVENT_TYPES:
            continue
        if t == 'input.key' and e.get('event') not in ('keydown', 'down'):
            continue
        if t == 'selection.change':
            continue  # 悬停高亮，单独画会把时间轴糊掉
        events.append({'t_wall': e.get('t_wall') or 0, 'seq': e.get('seq'),
                       'type': t,
                       'label': e.get('key') or e.get('code') or t})
    # 鼠标轨迹：工作台的 NG 在跨源 iframe 里，注不进去，只能在外壳页上
    # 叠一个点；坐标是录制页面的 CSS 像素，显示时按 iframe/录制视口缩放并夹紧。
    mouse = []
    for e in pack.get('events') or []:
        t = csp.ctype(e)
        if t == 'input.pointer':
            if e.get('phase') not in ('pointermove', 'mousemove',
                                      'pointerdown', 'mousedown'):
                continue
            if isinstance(e.get('x'), (int, float)) and isinstance(e.get('y'), (int, float)):
                mouse.append({'t': e.get('t_wall') or 0,
                              'x': float(e['x']), 'y': float(e['y'])})
        elif t == 'viewer.focus':
            pg = e.get('page') or []
            if (len(pg) >= 2 and isinstance(pg[0], (int, float))
                    and isinstance(pg[1], (int, float))):
                mouse.append({'t': e.get('t_wall') or 0,
                              'x': float(pg[0]), 'y': float(pg[1])})
    mouse.sort(key=lambda z: z['t'])

    t0 = min([x['t_wall'] for x in snaps] or [0])
    # 时间轴从会话开始算，而不是从第一份快照算：和“会话第 N 秒”/--seek-t 同一口径。
    t_start = (pack.get('time') or {}).get('start_wall_ms')
    if not isinstance(t_start, (int, float)):
        evs = pack.get('events') or []
        t_start = evs[0].get('t_wall') if evs else t0
    if isinstance(t_start, (int, float)):
        t0 = t_start
    # 鼠标点位是录制页面的 CSS 像素（通常 1280×740）；工作台 iframe 的显示尺寸
    # 可能更小，必须按录制视口缩放，否则红点会跑到 iframe 外面。
    viewport = None
    for e in pack.get('events') or []:
        if csp.ctype(e) != 'session.start':
            continue
        css = (e.get('viewport') or {}).get('css_px') or []
        if (len(css) >= 2 and isinstance(css[0], (int, float))
                and isinstance(css[1], (int, float))):
            viewport = [float(css[0]), float(css[1])]
            break
    if viewport is None:
        for e in pack.get('events') or []:
            if csp.ctype(e) not in STATE_TYPES:
                continue
            css = ((e.get('viewpoint') or {}).get('viewport') or {}).get('css_px') or []
            if (len(css) >= 2 and isinstance(css[0], (int, float))
                    and isinstance(css[1], (int, float))):
                viewport = [float(css[0]), float(css[1])]
                break
    return {
        'session': Path(packdir).name if packdir else '(还没有会话)',
        'viewport': viewport,
        'pack': str(packdir) if packdir else '',
        't0': t0,
        't1': max([x['t_wall'] for x in snaps] or [t0]),
        'snapshots': [{'t': rel(s['t_wall'], t0), 'seq': s['seq'],
                       'segments': sum(len(v) for v in segs_of(s['state']).values()),
                       'position': s['state'].get('position')} for s in snaps],
        'shots': [{'t': rel(s['t_wall'], t0), 'id': s['id'],
                   'status': s['status'], 'url': s['url']} for s in shots],
        'notes': [{'t': rel(n['t_wall'], t0), 'text': n['text']} for n in notes],
        'mouse': [{'t': round(rel(m['t'], t0), 2), 'x': m['x'], 'y': m['y']}
                  for m in mouse],
        'events': [{'t': rel(e['t_wall'], t0), 'seq': e['seq'], 'type': e['type'],
                    'label': e['label']} for e in events],
    }


# ---------------------------------------------------------------- NG 桥

class Bridge:
    """把"某一份 state"变成 demo server 的增量指令。"""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.started = False
        self.pin = False
        self.last = {}

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, '-u', str(HERE / 'cave_local_demo_server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', bufsize=1, env=_utf8_env())
        url = None
        for line in self.proc.stdout:
            line = line.strip()
            if line.startswith('VIEWER_URL='):
                url = line.split('=', 1)[1]
            elif line == 'READY':
                break
        if not url:
            raise SystemExit('demo server 没给出 VIEWER_URL')
        return url

    def send(self, cmd):
        with self.lock:
            if not self.proc or self.proc.poll() is not None:
                return
            try:
                self.proc.stdin.write(cmd + '\n')
                self.proc.stdin.flush()
            except Exception:
                pass

    def reset(self):
        """换包后要重发整份 state（新包的层/选中集可能完全不同）。"""
        self.started = False
        self.pin = False
        self.last = {}

    def pin_layers(self):
        """层由外部（img/seg 指令）建好：后续 seek 不再用包里的 state 重建层。

        为什么需要：切了自定义数据源后，包里录的 source 与用户刚选的不一样，
        再用 state 重建 = 把数据源改回去。
        """
        self.pin = True
        self.started = True
        self.last = {'layers': '__pinned__', 'pos': None, 'cross': None,
                     'proj': None, 'orient': None, 'segs': {}}

    def apply(self, state):
        """只发变化了的字段：位置类每次都要，层/选中集变化才重建。"""
        st = state or {}
        cmds = []
        layers_now = json.dumps([{k: l.get(k) for k in ('name', 'type', 'source')}
                                 for l in (st.get('layers') or [])],
                                sort_keys=True)
        if not self.pin and (not self.started or layers_now != self.last.get('layers')):
            cmds.append('state ' + json.dumps(st))
            self.started = True
            self.last = {
                'layers': layers_now,
                'pos': st.get('position'),
                'cross': st.get('crossSectionScale'),
                'proj': st.get('projectionScale'),
                'orient': st.get('projectionOrientation'),
                'segs': segs_of(st),
            }
            for c in cmds:
                self.send(c)
            return cmds

        pos = st.get('position')
        if pos and pos != self.last.get('pos'):
            cmds.append('pos %.4f %.4f %.4f' % tuple(pos))
            self.last['pos'] = pos
        cross = st.get('crossSectionScale')
        if isinstance(cross, (int, float)) and cross != self.last.get('cross'):
            cmds.append('cross %.6f' % float(cross))
            self.last['cross'] = cross
        proj = st.get('projectionScale')
        if isinstance(proj, (int, float)) and proj != self.last.get('proj'):
            cmds.append('proj %.6f' % float(proj))
            self.last['proj'] = proj
        orient = st.get('projectionOrientation')
        if isinstance(orient, list) and len(orient) == 4 and orient != self.last.get('orient'):
            cmds.append('orient ' + ' '.join('%.6f' % float(x) for x in orient))
            self.last['orient'] = orient
        segs = segs_of(st)
        if segs != self.last.get('segs'):
            for name, ids in segs.items():
                cmds.append('sel %s %s' % (name, ' '.join(ids)))
            self.last['segs'] = segs
        for c in cmds:
            self.send(c)
        return cmds


# ---------------------------------------------------------------- HTTP 外壳

class Studio:
    def __init__(self, pack, packdir, bridge):
        # packdir=None = 还没有任何会话（首页可以采集，回放/看包/派生显示占位）
        self.pack = pack
        self.packdir = Path(packdir) if packdir else None
        self.bridge = bridge
        self.tl = build_timeline(pack, self.packdir)
        self.snaps = snapshots_of(pack)
        t0 = self.tl['t0']
        self.t0 = t0
        for s in self.snaps:
            s['t'] = rel(s['t_wall'], t0)
        self.shots = shots_of(pack, self.packdir)
        self.notes = [{'t_wall': n.get('t_wall') or 0, 'text': n.get('text') or ''}
                      for n in (pack.get('notes') or [])]
        self.dataset = 'pack'

    def switch_pack(self, packdir):
        """采完一段后切到新包：重建时间轴，并让 NG 重发整份 state。"""
        packdir = from_root(packdir)
        pack = csp.load_session(packdir)
        self.pack, self.packdir = pack, packdir
        self.tl = build_timeline(pack, packdir)
        self.t0 = self.tl['t0']
        self.snaps = snapshots_of(pack)
        for s in self.snaps:
            s['t'] = rel(s['t_wall'], self.t0)
        self.shots = shots_of(pack, packdir)
        self.notes = [{'t_wall': n.get('t_wall') or 0, 'text': n.get('text') or ''}
                      for n in (pack.get('notes') or [])]
        self.bridge.reset()
        if self.dataset != 'pack':
            # 换包不换数据源：层保持用户选的那份
            self.bridge.pin_layers()
        return self.tl['session']

    def apply_dataset(self, ds, em=None, seg=None):
        """切数据源：pack = 跟随包；其余用 img/seg 重建层，并钉住不再被 state 覆盖。"""
        if ds == 'pack':
            self.dataset = 'pack'
            self.bridge.reset()
            return {'ok': True, 'dataset': 'pack'}
        preset = next((d for d in DATASETS if d['id'] == ds), None)
        if ds == 'custom':
            preset = {'em': (em or '').strip(), 'seg': (seg or '').strip()}
        if not preset or not preset.get('em') or not preset.get('seg'):
            return {'err': '数据源需要 EM 与分割两个 URL'}
        self.bridge.send('img em ' + preset['em'])
        self.bridge.send('seg autoseg ' + preset['seg'])
        self.bridge.pin_layers()
        self.dataset = ds
        return {'ok': True, 'dataset': ds}

    def seek(self, t_rel):
        """t（相对秒）→ 最近一份快照 + 该时刻的数据描述。"""
        t_wall = self.t0 + float(t_rel) * 1000.0
        snap = None
        for s in self.snaps:
            if s['t_wall'] <= t_wall:
                snap = s
            else:
                break
        if snap is None:
            snap = self.snaps[0] if self.snaps else None
        if snap is None:
            return {'err': 'no_state'}
        applied = self.bridge.apply(snap['state'])
        st = snap['state']
        shot = None
        best = 2.0
        for s in self.shots:
            d = abs((s['t_wall'] - snap['t_wall']) / 1000.0)
            if d < best:
                best, shot = d, s
        notes = [n['text'] for n in self.notes
                 if abs((n['t_wall'] - snap['t_wall']) / 1000.0) <= 3.0]
        return {
            'asked_t': round(float(t_rel), 2),
            'at': {'t': snap['t'], 'seq': snap['seq'],
                   'position': [round(float(x), 2) for x in (st.get('position') or [])],
                   'cross': st.get('crossSectionScale'),
                   'proj': st.get('projectionScale'),
                   'layout': st.get('layout'),
                   'segments': segs_of(st),
                   'n_segments': sum(len(v) for v in segs_of(st).values())},
            'applied': applied,
            'shot': ({'id': shot['id'], 'url': shot['url'],
                      'status': shot.get('status'),
                      'dt': round((shot['t_wall'] - snap['t_wall']) / 1000.0, 2)}
                     if shot else None),
            'notes': notes,
        }


class Runner:
    """后台跑“精确重放 / 导出片段”（调用 replay_manual_session.py）。

    为什么不在本进程里做：逐条重放要 Playwright 控制一个**自己创建**的浏览器，
    而 studio 的 NG 在用户浏览器里（iframe），没法向跨源页面喂合成输入。
    同一时刻只允许一个任务：两个 Playwright 窗口会抢焦点，也难看出进度。
    """

    def __init__(self, packdir):
        self.packdir = Path(packdir) if packdir else None
        self.proc = None
        self.kind = ''
        self.log = ''
        self.out = ''
        self.new_pack = ''
        self.open_failed = ''
        self.open_hint = ''
        self.started_at = 0.0
        self.lock = threading.Lock()

    def busy(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self, kind, t=0, opts=None):
        opts = opts or {}
        with self.lock:
            if self.busy():
                return {'err': '已有任务在跑，等它结束'}
            if kind == 'collect':
                # 采集：开一个已注入 recorder 的浏览器窗口（用户自己操作）。
                # 退出码 / 新包目录由 _pump 收尾时补上。
                # 直接调 python：demo_manual.sh 是 bash，Windows 上跑不了
                cmd = [_child_py(), str(HERE / 'cave_manual_server.py'),
                       '--open']
                # 首页填的"存哪 / 叫什么 / 用哪份数据"从这里进去
                if opts.get('out'):
                    cmd += ['--out', str(opts['out'])]
                if opts.get('session_name'):
                    cmd += ['--session-name', str(opts['session_name'])]
                ds = opts.get('dataset') or 'pack'
                if ds == 'custom':
                    if opts.get('em'):
                        cmd += ['--em', str(opts['em'])]
                    if opts.get('seg'):
                        cmd += ['--seg', str(opts['seg'])]
                elif ds != 'pack':
                    d = next((x for x in DATASETS if x['id'] == ds), None)
                    if d and d.get('em'):
                        cmd += ['--em', d['em']]
                    if d and d.get('seg'):
                        cmd += ['--seg', d['seg']]
            else:
                cmd = [_child_py(), '-u',
                       str(HERE / 'replay_manual_session.py'),
                       '--pack', str(self.packdir),
                       '--seek-t', '%.2f' % float(t)]
                if kind == 'precise':
                    cmd += ['--headed', '--no-pixels']
                else:
                    cmd += ['--video']
            self.kind, self.log, self.out, self.new_pack = kind, '', '', ''
            self.open_failed, self.open_hint = '', ''
            self.started_at = time.time()
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT,
                                         text=True, encoding='utf-8',
                                         errors='replace', bufsize=1,
                                         env=_utf8_env())
            threading.Thread(target=self._pump, daemon=True).start()
            # 带 cmd 出来：首页/日志里能核对"到底用什么参数起的"
            return {'ok': True, 'kind': kind, 'cmd': cmd}

    def _pump(self):
        for line in self.proc.stdout:
            self.log = (self.log + line)[-6000:]
            if line.startswith('报告:'):
                self.out = line.split(':', 1)[1].strip()
            if self.kind == 'collect' and line.startswith('[open] 打开浏览器失败'):
                # 采集子进程开不了窗口时会留着服务继续等 --cdp/油猴（CLI 场景合理），
                # 工作台里没终端可 Ctrl+C，会一直占着 busy，之后每次点按钮只回
                # “已有任务在跑”——看起来就是“点了没反应”。这里直接收尾。
                self.open_failed = line.strip()
                # 补救命令自己拼（杀掉子进程后它的后续行读不到了），文案沿用采集端那份
                self.open_hint = cms.open_fail_hint(line)
                try:
                    self.proc.kill()
                except OSError:
                    pass
                break
        if self.kind == 'collect':
            self.new_pack = newest_pack_since(self.started_at - 5) or ''

    def status(self):
        lines = [l for l in self.log.strip().splitlines() if l.strip()]
        return {'kind': self.kind, 'running': self.busy(),
                'tail': lines[-6:], 'out': self.out,
                'open_failed': self.open_failed,
                'open_hint': self.open_hint,
                'new_pack': self.new_pack}


HTML = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>回放工作台</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #ffffff; color: #1f2328;
         font: 13px/1.5 -apple-system, "PingFang SC", "Helvetica Neue", sans-serif; }
  #app { display: grid; grid-template-columns: 1fr 300px; grid-template-rows: 40px 1fr 86px;
         height: 100vh; }
  header { grid-column: 1 / 3; display: flex; align-items: center; gap: 12px;
           padding: 0 12px; border-bottom: 1px solid #d7dce4; background: #fff; }
  header b { color: #111827; font-weight: 600; }
  header .muted { color: #667085; }
  header .right { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  #stage { position: relative; background: #000; overflow: hidden; }
  #ng { width: 100%; height: 100%; border: 0; display: block; }
  #side { border-left: 1px solid #d7dce4; padding: 10px 12px; overflow: auto; background: #fff; }
  #side h4 { margin: 12px 0 6px; font-size: 11px; letter-spacing: .08em;
             text-transform: uppercase; color: #667085; font-weight: 600; }
  #side h4:first-child { margin-top: 0; }
  .kv { display: grid; grid-template-columns: 72px 1fr; gap: 2px 8px; }
  .kv span:nth-child(odd) { color: #667085; }
  .kv span:nth-child(even) { color: #1f2328; word-break: break-all; }
  #bars { grid-column: 1 / 3; border-top: 1px solid #d7dce4; padding: 6px 12px 10px; background: #fff; }
  #track { position: relative; height: 38px; margin-top: 4px; }
  #ticks { position: absolute; left: 0; right: 0; top: 0; height: 12px; }
  .tick { position: absolute; top: 3px; width: 2px; height: 7px; background: #c5ccd6; }
  .tick.shot { background: #b86e00; height: 12px; top: 0; width: 3px; }
  .tick.note { background: #2d6cdf; height: 12px; top: 0; width: 3px; border-radius: 1px; }
  input[type=range] { position: absolute; left: 0; right: 0; top: 12px; width: 100%;
                      margin: 0; accent-color: #4f8cff; }
  #row { display: flex; align-items: center; gap: 10px; margin-top: 2px; flex-wrap: wrap; }
  #task { color: #667085; font-size: 11px; }
  button { background: #ffffff; color: #24292f; border: 1px solid #c9d0da;
           border-radius: 5px; padding: 4px 10px; cursor: pointer; font-size: 12px; }
  button:hover { background: #f0f3f7; }
  button.primary { background: #2d6cdf; color: #fff; border-color: #2d6cdf; }
  #time { font-variant-numeric: tabular-nums; color: #24292f; }
  #thumb { width: 100%; border: 1px solid #c9d0da; border-radius: 4px; display: none; }
  #shotinfo { color: #667085; font-size: 11px; margin-top: 4px; }
  #note { color: #1f5fbf; font-size: 12px; margin-top: 6px; white-space: pre-wrap; }
  .note { cursor: pointer; color: #1f5fbf; padding: 1px 0; font-size: 12px; }
  .note:hover { text-decoration: underline; }
  #applied { color: #667085; font-size: 11px; margin-top: 4px; }
  #hint { color: #667085; font-size: 11px; }
  select, input { background: #ffffff; color: #24292f; border: 1px solid #c9d0da;
                  border-radius: 5px; padding: 3px 6px; font-size: 12px; }
  header select { max-width: 250px; }
  #customBox { margin-top: 10px; }
  #customBox input { width: 100%; margin-bottom: 4px; }

  /* 顶部 tab：一个窗口里切功能（回放 / 看包 / 派生） */
  header { flex-wrap: nowrap; overflow: hidden; }
  header .right { white-space: nowrap; flex-shrink: 0; }
  #packname { max-width: 240px; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  #tabs { display: flex; gap: 4px; margin-left: 10px; flex-shrink: 0; }
  #tabs button { background: #ffffff; color: #667085; border: 1px solid #c9d0da;
                 border-radius: 6px; padding: 4px 12px; font-size: 12px;
                 cursor: pointer; white-space: nowrap; }
  #tabs button.on { background: #2d6cdf; color: #fff; border-color: #2d6cdf; }
  .pane { display: none; }
  .pane.on { display: block; }
  /* #app 是 grid（主区 + 300px 侧栏）；切到看包/派生时侧栏收起，栅格位跨两列 */
  #stage.pane.on { display: block; }
  #paneHome.pane.on, #panePack.pane.on, #paneDerive.pane.on {
    display: block; grid-column: 1 / 3; grid-row: 2 / 4;
  }
  #paneHome, #panePack, #paneDerive {
    padding: 16px 20px; overflow: auto; height: calc(100vh - 46px);
    box-sizing: border-box;
  }
  #paneHome { max-width: 880px; }
  #paneHome h3 { font-size: 13px; color: #344054; margin: 0 0 10px; }
  #paneHome .sec { background: #ffffff; border: 1px solid #d7dce4;
                   border-radius: 10px; padding: 14px 16px; margin-bottom: 16px; }
  #paneHome label { display: inline-block; min-width: 62px; color: #667085;
                    font-size: 12px; }
  #paneHome input, #paneHome select { background: #ffffff; color: #24292f;
                    border: 1px solid #c9d0da; border-radius: 6px;
                    padding: 5px 8px; font: 12px ui-monospace,Menlo,monospace; }
  #paneHome input.wide { width: 420px; }
  #paneHome .row { margin: 6px 0; display: flex; align-items: center; gap: 8px;
                   flex-wrap: wrap; }
  #paneHome .srow { display: flex; align-items: center; gap: 10px;
                    padding: 7px 0; border-bottom: 1px solid #edf0f4; font-size: 12px; }
  #paneHome .srow:last-child { border-bottom: 0; }
  #paneHome .srow .sname { flex: 1; color: #24292f;
                           font: 12px ui-monospace,Menlo,monospace; }
  #paneHome .srow.cur .sname { color: #087443; }
  #paneHome .srow .meta { color: #667085; }
  #paneHome .hmsg { color: #667085; font-size: 12px; margin-left: 8px; }
  #panePack h4, #paneDerive h4 { margin: 18px 0 6px; font-size: 12px;
                                  color: #667085; font-weight: 600; }
  .toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
  .cards { display: flex; flex-wrap: wrap; gap: 10px; }
  .card { background: #ffffff; border: 1px solid #d7dce4; border-radius: 8px;
          padding: 10px 18px; min-width: 110px; }
  .card .n { font-size: 20px; font-weight: 600; }
  .card .l { font-size: 11px; color: #667085; margin-top: 2px; }
  .kv.wide { display: block; background: #ffffff; border: 1px solid #d7dce4;
             border-radius: 8px; padding: 10px 14px;
             font: 12px/1.7 ui-monospace,Menlo,monospace;
             white-space: pre-wrap; word-break: break-word; }
  pre.out { background: #f8fafc; border: 1px solid #d7dce4; border-radius: 8px;
            padding: 12px 14px; font: 12px/1.6 ui-monospace,Menlo,monospace;
            white-space: pre-wrap; overflow: auto; max-height: calc(100vh - 180px);
            margin: 0; }
</style>
</head>
<body>
<div id="app">
  <header>
    <b>Neuromancer</b>
    <nav id="tabs">
      <button data-tab="home" class="on">开始</button>
      <button data-tab="replay">回放</button>
      <button data-tab="pack">看包</button>
      <button data-tab="derive">派生</button>
    </nav>
    <span class="muted" id="packname"
          style="max-width:220px;overflow:hidden;text-overflow:ellipsis;
                 white-space:nowrap">—</span>
    <span class="muted" id="hint">拖动时间轴 / 空格播放</span>
    <span class="right">
      <select id="packsel" title="回放包"></select>
      <select id="dssel" title="数据源"></select>
      <button id="openbtn">打开</button>
      <span class="muted" id="task"></span>
      <button id="collect">采集新会话</button>
    </span>
  </header>

  <div id="paneHome" class="pane on" data-pane="home">
    <h3>采集一段</h3>
    <div class="sec">
      <div class="row">
        <label>数据源</label>
        <select id="hdssel" style="min-width:260px"></select>
      </div>
      <div class="row" id="hCustom" style="display:none">
        <label>EM</label>
        <input id="hem" class="wide" placeholder="precomputed://… 或 graphene://…">
      </div>
      <div class="row" id="hCustom2" style="display:none">
        <label>分割</label>
        <input id="hseg" class="wide" placeholder="precomputed://… 或 graphene://…">
      </div>
      <div class="row">
        <label>保存到</label>
        <input id="hout" class="wide" placeholder="留空 = 默认目录">
        <button id="hbrowse">浏览…</button>
        <span class="hmsg" id="houtdef"></span>
      </div>
      <div class="row">
        <label>会话名</label>
        <input id="hname" placeholder="留空自动（sess_日期_随机）">
      </div>
      <div class="row" style="margin-top:10px">
        <button id="hstart">开始采集</button>
        <span class="hmsg" id="hmsg"></span>
      </div>
      <div class="hmsg" id="hwarn" style="color:#b42318"></div>
      <div class="hmsg" style="margin-top:8px;line-height:1.7">
        点了会在新窗口里打开 Neuroglancer 并开始录；录完先点左下角「结束并保存」，再关窗口。
      </div>
    </div>

    <h3>已有会话</h3>
    <div class="sec">
      <div class="hmsg" id="homeRoot" style="margin-bottom:8px"></div>
      <div id="hsessions"><span class="hmsg">读取中…</span></div>
    </div>
  </div>

  <div id="stage" class="pane" data-pane="replay">
    <iframe id="ng" allow="fullscreen"></iframe>
  </div>

  <div id="panePack" class="pane" data-pane="pack">
    <div class="toolbar">
      <span class="muted">当前包：<b id="pkname">—</b></span>
      <button id="pkrefresh">重新读取</button>
      <span class="muted" id="pkmsg"></span>
    </div>
    <div class="cards" id="pkcards"></div>
    <h4>事件构成</h4>
    <div class="kv wide" id="pkevents">—</div>
    <h4>锚点（能用于回放的）</h4>
    <div class="kv wide" id="pkanchors">—</div>
    <h4>完整性告警</h4>
    <div class="kv wide" id="pkwarn">—</div>
  </div>

  <div id="paneDerive" class="pane" data-pane="derive">
    <div class="toolbar">
      <span class="muted">对当前包跑后处理（直接调用现有命令行工具）</span>
      <button id="dvEp">切段（episodes）</button>
      <button id="dvJoin">关联 op log（join）</button>
      <span class="muted" id="dvmsg"></span>
    </div>
    <pre class="out" id="dvout">点上面的按钮开始。产出落在包的 derived/ 目录。</pre>
  </div>

  <aside id="side">
    <h4>这一刻</h4>
    <div class="kv" id="at"></div>
    <h4>关键帧</h4>
    <img id="thumb" alt="keyframe">
    <div id="shotinfo">附近没有关键帧</div>
    <h4>附近备注</h4>
    <div id="note">—</div>
    <h4>全部备注（点击跳转）</h4>
    <div id="notes">—</div>
    <h4>实际发出的指令</h4>
    <div id="applied">—</div>
    <div id="customBox" style="display:none">
      <h4>自定义数据源</h4>
      <input id="emurl" placeholder="EM: precomputed://…">
      <input id="segurl" placeholder="分割: precomputed://…">
      <button id="applycustom">应用到当前包</button>
    </div>
  </aside>

  <div id="bars">
    <div id="track">
      <div id="ticks"></div>
      <input id="range" type="range" min="0" max="1" step="0.05" value="0">
    </div>
    <div id="row">
      <button id="play" class="primary">播放</button>
      <span id="time">0.0 / 0.0 s</span>
      <span class="muted">速度</span>
      <select id="speed">
        <option value="1">1x</option>
        <option value="2">2x</option>
        <option value="4">4x</option>
        <option value="8">8x</option>
      </select>
      <button id="prev">上一检查点</button>
      <button id="next">下一检查点</button>
      <button id="precise">精确重放到这里</button>
      <button id="export">导出这一段视频</button>
      <span class="muted" id="stateinfo"></span>
    </div>
  </div>
</div>
<script>
let TL = null, snaps = [], cur = 0, playing = false, timer = null, lastSeek = 0;

function fmt(x) { return (Math.round(x * 10) / 10).toFixed(1); }
function num(x) {
  if (typeof x !== 'number') return (x === null || x === undefined) ? '—' : x;
  if (Math.abs(x) >= 10000) return x.toExponential(2);
  return (Math.round(x * 100) / 100).toString();
}

function build(d) {
  TL = d;
  snaps = d.snapshots;
  const range = document.getElementById('range');
  range.max = Math.max(0.1, (d.t1 - d.t0) / 1000);
  range.step = 0.05;
  document.getElementById('packname').textContent = d.session;
  document.getElementById('ng').src = d.viewer_url;
  const ticks = document.getElementById('ticks');
  const span = Math.max(0.1, (d.t1 - d.t0) / 1000);
  ticks.innerHTML = '';
  snaps.forEach(s => {
    const el = document.createElement('div');
    el.className = 'tick'; el.style.left = (s.t / span * 100) + '%';
    ticks.appendChild(el);
  });
  d.shots.forEach(s => {
    const el = document.createElement('div');
    el.className = 'tick shot'; el.style.left = (s.t / span * 100) + '%';
    el.title = '关键帧 ' + s.id;
    ticks.appendChild(el);
  });
  d.notes.forEach(n => {
    const el = document.createElement('div');
    el.className = 'tick note'; el.style.left = (n.t / span * 100) + '%';
    el.title = '备注：' + n.text;
    ticks.appendChild(el);
  });
  renderNotes(d);
}

function esc(s) {
  return String(s).replace(/[&<>"]/g,
    c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}

function renderNotes(d) {
  const box = document.getElementById('notes');
  if (!box) return;
  if (!d.notes || !d.notes.length) { box.textContent = '—'; return; }
  box.innerHTML = d.notes.map(n =>
    '<div class="note" data-t="' + n.t + '">' + fmt(n.t) + ' s｜' +
    esc(n.text) + '</div>').join('');
  Array.from(box.querySelectorAll('.note')).forEach(el => {
    el.addEventListener('click',
      () => seek(parseFloat(el.getAttribute('data-t'))));
  });
}

function runTask(kind) {
  const el = document.getElementById('task');
  el.textContent = kind === 'precise' ? '正在新开窗口…'
                 : kind === 'collect' ? '正在打开采集窗口…' : '正在导出…';
  fetch('/api/run', {method: 'POST', body: JSON.stringify({kind: kind, t: cur})})
    .then(r => r.json()).then(d => {
      if (d.err) { el.textContent = d.err; return; }
      pollTask();
    });
}

function pollTask() {
  fetch('/api/run').then(r => r.json()).then(d => {
    const el = document.getElementById('task');
    if (d.running) {
      el.textContent = (d.kind === 'precise' ? '重放中：'
                      : d.kind === 'collect' ? '采集窗口开着：' : '导出中：') +
        (d.tail.slice(-1)[0] || '…');
      setTimeout(pollTask, 1500);
    } else if (d.kind === 'collect' && d.new_pack) {
      el.textContent = '新包：' + d.new_pack.split('/').pop() + ' ';
      const btn = document.createElement('button');
      btn.textContent = '载入';
      btn.style.marginLeft = '6px';
      btn.onclick = () => fetch('/api/load',
        {method: 'POST', body: JSON.stringify({pack: d.new_pack})})
        .then(r => r.json())
        .then(x => { if (x.err) { el.textContent = x.err; } else { location.reload(); } });
      el.appendChild(btn);
    } else if (d.open_failed) {
      // 窗口没开起来时把原因和补救命令直接拉到页面上，不让人去猜
      el.textContent = '采集窗口没开起来：' + d.open_failed +
        (d.open_hint ? '　' + d.open_hint : '');
    } else if (d.out) {
      el.textContent = '完成 → ' + d.out;
    } else if (d.tail.length) {
      el.textContent = '结束：' + d.tail.slice(-1)[0];
    }
  });
}

function render(d) {
  if (typeof updateMouse === 'function') updateMouse(cur);
  const at = d.at || {};
  const seg = at.segments || {};
  const segTxt = Object.keys(seg).map(k => k + ': ' + seg[k].length + ' 个')
                   .join('<br>') || '—';
  document.getElementById('at').innerHTML =
    '<span>位置</span><span>' + (at.position || []).join(', ') + '</span>' +
    '<span>缩放</span><span>cross ' + num(at.cross) + ' / proj ' + num(at.proj) + '</span>' +
    '<span>布局</span><span>' + (at.layout || '—') + '</span>' +
    '<span>选中集</span><span>' + segTxt + '</span>' +
    '<span>快照</span><span>#' + (at.seq ?? '—') + ' @ ' + fmt(at.t || 0) + ' s</span>';
  const img = document.getElementById('thumb');
  const info = document.getElementById('shotinfo');
  if (d.shot) {
    img.style.display = 'block';
    img.src = d.shot.url + '?v=' + d.shot.id;
    info.textContent = d.shot.id + '（' + (d.shot.status || '') + '，距此 ' +
                       d.shot.dt + ' s）';
  } else {
    img.style.display = 'none';
    info.textContent = '附近没有关键帧';
  }
  document.getElementById('note').textContent = (d.notes && d.notes.length)
    ? d.notes.join('\n') : '—';
  document.getElementById('applied').textContent = (d.applied && d.applied.length)
    ? d.applied.map(x => x.split(' ')[0]).join(', ') : '无（画面未变）';
  document.getElementById('time').textContent = fmt(d.at ? d.at.t : 0) +
    ' / ' + fmt((TL.t1 - TL.t0) / 1000) + ' s';
}

// 鼠标标记：工作台的 NG 在跨源 iframe 里，没法往里注入，
// 只能在外壳页上叠一个红点，位置 = iframe 偏移 + 包里的页面像素。
let mouseDot = null;
function ensureDot() {
  if (mouseDot) return mouseDot;
  mouseDot = document.createElement('div');
  mouseDot.id = 'replay-mouse';
  mouseDot.style.cssText = 'position:fixed;width:13px;height:13px;'
    + 'border-radius:50%;background:rgba(255,64,64,.92);border:2px solid #fff;'
    + 'box-shadow:0 0 6px rgba(0,0,0,.7);transform:translate(-50%,-50%);'
    + 'pointer-events:none;z-index:2147483647;display:none';
  document.body.appendChild(mouseDot);
  return mouseDot;
}

function updateMouse(t) {
  const dot = ensureDot();
  const arr = TL.mouse || [];
  if (!arr.length) { dot.style.display = 'none'; return; }
  // 二分找最后一条 t <= 当前时刻
  let lo = 0, hi = arr.length - 1, best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (arr[mid].t <= t) { best = arr[mid]; lo = mid + 1; } else { hi = mid - 1; }
  }
  if (!best) best = arr[0];
  const box = document.getElementById('ng').getBoundingClientRect();
  if (!box.width || !box.height) { dot.style.display = 'none'; return; }
  const vp = TL.viewport || [1280, 740];
  const sx = box.width / Math.max(1, vp[0]);
  const sy = box.height / Math.max(1, vp[1]);
  // 夹到 iframe 内：记录点可能落在录制页的侧栏/工具栏，不能直接加偏移。
  const x = Math.max(0, Math.min(box.width, best.x * sx));
  const y = Math.max(0, Math.min(box.height, best.y * sy));
  dot.style.left = (box.left + x) + 'px';
  dot.style.top = (box.top + y) + 'px';
  dot.style.display = 'block';
  dot.title = 't=' + best.t + 's (' + Math.round(best.x) + ', ' +
              Math.round(best.y) + ')';
}

function seek(t) {
  cur = t;
  updateMouse(t);
  const range = document.getElementById('range');
  range.value = t;
  const now = performance.now();
  if (now - lastSeek < 60) return;
  lastSeek = now;
  fetch('/api/seek', {method: 'POST', body: JSON.stringify({t: t})})
    .then(r => r.json()).then(d => { if (!d.err) render(d); });
}

function toggle() {
  playing = !playing;
  document.getElementById('play').textContent = playing ? '暂停' : '播放';
  if (playing) {
    const speed = parseFloat(document.getElementById('speed').value);
    timer = setInterval(() => {
      const max = (TL.t1 - TL.t0) / 1000;
      if (cur >= max) { toggle(); return; }
      seek(Math.min(max, cur + 0.25 * speed));
    }, 250);
  } else if (timer) {
    clearInterval(timer); timer = null;
  }
}

document.getElementById('range').addEventListener('input', e => seek(parseFloat(e.target.value)));
document.getElementById('play').addEventListener('click', toggle);
document.getElementById('speed').addEventListener('change', () => {
  if (playing) { toggle(); toggle(); }
});
document.getElementById('prev').addEventListener('click', () => {
  for (let i = snaps.length - 1; i >= 0; i--) {
    if (snaps[i].t < cur - 0.05) { seek(snaps[i].t); return; }
  }
  seek(0);
});
document.getElementById('next').addEventListener('click', () => {
  for (let i = 0; i < snaps.length; i++) {
    if (snaps[i].t > cur + 0.05) { seek(snaps[i].t); return; }
  }
  seek((TL.t1 - TL.t0) / 1000);
});
document.getElementById('precise').addEventListener('click', () => runTask('precise'));
document.getElementById('export').addEventListener('click', () => runTask('export'));
document.getElementById('collect').addEventListener('click', () => runTask('collect'));

// ---- 打开哪个包 / 看哪份数据 -------------------------------------------
function loadCatalog() {
  fetch('/api/catalog').then(r => r.json()).then(c => {
    const ps = document.getElementById('packsel');
    ps.innerHTML = (c.packs || []).map(p =>
      '<option value="' + p + '"' + (p === c.current_pack ? ' selected' : '') +
      '>' + p.split('/').pop() + '</option>').join('');
    const opts = (c.datasets || []).map(d =>
      '<option value="' + d.id + '">' + d.label + '</option>').join('');
    const ds = document.getElementById('dssel');
    ds.innerHTML = opts;
    ds.value = c.current_dataset || 'pack';
    // 首页的同一份列表（采集时用）
    const hs = document.getElementById('hdssel');
    if (hs) {
      hs.innerHTML = opts;
      hs.value = 'minnie65-bossdb';
      document.getElementById('houtdef').textContent = '默认：' + (c.root || '');
      hs.onchange = () => {
        const on = hs.value === 'custom';
        document.getElementById('hCustom').style.display = on ? 'flex' : 'none';
        document.getElementById('hCustom2').style.display = on ? 'flex' : 'none';
      };
    }
    document.getElementById('customBox').style.display =
      ds.value === 'custom' ? 'block' : 'none';
    ds.onchange = () => {
      document.getElementById('customBox').style.display =
        ds.value === 'custom' ? 'block' : 'none';
    };
  });
}

// ---- 首页：采集 + 已有会话 -------------------------------------------
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function homeMsg(t) {
  const el = document.getElementById('hmsg');
  if (el) el.textContent = t || '';
}
let collectTimer = null;
function pollCollect() {
  if (collectTimer) clearInterval(collectTimer);
  collectTimer = setInterval(() => {
    fetch('/api/run').then(r => r.json()).then(d => {
      if (d.running) return;
      clearInterval(collectTimer); collectTimer = null;
      if (d.open_failed) {
        homeMsg('采集窗口没开起来：' + d.open_failed +
                (d.open_hint ? '　' + d.open_hint : ''));
        return;
      }
      homeMsg(d.new_pack ? ('已保存：' + d.new_pack) : '采集结束。');
      loadHome();
    });
  }, 2000);
}
function loadHome() {
  fetch('/api/sessions').then(r => r.json()).then(d => {
    const warn = document.getElementById('hwarn');
    if (warn) {
      const py = d.py || {};
      warn.textContent = py.ok === false
        ? ('这台机器的解释器（' + py.exe + '）开不了采集窗口：' + py.why +
           '　先跑：' + (py.fixes || []).join('　'))
        : '';
      // 解释器是启动后在后台问的，第一次刷新时可能还没结果，过几秒再看一次
      if (py.ok === null || py.ok === undefined) setTimeout(loadHome, 4000);
    }
    document.getElementById('homeRoot').textContent = '目录：' + d.root;
    const box = document.getElementById('hsessions');
    if (!d.sessions || !d.sessions.length) {
      box.innerHTML = '<span class="hmsg">还没有会话 —— 上面填好点「开始采集」。</span>';
      return;
    }
    box.innerHTML = d.sessions.map(s => {
      const t = (s.when || '').replace('T', ' ').slice(0, 16);
      return '<div class="srow' + (s.is_current ? ' cur' : '') + '">'
        + '<span class="sname">' + esc(s.name) + '</span>'
        + '<span class="meta">' + t + ' · ' + (s.n_events || 0) + ' 事件 · '
        + (s.duration_s || 0) + 's · ' + (s.n_shots || 0) + ' 帧</span>'
        + (s.has_html ? '<button data-html="' + esc(s.path) + '">看轨迹</button>' : '')
        + '<button data-open="' + esc(s.path) + '">回放</button>'
        + '<button data-ren="' + esc(s.path) + '" title="改目录名">改名</button>'
        + '<button data-del="' + esc(s.path) + '" title="移到回收站 _trash">删除</button>'
        + '</div>';
    }).join('');
    box.querySelectorAll('button[data-open]').forEach(b => {
      b.onclick = () => {
        fetch('/api/open', {method: 'POST',
          body: JSON.stringify({pack: b.dataset.open})})
          .then(r => r.json()).then(() => {
            location.hash = 'replay';
            location.reload();
          });
      };
    });
    box.querySelectorAll('button[data-html]').forEach(b => {
      b.onclick = () => window.open('/html?pack=' +
        encodeURIComponent(b.dataset.html), '_blank');
    });
    box.querySelectorAll('button[data-ren]').forEach(b => {
      b.onclick = () => {
        const cur = b.dataset.ren.replace(/\/+$/, '').split('/').pop();
        const nn = prompt('新的会话名（就是目录名）：', cur);
        if (!nn || nn === cur) return;
        fetch('/api/packs/op', {method: 'POST',
          body: JSON.stringify({op: 'rename', path: b.dataset.ren, name: nn})})
          .then(r => r.json()).then(d => {
            if (d.err) { alert(d.err); return; }
            homeMsg('已改名为 ' + d.name);
            loadHome(); loadCatalog();
          });
      };
    });
    box.querySelectorAll('button[data-del]').forEach(b => {
      b.onclick = () => {
        const name = b.dataset.del.replace(/\/+$/, '').split('/').pop();
        if (!confirm('把「' + name + '」移到回收站？\n\n'
                     + '会移到 manual_sessions/_trash/，确认后再手动清理。')) return;
        fetch('/api/packs/op', {method: 'POST',
          body: JSON.stringify({op: 'delete', path: b.dataset.del})})
          .then(r => r.json()).then(d => {
            if (d.err) { alert(d.err); return; }
            homeMsg('已移到回收站 ' + (d.name || ''));
            loadHome(); loadCatalog();
          });
      };
    });
  });
}

const hbrowseBtn = document.getElementById('hbrowse');
if (hbrowseBtn) {
  hbrowseBtn.addEventListener('click', () => {
    homeMsg('等系统对话框…（在弹出的窗口里选目录）');
    fetch('/api/pickdir').then(r => r.json()).then(d => {
      if (d.dir) {
        document.getElementById('hout').value = d.dir;
        homeMsg('保存位置：' + d.dir);
      } else {
        homeMsg('没有选目录（或系统没有可用的选择框，直接手打路径也行）');
      }
    }).catch(() => homeMsg('选择框调用失败，直接手打路径'));
  });
}
const hstartBtn = document.getElementById('hstart');
if (hstartBtn) {
  hstartBtn.addEventListener('click', () => {
    const ds = document.getElementById('hdssel').value;
    const body = {kind: 'collect', dataset: ds,
                  out: document.getElementById('hout').value.trim(),
                  session_name: document.getElementById('hname').value.trim()};
    if (ds === 'custom') {
      body.em = document.getElementById('hem').value.trim();
      body.seg = document.getElementById('hseg').value.trim();
      if (!body.em || !body.seg) { homeMsg('自定义数据源需要 EM 和分割两个 URL'); return; }
    }
    if (!body.out) delete body.out;
    if (!body.session_name) delete body.session_name;
    homeMsg('正在打开采集窗口…');
    fetch('/api/run', {method: 'POST', body: JSON.stringify(body)})
      .then(r => r.json()).then(d => {
        if (d.err) { homeMsg(d.err); return; }
        homeMsg('采集窗口已打开。录完点「结束并保存」再关窗口，这里会自动刷新。');
        pollCollect();
      });
  });
}
loadHome();

function openSelection() {
  const body = {pack: document.getElementById('packsel').value,
                dataset: document.getElementById('dssel').value};
  if (body.dataset === 'custom') {
    body.em = document.getElementById('emurl').value.trim();
    body.seg = document.getElementById('segurl').value.trim();
    if (!body.em || !body.seg) { alert('自定义数据源需要 EM 和分割两个 URL'); return; }
  }
  fetch('/api/open', {method: 'POST', body: JSON.stringify(body)})
    .then(r => r.json()).then(d => {
      if (d.err) { alert(d.err); return; }
      location.reload();
    });
}

document.getElementById('openbtn').addEventListener('click', openSelection);
document.getElementById('applycustom').addEventListener('click', openSelection);
loadCatalog();
document.addEventListener('keydown', e => {
  if (e.code === 'Space' && e.target.tagName !== 'INPUT') { e.preventDefault(); toggle(); }
  if (e.code === 'ArrowRight') { seek(cur + 1); }
  if (e.code === 'ArrowLeft') { seek(Math.max(0, cur - 1)); }
});

fetch('/api/timeline').then(r => r.json()).then(d => {
  build(d);
  seek(0);
  setInterval(() => {
    document.getElementById('stateinfo').textContent =
      snaps.length + ' 个状态快照 / ' + d.shots.length + ' 张关键帧';
  }, 500);
});

/* ---------- 顶部 tab：一个窗口里切功能 ---------- */
let curTab = 'home';
document.querySelectorAll('#tabs button').forEach(function (b) {
  b.onclick = function () { switchTab(b.dataset.tab); };
});
function switchTab(name) {
  curTab = name;
  document.querySelectorAll('#tabs button').forEach(function (b) {
    b.classList.toggle('on', b.dataset.tab === name);
  });
  document.querySelectorAll('.pane').forEach(function (p) {
    p.classList.toggle('on', p.dataset.pane === name);
  });
  // 侧栏与时间轴只属于回放，切走时一并收起
  const isReplay = name === 'replay';
  document.getElementById('side').style.display = isReplay ? '' : 'none';
  document.getElementById('bars').style.display = isReplay ? '' : 'none';
  // 顶栏的快捷键提示只对回放有意义
  const hint = document.getElementById('hint');
  if (hint) hint.style.display = isReplay ? '' : 'none';
  if (name === 'pack') loadPack();
}

// 初始 tab：URL hash 优先，否则首页（放在 switchTab 之后，避免 TDZ）
(function () {
  const want = (location.hash || '').replace('#', '');
  const ok = want && document.querySelector('button[data-tab="' + want + '"]');
  switchTab(ok ? want : 'home');
})();

/* ---------- 看包 ---------- */
function fmtBytes(n) {
  if (n == null) return '—';
  return n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
                     : (n / 1024).toFixed(0) + ' KB';
}
function card(n, l) {
  return '<div class="card"><div class="n">' + n +
         '</div><div class="l">' + l + '</div></div>';
}
function loadPack() {
  const pack = document.getElementById('packsel').value || '';
  document.getElementById('pkmsg').textContent = '读取中…';
  fetch('/api/inspect?pack=' + encodeURIComponent(pack))
    .then(r => r.json()).then(function (d) {
      if (d.err) {
        document.getElementById('pkmsg').textContent = '失败：' + d.err;
        return;
      }
      document.getElementById('pkmsg').textContent = '';
      document.getElementById('pkname').textContent = (d.session_id || '—');
      document.getElementById('pkcards').innerHTML =
        card(d.n_events, '事件') +
        card(d.duration_s == null ? '—' : d.duration_s + 's', '时长') +
        card(d.n_state_full, '完整 state') +
        card(d.n_shots_usable + '/' + d.n_shots, '可用关键帧') +
        card(d.n_notes, '备注') +
        card(fmtBytes(d.bytes), '体积') +
        card(d.integrity === 'present' ? '✓' : '缺', 'integrity');
      const c = d.counts || {};
      document.getElementById('pkevents').textContent =
        Object.keys(c).length
          ? Object.keys(c).map(k => k + '  ' + c[k]).join('\n') : '—';
      const a = d.anchors || [];
      document.getElementById('pkanchors').textContent = a.length
        ? a.map(x => '[ok] ' + (x.reason || '?') + '  state_id=' + x.state_id +
                     '  t=' + x.t_wall).join('\n')
        : '没有带 position 的锚点';
      const warn = [];
      const h = d.health || {};
      if (h.dropped_events) warn.push('丢事件 ' + h.dropped_events + ' 条');
      if (h.dropped_checkpoints) warn.push('丢检查点 ' + h.dropped_checkpoints + ' 个');
      if (h.gaps) warn.push('缺口 ' + h.gaps + ' 次');
      if ((d.anchors_dropped || []).length)
        warn.push('被丢弃的 checkpoint ' + d.anchors_dropped.length + ' 个（无 position）');
      if (d.integrity !== 'present') warn.push('manifest 缺 integrity');
      document.getElementById('pkwarn').textContent = warn.length
        ? warn.join('\n') : '无告警';
    })
    .catch(e => { document.getElementById('pkmsg').textContent = String(e); });
}
document.getElementById('pkrefresh').onclick = loadPack;

/* ---------- 派生 ---------- */
function runDerive(kind) {
  const pack = document.getElementById('packsel').value || '';
  document.getElementById('dvmsg').textContent = '跑 ' + kind + '…';
  document.getElementById('dvout').textContent = '';
  fetch('/api/derive', {method: 'POST',
    body: JSON.stringify({kind: kind, pack: pack})})
    .then(r => r.json()).then(function (d) {
      document.getElementById('dvmsg').textContent =
        d.err ? '失败' : ('exit ' + d.code);
      document.getElementById('dvout').textContent = d.err
        ? d.err
        : ((d.summary ? d.summary + '\n\n' : '') +
           (d.stdout || '') + (d.stderr ? '\n[stderr]\n' + d.stderr : ''));
    })
    .catch(e => { document.getElementById('dvmsg').textContent = String(e); });
}
document.getElementById('dvEp').onclick = function () { runDerive('episodes'); };
document.getElementById('dvJoin').onclick = function () { runDerive('join'); };
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    studio = None
    runner = None
    viewer_url = ''

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path.split('?')[0]
        if p in ('/', '/index.html'):
            # 把 NG 的地址注入页面（模板里只留一个占位表达式，不做字符串格式化，
            # 避免 HTML 里的 JS 花括号被 Python 当成占位符）。
            html = HTML.replace(
                "document.getElementById('ng').src = d.viewer_url;",
                "document.getElementById('ng').src = " +
                json.dumps(self.viewer_url) + ";")
            self._send(200, html, 'text/html; charset=utf-8')
            return
        if p == '/api/timeline':
            tl = dict(self.studio.tl)
            tl['viewer_url'] = self.viewer_url
            self._send(200, json.dumps(tl, ensure_ascii=False))
            return
        if p == '/api/pickdir':
            self._send(200, json.dumps({'dir': pick_directory()},
                                       ensure_ascii=False))
            return
        if p == '/api/sessions':
            # 首页的"已有会话"列表：只读 manifest，不扫事件流（30 个包也秒回）
            root = pack_root()
            rows = []
            if root.is_dir():
                dirs = list_pack_dirs(root)
                dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                for d in dirs[:40]:
                    mf = d / 'manifest.json'
                    if not mf.exists():
                        continue
                    try:
                        m = json.loads(mf.read_text('utf-8'))
                    except Exception:
                        continue
                    t = m.get('time') or {}
                    a0, a1 = t.get('start_wall_ms'), t.get('end_wall_ms')
                    dur = round((a1 - a0) / 1000.0, 1) if a0 and a1 else None
                    rows.append({
                        'name': d.name,
                        'path': rel_to_root(d),
                        'session_id': m.get('session_id'),
                        'n_events': (m.get('integrity') or {}).get('n_events'),
                        'n_shots': (m.get('integrity') or {}).get('n_shots'),
                        'duration_s': dur,
                        'when': m.get('created_at_utc') or '',
                        'url': (m.get('page') or {}).get('url') or '',
                        'has_html': (d / '打开.html').exists(),
                        'is_current': bool(self.studio.packdir) and
                                      d.resolve() == Path(self.studio.packdir).resolve(),
                    })
            self._send(200, json.dumps({'root': rel_to_root(root), 'sessions': rows,
                                        'py': _PY_CHECK},
                                       ensure_ascii=False))
            return
        if p == '/api/catalog':
            root = pack_root()
            packs = []
            if root.is_dir():
                dirs = list_pack_dirs(root)
                dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                packs = [rel_to_root(d) for d in dirs[:30]]
            self._send(200, json.dumps(
                {'packs': packs, 'datasets': DATASETS,
                 'root': rel_to_root(pack_root()),
                 'current_pack': str(self.studio.packdir or ''),
                 'current_dataset': self.studio.dataset},
                ensure_ascii=False))
            return
        if p == '/api/run':
            self._send(200, json.dumps(self.runner.status(), ensure_ascii=False))
            return
        if p == '/api/inspect':
            # “看包”：复用 inspect_pack.summarize，不在页面里重写一遍。
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            pack = (q.get('pack') or [None])[0] or str(self.studio.packdir)
            try:
                import inspect_pack
                s = inspect_pack.summarize(pack)
                self._send(200, json.dumps(s, ensure_ascii=False, default=str))
            except Exception as err:
                self._send(200, json.dumps(
                    {'err': '%s: %s' % (type(err).__name__, err)},
                    ensure_ascii=False))
            return
        if p == '/html':
            from urllib.parse import parse_qs
            q = parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            pk = (q.get('pack') or [None])[0]
            f = from_root(pk) / '打开.html' if pk else None
            if f and f.exists():
                self._send(200, f.read_text('utf-8'), 'text/html; charset=utf-8')
                return
            # 还没生成就现场做一份（老包没有这个文件）
            if f:
                try:
                    import cave_session_replay as csr
                    csr.replay_one(f.parent, out_path=f, quiet=True)
                    self._send(200, f.read_text('utf-8'),
                               'text/html; charset=utf-8')
                except Exception as err:
                    self._send(500, ('生成失败：%s' % err).encode(),
                               'text/plain; charset=utf-8')
                return
            self._send(404, b'')
            return
        m = re.match(r'^/shots/([A-Za-z0-9_\-]+)\.png$', p)
        if m:
            f = Path(self.studio.packdir) / 'client' / 'shots' / (m.group(1) + '.png')
            if f.exists():
                self._send(200, f.read_bytes(), 'image/png')
            else:
                self._send(404, b'')
            return
        self._send(404, b'')

    def do_POST(self):
        p = self.path.split('?')[0]
        if p not in ('/api/seek', '/api/run', '/api/load', '/api/open',
                     '/api/derive', '/api/packs/op'):
            self._send(404, b'')
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
            if p == '/api/derive':
                # “派生”：直接跑现成的命令行工具，不另写一套逻辑。
                # episodes（切段）/ join（对 op log），输出原样回传。
                out = self._derive(body)
            elif p == '/api/run':
                out = self.runner.start(body.get('kind') or 'export',
                                        body.get('t') or 0, body)
            elif p == '/api/load':
                pack = body.get('pack')
                out = ({'ok': True, 'session': self.studio.switch_pack(pack)}
                       if pack else {'err': '缺少 pack'})
            elif p == '/api/packs/op':
                out = pack_op(body.get('op'), body.get('path'),
                              body.get('name'))
            elif p == '/api/open':
                out = {}
                pack = body.get('pack')
                if pack:
                    out['session'] = self.studio.switch_pack(pack)
                ds = body.get('dataset')
                if ds:
                    out.update(self.studio.apply_dataset(
                        ds, body.get('em'), body.get('seg')))
                out.setdefault('ok', True)
            else:
                out = self.studio.seek(body.get('t') or 0)
        except Exception as err:
            out = {'err': '%s: %s' % (type(err).__name__, err)}
        self._send(200, json.dumps(out, ensure_ascii=False))

    def _derive(self, body):
        """跑 cave_episodes / cave_join，只回传 stdout 末尾。"""
        kind = body.get('kind') or 'episodes'
        pack = body.get('pack') or str(self.studio.packdir)
        ops = HERE.parent / 'results' / 'cave_watch' / 'ops.jsonl'
        if kind == 'join':
            if not ops.exists():
                return {'kind': kind, 'pack': pack, 'code': 2,
                        'stdout': '',
                        'stderr': '缺少 op log：%s\n先跑 cave_op_watcher.py 生成。'
                                  % rel_to_root(ops)}
            # 和 episodes 一致：派生结果跟着包走，不写到全局 results/joined。
            cmd = [sys.executable, str(HERE / 'cave_join.py'), pack,
                   '--out', str(Path(pack) / 'derived'),
                   '--ops', str(ops)]
        else:
            cmd = [sys.executable, str(HERE / 'cave_episodes.py'), pack]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               timeout=300, cwd=str(HERE.parent),
                               env=_utf8_env())
            out = {'kind': kind, 'pack': pack, 'code': r.returncode,
                   'stdout': (r.stdout or '')[-6000:],
                   'stderr': (r.stderr or '')[-2000:]}
            out['summary'] = (self._derive_summary(kind, pack)
                             if r.returncode == 0 else '')
            return out
        except Exception as err:
            return {'err': '%s: %s' % (type(err).__name__, err)}

    @staticmethod
    def _derive_summary(kind, pack):
        """给工作台一行可读摘要；文件不存在就返回空串。"""
        derived = Path(pack) / 'derived'
        try:
            if kind == 'episodes':
                ep = derived / 'episodes.jsonl'
                if not ep.exists():
                    return ''
                rows = [json.loads(x) for x in
                        ep.read_text(encoding='utf-8').splitlines() if x.strip()]
                n = len(rows)
                dur = sum(r.get('duration_ms') or 0 for r in rows) / 1000.0
                return '派生摘要：%d 个 episode，总时长 %.1f s；输出 %s' % (
                    n, dur, rel_to_root(derived))
            js = sorted(derived.glob('*.join_summary.json'))
            if not js:
                return ''
            s = json.loads(js[-1].read_text(encoding='utf-8'))
            return '关联摘要：%s 个 op 匹配（%s）；输出 %s' % (
                s.get('n_matched', 0),
                ', '.join('%s=%s' % (k, v)
                          for k, v in (s.get('matched') or {}).items()) or '无',
                rel_to_root(js[-1].parent))
        except Exception:
            return ''


REPO_ROOT = HERE.parent


def rel_to_root(p):
    """对外显示 / 前后端传递用的路径：仓库内 → 相对仓库根，仓库外 → 绝对。

    写死绝对路径会让换台机器就跑不了；而相对路径又必须相对**固定**的锚点
    （仓库根），不能相对 cwd —— 用户在哪个目录敲命令不该影响结果。
    """
    if p is None:
        return ''
    p = Path(p)
    try:
        rp = p.resolve()
    except OSError:
        return str(p)
    try:
        return rp.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(rp)


def from_root(p):
    """`rel_to_root()` 的逆操作：相对路径按仓库根解析，绝对路径原样用。"""
    if not p:
        return None
    p = Path(p)
    return p if p.is_absolute() else (REPO_ROOT / p)


def pack_root():
    """会话包根目录：设了 NM_SESSIONS 就用它（和 nx.py / 采集落点一致）。"""
    return Path(os.environ.get('NM_SESSIONS')
                or (HERE.parent / 'results' / 'manual_sessions')).expanduser()


def pick_directory():
    """弹系统原生的"选目录"对话框，返回绝对路径；取消或没有工具时返回 None。

    为什么必须后端做：浏览器拿不到本地目录的绝对路径
    （`<input webkitdirectory>` 只给文件名列表，File System Access API 只给 handle）。
    工作台是本地服务，所以直接调系统的选择框最省事。
    """
    import platform
    # 这三个调用不强制 UTF-8：对面是系统自带的脚本宿主/对话框工具，输出用的是
    # 它自己的控制台编码（不是我们那些 python 子进程），按本机缺省读最稳。
    sysname = platform.system()
    if sysname == 'Darwin':
        try:
            r = subprocess.run(
                ['osascript', '-e', 'activate',
                 '-e', 'POSIX path of (choose folder with prompt "选保存目录")'],
                capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().rstrip('/')
        except Exception:
            return None
        return None
    if sysname == 'Windows':
        # PowerShell 自带 FolderBrowserDialog，不用装任何东西
        ps = ('Add-Type -AssemblyName System.Windows.Forms;'
              '$d = New-Object System.Windows.Forms.FolderBrowserDialog;'
              '$d.Description = "选保存目录";'
              'if ($d.ShowDialog() -eq "OK") { $d.SelectedPath }')
        try:
            r = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                               capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            return None
        return None
    # Linux：zenity / kdialog 随便有一个就行
    for cmd in (['zenity', '--file-selection', '--directory'],
                ['kdialog', '--getexistingdirectory']):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return None


def pack_op(op, path, name=None):
    """会话包改名 / 移到回收站。

    只允许动 `pack_root()` 的直接子目录（防止被当成任意路径操作）；
    删除是真删（shutil.rmtree），只允许动根目录的直接子目录。
    """
    if not path:
        return {'err': '没给包路径'}
    root = pack_root().resolve()
    p = from_root(path).resolve()
    trash_root = (root / '_trash').resolve()
    if p.parent not in (root, trash_root):
        return {'err': '只能操作会话根目录（或 _trash）下的包：%s'
                % rel_to_root(root)}
    if not (p / 'manifest.json').exists():
        return {'err': '不是会话包：%s' % p.name}
    if op == 'rename':
        new = csp._safe_dirname(name or '', '')
        if not new:
            return {'err': '名字不合法（不能为空，也不能含 / \\ : * ? " < > |）'}
        target = p.with_name(new)
        if target.exists():
            return {'err': '同名已存在：%s' % new}
        p.rename(target)
        return {'ok': True, 'path': rel_to_root(target), 'name': new}
    if op == 'delete':
        # v1 起默认软删：移到根目录的 _trash/，可手动恢复；purge 才是真删。
        import time as _time
        trash = root / '_trash'
        trash.mkdir(exist_ok=True)
        target = trash / ('%s__%s' % (p.name, _time.strftime('%Y%m%d_%H%M%S')))
        if target.exists():
            target = trash / ('%s__%s_%s' % (p.name, _time.strftime('%Y%m%d_%H%M%S'),
                                             _time.time_ns() % 100000))
        p.rename(target)
        return {'ok': True, 'trashed': True, 'deleted': False,
                'path': rel_to_root(target), 'name': p.name}
    if op == 'purge':
        import shutil
        shutil.rmtree(p)
        return {'ok': True, 'deleted': True, 'name': p.name}
    return {'err': '未知操作：%s' % op}


def list_pack_dirs(root=None):
    """根目录下的会话目录：跳过隐藏目录与草稿/回收站。"""
    root = Path(root) if root else pack_root()
    if not root.is_dir():
        return []
    return [d for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(('.', '_draft_'))
            and d.name != '_trash']


def latest_pack():
    root = pack_root()
    # "最新包"必须是真的包（有 manifest.json），否则会把 _trash/ 之类算进去
    dirs = [d for d in list_pack_dirs(root) if (d / 'manifest.json').exists()]
    if not dirs:
        raise SystemExit('%s 下没有包' % rel_to_root(root))
    return max(dirs, key=lambda d: d.stat().st_mtime)


def newest_pack_since(ts):
    """采集结束后找新包：目录 mtime 晚于开跑时间（容 5 s 时钟误差）。"""
    root = pack_root()
    if not root.is_dir():
        return None
    cands = [(d.stat().st_mtime, d) for d in list_pack_dirs(root)
             if (d / 'manifest.json').exists()]
    cands = [x for x in cands if x[0] >= ts]
    return str(max(cands, key=lambda x: x[0])[1]) if cands else None


def main(argv=None):
    ap = argparse.ArgumentParser(description='会话包 → 可拖的界面级回放')
    ap.add_argument('pack', nargs='?', help='会话包目录（缺省用最新的）')
    ap.add_argument('--port', type=int, default=8913)
    ap.add_argument('--no-open', action='store_true', help='不自动开浏览器')
    args = ap.parse_args(argv)

    if args.pack:
        packdir = Path(args.pack)
    else:
        try:
            packdir = latest_pack()
        except SystemExit:
            packdir = None      # 一个包都没有：照常起，首页负责采集
    pack = csp.load_session(packdir) if packdir else csp.empty_pack()
    bridge = Bridge()
    url = bridge.start()
    studio = Studio(pack, packdir, bridge)

    Handler.studio = studio
    Handler.runner = Runner(packdir)
    Handler.viewer_url = url
    srv = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    addr = 'http://127.0.0.1:%d/' % srv.server_address[1]
    print('[studio] 包      %s' % (rel_to_root(packdir) if packdir
                                    else '（暂无；在首页点“开始采集”）'))
    print('[studio] 快照    %d 个｜关键帧 %d 张｜事件 %d 条'
          % (len(studio.snaps), len(studio.shots), len(pack.get('events') or [])))
    print('[studio] NG      %s' % url)
    print('[studio] 工作台  %s' % addr)
    print('[python] 解释器 %s（采集子进程用 %s）'
          % (sys.executable, _child_py()), flush=True)
    # 后台问一次“这个解释器能不能开采集窗口”，不成则只提醒，不影响其它功能
    threading.Thread(target=_check_py, daemon=True).start()
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(addr)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if bridge.proc:
            bridge.proc.terminate()


if __name__ == '__main__':
    main()
