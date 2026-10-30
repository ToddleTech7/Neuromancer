#!/usr/bin/env python3
"""nx doctor：发布前 / 首次运行的环境自检。

检查的是“这台机器现在能不能跑采集、回放、派生”，不启动浏览器、不写会话数据。
退出码：0 = 必需项全过（可选依赖缺失只提示）；1 = 有必需项失败。

    python3 scripts/nx_doctor.py
    python3 scripts/nx_doctor.py --json
"""

import argparse
import importlib.metadata as md
import json
import os
import shutil
import socket
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

SESSIONS = Path(os.environ.get('NM_SESSIONS')
                or (ROOT / 'results' / 'manual_sessions')).expanduser()
REQUIRED_PKGS = ('neuroglancer', 'playwright')
OPTIONAL_PKGS = ('caveclient', 'numpy', 'Pillow', 'jsonschema')


def _pkg(name):
    try:
        return md.version(name)
    except Exception:
        return None


def _port_free(port):
    for host in ('127.0.0.1',):
        s = socket.socket()
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False
        finally:
            s.close()
    return False


def _browser():
    """只取 Playwright 的 chromium 路径，不启动浏览器。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            p = pw.chromium.executable_path
            return p, bool(p and Path(p).exists())
    except Exception as err:
        return None, False


def run_checks(with_browser=True):
    checks = []

    def add(name, ok, detail='', required=True):
        checks.append({'name': name, 'ok': bool(ok),
                       'detail': str(detail)[:300],
                       'required': bool(required)})

    add('Python >= 3.10', sys.version_info >= (3, 10),
        sys.version.split()[0])
    add('项目根目录', (ROOT / 'scripts' / 'nx.py').exists(), str(ROOT))
    for name in REQUIRED_PKGS:
        v = _pkg(name)
        add('依赖 %s' % name, bool(v), v or '未安装（必需）')
    for name in OPTIONAL_PKGS:
        v = _pkg(name)
        add('可选依赖 %s' % name, bool(v), v or '未安装（仅部分功能需要）',
            required=False)

    add('会话目录可写', SESSIONS.parent.is_dir()
        and os.access(str(SESSIONS.parent), os.W_OK),
        str(SESSIONS))
    try:
        free = shutil.disk_usage(str(SESSIONS.parent if SESSIONS.parent.exists()
                                    else ROOT)).free
        add('磁盘剩余 >= 2 GB', free >= 2 * 1024 ** 3,
            '%.1f GB' % (free / 1024 ** 3))
    except OSError as err:
        add('磁盘剩余', False, err)

    if with_browser:
        path, ok = _browser()
        add('Playwright Chromium', ok, path or '未安装（python -m playwright install chromium）')

    for port in (8899, 8913):
        add('端口 %d 空闲（被占用时会自动换）' % port, _port_free(port),
            '占用中也可用，服务会选 89xx/891x', required=False)

    try:
        import inspect_pack
        p = inspect_pack.latest_pack()
        add('最新会话包可读', True, str(p) if p else '还没有会话包（可选）',
            required=False)
    except Exception as err:
        add('最新会话包可读', False, '%s: %s' % (type(err).__name__, err),
            required=False)

    return checks


def main(argv=None):
    ap = argparse.ArgumentParser(description='Neuromancer 环境自检')
    ap.add_argument('--json', action='store_true', help='机器可读输出')
    ap.add_argument('--no-browser', action='store_true',
                    help='跳过 Playwright 浏览器检查')
    args = ap.parse_args(argv)
    checks = run_checks(with_browser=not args.no_browser)
    bad = [c for c in checks if c['required'] and not c['ok']]
    if args.json:
        print(json.dumps({'ok': not bad, 'checks': checks,
                          'errors': [c['name'] for c in bad]},
                         ensure_ascii=False, indent=2))
    else:
        print('Neuromancer doctor')
        for c in checks:
            tag = 'PASS' if c['ok'] else ('WARN' if not c['required'] else 'FAIL')
            print('  [%s] %-34s %s' % (tag, c['name'], c['detail']))
        print('\nRESULT:', 'PASS' if not bad else 'FAIL')
        if bad:
            print('缺：' + ', '.join(c['name'] for c in bad))
    return 0 if not bad else 1


if __name__ == '__main__':
    raise SystemExit(main())
