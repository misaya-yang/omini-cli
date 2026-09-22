#!/bin/zsh
set -e
STUDIO_ROOT="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$STUDIO_ROOT"
if [[ ! -x "$STUDIO_ROOT/.venv/bin/omni-vlog" ]]; then
  echo '请先按照 README 安装项目依赖，然后重新双击启动。'
  read -k 1 '?按任意键退出'
  exit 1
fi
exec "$STUDIO_ROOT/.venv/bin/omni-vlog" studio
