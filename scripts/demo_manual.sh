#!/usr/bin/env bash
# 公开数据手动 demo —— 一条命令，不用填任何参数。
#
#   采集：bash scripts/demo_manual.sh
#         然后按终端打印的三步做（装一次脚本 → 打开页面 → 操作并保存）
#   回放：bash scripts/demo_manual.sh --replay
#
# 固定不变的东西都在这里，不要在命令行里改：
#   数据  minnie65 公开 EM + 分割（BOSS）
#   体素  (4, 4, 40) nm   位置 (190587, 114102, 21773)   缩放 6
#   保存端口 8899
# 要换数据源时改 cave_manual_server.py 的默认值（或直接传 --em/--seg）。
set -euo pipefail
cd "$(dirname "$0")/.."          # → 仓库根的 neuromancer/

# 解释器查找顺序（本地/服务器通用）：$PYTHON → 项目内 .venv → 上一级旧 venv → python3
# 判据是"能 import 到依赖"，不是"文件可执行"：空 venv 可执行但没有包，不能选它。
# $PYTHON 允许写裸名字（python3），用 command -v 解析成绝对路径。
PY=""
CANDIDATES="${PYTHON:-}"
for cand in ".venv/bin/python" "venv/bin/python" \
            "../.venv-cavecopilot/bin/python" "../.venv-qwen35/bin/python" python3; do
  CANDIDATES="$CANDIDATES $cand"
done
for cand in $CANDIDATES; do
  [ -n "$cand" ] || continue
  case "$cand" in
    */*) exe="$cand" ;;
    *)   exe="$(command -v "$cand" 2>/dev/null || true)" ;;
  esac
  [ -n "$exe" ] && [ -x "$exe" ] || continue
  if "$exe" -c 'import neuroglancer, playwright' >/dev/null 2>&1; then
    PY="$exe"; break
  fi
done

if [ -z "$PY" ]; then
  echo "[错误] 没找到同时有 neuroglancer + playwright 的解释器（试过：$CANDIDATES）。先跑：" >&2
  echo "  python3 -m pip install neuroglancer playwright && python3 -m playwright install chromium" >&2
  exit 1
fi
echo "[python] $PY"

exec "$PY" scripts/cave_manual_server.py --port 8899 "$@"
