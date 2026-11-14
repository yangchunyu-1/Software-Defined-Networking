#!/usr/bin/env bash
# S2 链路时延 · 离线合成演示（不连 ONOS，纯标准库）
# 用法：cd brain && bash samples/demo_delay.sh
# 输出带 [OFFLINE/SYNTHETIC] 标注，不能作为时延精度证据。
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v python >/dev/null 2>&1; then
    PY=python
else
    PY=python3
fi

exec "$PY" -m delay --offline --demo
