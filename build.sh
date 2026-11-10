#!/bin/bash
# 网络UPS · TOS 7 打包入口（实际逻辑在 build.py，这里只是方便习惯用 shell 的人）
set -e
cd "$(dirname "$0")"
exec python3 build.py "$@"
