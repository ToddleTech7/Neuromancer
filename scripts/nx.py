#!/usr/bin/env python3
"""nx：Neuromancer 的统一入口（一条命令覆盖采集/回放/工作台/看包/导出）。

以前要记四条命令和它们的参数，散在 `demo_manual.sh`、`replay_manual_session.py`、
`inspect_pack.py`、`cave_replay_studio.py` 里。这个文件只做分派，不实现逻辑：

    python3 scripts/nx.py                 # 默认：打开回放工作台（最新包）
    python3 scripts/nx.py studio [包]      # 同上，可指定包
    python3 scripts/nx.py collect          # 打开采集窗口（原来 --open）
    python3 scripts/nx.py replay [--seek-t 140] [--video] [--headed]
    python3 scripts/nx.py pack [包]        # 看包里有什么（只读）
    python3 scripts/nx.py verify-pack [包] # 校验 integrity 与 PNG 哈希
    python3 scripts/nx.py doctor          # 环境自检（依赖/浏览器/目录/端口）
    python3 scripts/nx.py export-csv [包]  # 导出 events/episodes/hover CSV
    python3 scripts/nx.py export [--seek-t 140]   # 导出一段视频
    python3 scripts/nx.py recover          # 从崩溃草稿恢复成正式包

后面多出来的参数原样透传给下面那个脚本，所以旧用法都还能用，
例如 `nx.py replay --pack <包> --start manual_2 --end manual_3 --mesh-wait 30`。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
VERSION = (HERE.parent / 'VERSION').read_text('utf-8').strip() if (HERE.parent / 'VERSION').exists() else 'dev'
sys.path.insert(0, str(HERE))
from cave_session_pack import relpath          # noqa: E402
# 会话包根目录。设了 NM_SESSIONS 就用它（采集和"找最新包"都跟着走），
# 否则是仓库内的 results/manual_sessions。
PKG_ROOT = Path(os.environ.get('NM_SESSIONS')
                or (HERE.parent / 'results' / 'manual_sessions')).expanduser()


def latest_pack():
    if not PKG_ROOT.is_dir():
        return None
    # 只有正式包（有 manifest）才是“最新会话”；_draft_*/_trash 不是包。
    dirs = [d for d in PKG_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(('.', '_'))
            and (d / 'manifest.json').exists()]
    return str(max(dirs, key=lambda d: d.stat().st_mtime)) if dirs else None


# 这些选项后面跟一个值；值不能被误当成“位置参数（包路径）”。
_VALUE_OPTS = {'--pack', '--start', '--end', '--seek-t', '--out',
               '--accept-ncc', '--mesh-wait', '--anchor', '--port',
               '--em', '--seg', '--pos', '--dims', '--session-name',
               '--url', '--profile', '--cdp-port'}


def _has_positional(args):
    i = 0
    while i < len(args):
        a = args[i]
        if a in _VALUE_OPTS:
            i += 2
            continue
        if a.startswith('-'):
            i += 1
            continue
        return True
    return False


def with_pack(args):
    """没给 --pack、也没给位置包路径时，补上最新的包。"""
    if '--pack' in args:
        return args
    if _has_positional(args):
        return args
    p = latest_pack()
    if not p:
        raise SystemExit('没找到会话包：%s 是空的' % relpath(PKG_ROOT))
    return ['--pack', p] + args


def build(ns):
    a = list(ns.args)
    if ns.cmd == 'doctor':
        return [PY, str(HERE / 'nx_doctor.py')] + a
    if ns.cmd == 'export-csv':
        return [PY, str(HERE / 'export_csv.py')] + with_pack(a)
    if ns.cmd == 'studio':
        # 不给包时 studio 自己找最新（它也认 NM_SESSIONS）
        return [PY, str(HERE / 'cave_replay_studio.py')] + a
    if ns.cmd == 'collect':
        # 设了 NM_SESSIONS 就让采集和"找最新"落到同一个地方
        if os.environ.get('NM_SESSIONS') and '--out' not in a:
            a = ['--out', str(PKG_ROOT)] + a
        # 直接调 python，不经过 demo_manual.sh —— 那个是 bash，Windows 没有
        return [PY, str(HERE / 'cave_manual_server.py'), '--open'] + a
    if ns.cmd == 'replay':
        return [PY, str(HERE / 'replay_manual_session.py')] + with_pack(a)
    if ns.cmd == 'export':
        # 导出 = 逐条重放 + 录屏；--video 由这里补，其余参数透传
        return [PY, str(HERE / 'replay_manual_session.py'), '--video'] + with_pack(a)
    if ns.cmd in ('pack', 'verify-pack'):
        if not a:
            p = latest_pack()
            if not p:
                raise SystemExit('没找到会话包：%s 是空的' % relpath(PKG_ROOT))
            a = [p]
        extra = ['--verify'] if ns.cmd == 'verify-pack' else []
        return [PY, str(HERE / 'inspect_pack.py')] + extra + a
    if ns.cmd == 'recover':
        # 和采集/找最新包用同一个根目录，避免 NM_SESSIONS 下恢复不到草稿
        if os.environ.get('NM_SESSIONS') and '--out' not in a:
            a = ['--out', str(PKG_ROOT)] + a
        return [PY, str(HERE / 'recover_draft.py')] + a
    raise SystemExit('未知命令 %r' % ns.cmd)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='nx', description='Neuromancer 统一入口（默认打开回放工作台）',
        epilog='例子：nx collect / nx replay --seek-t 140 / nx doctor')
    ap.add_argument('--version', action='version',
                    version='Neuromancer %s' % VERSION)
    ap.add_argument('cmd', nargs='?', default='studio',
                    choices=['studio', 'collect', 'replay', 'export', 'pack',
                             'verify-pack', 'doctor', 'export-csv', 'recover'])
    ap.add_argument('args', nargs=argparse.REMAINDER,
                    help='透传给下层脚本的参数')
    ns = ap.parse_args(argv)
    cmd = build(ns)
    # 路径只显示相对仓库根的部分，换台机器也不会看到别人的 home
    print('[nx] ' + ' '.join(cmd).replace(str(HERE.parent), ''), flush=True)
    try:
        raise SystemExit(subprocess.call(cmd))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == '__main__':
    main()
