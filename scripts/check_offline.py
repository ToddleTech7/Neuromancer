#!/usr/bin/env python3
"""离线一键自检（跨平台；不需要外网、不需要 GPU，默认不开浏览器）。

包含：单测 + 文档自检 + join + 提交钩子 + 图层字段 + 界面/扩展口；--browser 加回放复位。

    python3 scripts/check_offline.py            # 单测 + join + 钩子 + 图层 + 界面
    python3 scripts/check_offline.py --quick    # 只跑单测
    python3 scripts/check_offline.py --browser  # 额外跑回放复位（无头浏览器，不连公网）

退出码：0 = 全过；非 0 = 有用例失败（CI / pre-commit 可直接用）。

和 `check_offline.sh` 等价，区别是不依赖 bash、不写 `/tmp`（Windows 没有 `/tmp`）。
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable

# 中文 Windows 常注入 PYTHONIOENCODING=cp936：父端按 UTF-8 读、子端按 GBK 写会解不开
# （捕获变 None）。这里统一强制 UTF-8，子/孙进程继承后两端一致。
os.environ['PYTHONUTF8'] = '1'
os.environ['PYTHONIOENCODING'] = 'utf-8'


def step(name, argv):
    """跑一步，只回传末尾几行（失败时多给一些）。"""
    print('\n=== %s ===' % name, flush=True)
    r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    lines = [x for x in out.strip().splitlines() if x.strip()]
    if r.returncode == 0:
        for line in lines[-2:]:
            print(line)
        print('[OK] %s' % name)
        return True
    for line in lines[-30:]:
        print(line)
    print('[FAIL] %s' % name)
    return False


def preflight():
    """先确认测试依赖；缺了给一条可执行的安装命令，不吐长 traceback。"""
    missing = []
    for name in ('pytest', 'jsonschema', 'pandas', 'caveclient',
                 'neuroglancer', 'playwright'):
        try:
            __import__(name)
        except Exception:
            missing.append(name)
    if missing:
        print('[需安装] %s 缺少：%s' % (PY, ', '.join(missing)), file=sys.stderr)
        print('  %s -m pip install -r requirements-dev.txt' % PY,
              file=sys.stderr)
        return False
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description='离线一键自检（跨平台）')
    ap.add_argument('--quick', action='store_true', help='只跑单测')
    ap.add_argument('--browser', action='store_true',
                    help='额外跑回放复位（无头浏览器，不连公网）')
    args = ap.parse_args(argv)
    if not preflight():
        return 2

    steps = [('单元测试（scripts/test_*.py）',
              [PY, '-m', 'pytest', 'scripts/', '-q']),
             ('文档自检（死链/必需文档/旧文件名）',
              [PY, str(HERE / 'check_docs.py')])]
    if not args.quick:
        steps += [
            ('join 夹具（24 项，0 假匹配）',
             [PY, str(HERE / 'verify_cave_join.py')]),
            ('提交钩子装置（14 项，本地假网关）',
             [PY, str(HERE / 'verify_cave_recorder_submit_hooks.py')]),
            # 只用 about:blank + 构造图层对象，不连任何站点
            ('图层字段（17 项，source 三形态 + 样式）',
             [PY, str(HERE / 'verify_layer_fields.py')]),
            # 本地 demo NG + 假能力端点；不连外网
            ('界面状态 + 扩展口 + 私有记录/提交头（19 项）',
             [PY, str(HERE / 'verify_ui_and_providers.py')]),
        ]
    if args.browser:
        steps.append(('回放复位（脏→干净，无头浏览器）',
                      [PY, str(HERE / 'verify_replay_reset.py')]))

    failed = [name for name, cmd in steps if not step(name, cmd)]
    if failed:
        print('\n离线自检：FAIL（上面有明细）')
        return 1
    print('\n离线自检：PASS')
    print('下一步（需外网）：'
          'python3 scripts/verify_cave_recorder_e2e.py && '
          'python3 scripts/demo_capture_episodes.py && '
          'python3 scripts/verify_action_replay.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
