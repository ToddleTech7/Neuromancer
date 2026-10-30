#!/usr/bin/env bash
# 离线一键自检（不需要外网、不需要 GPU；默认不开浏览器）。
# 用途：改完代码先跑这一条；全绿再跑需要外网的 E2E / demo。
#
#   bash scripts/check_offline.sh            # 全跑（单测 + join + 钩子）
#   bash scripts/check_offline.sh --quick    # 只跑单测（1~2 秒）
#   bash scripts/check_offline.sh --browser  # 额外跑回放复位自检（无头浏览器，无公网）
#
# 退出码：0 = 全过；非 0 = 有用例失败（CI / pre-commit 可直接用）。
set -u
cd "$(dirname "$0")/.." || exit 2

PY=${PY:-../.venv-cavecopilot/bin/python}
if [ ! -x "$PY" ]; then PY=$(command -v python3); fi
for _m in pytest jsonschema pandas caveclient neuroglancer playwright; do
  if ! "$PY" -c "import $_m" >/dev/null 2>&1; then
    echo "[需安装] $PY 缺少 $_m" >&2
    echo "  $PY -m pip install -r requirements-dev.txt" >&2
    exit 2
  fi
done
fail=0
QUICK=0
BROWSER=0
for a in "$@"; do
  case "$a" in
    --quick) QUICK=1 ;;
    --browser) BROWSER=1 ;;
  esac
done

run() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@" >/tmp/check_offline_$$.log 2>&1; then
    tail -2 /tmp/check_offline_$$.log
    printf '[OK] %s\n' "$name"
  else
    tail -30 /tmp/check_offline_$$.log
    printf '[FAIL] %s\n' "$name"
    fail=1
  fi
}

run "单元测试（scripts/test_*.py）" "$PY" -m pytest scripts/ -q
run "文档自检（死链/必需文档/旧文件名）" "$PY" scripts/check_docs.py

if [ "$QUICK" -eq 0 ]; then
  run "join 夹具（24 项，0 假匹配）" "$PY" scripts/verify_cave_join.py
  run "提交钩子装置（14 项，本地假网关）" "$PY" scripts/verify_cave_recorder_submit_hooks.py
  # 只用 about:blank + 构造图层对象，不连任何站点
  run "图层字段（17 项，source 三形态 + 样式）" "$PY" scripts/verify_layer_fields.py
  # 本地 demo NG + 假能力端点；不连外网
  run "界面状态 + 扩展口 + 私有记录/提交头（19 项）" "$PY" scripts/verify_ui_and_providers.py
fi

if [ "$BROWSER" -eq 1 ]; then
  run "回放复位（脏→干净，无头浏览器）" "$PY" scripts/verify_replay_reset.py
fi

rm -f /tmp/check_offline_$$.log
if [ "$fail" -eq 0 ]; then
  printf '\n离线自检：PASS\n'
  printf '下一步（需外网）：python3 scripts/verify_cave_recorder_e2e.py && python3 scripts/demo_capture_episodes.py && python3 scripts/verify_action_replay.py\n'
else
  printf '\n离线自检：FAIL（上面有明细）\n'
fi
exit "$fail"
