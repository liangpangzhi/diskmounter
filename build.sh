#!/bin/bash
# 磁盘挂载管家 fpk 打包脚本 —— 在 NAS 构建目录内运行 (fnpack 仅在 fnOS 上可用)
# 约定: 产出的 fpk 文件名始终带版本号 -> diskmounter_<version>.fpk
set -e
cd "$(dirname "$0")"

VER=$(grep -E '^[[:space:]]*version[[:space:]]*=' manifest | head -1 | cut -d= -f2 | tr -d '[:space:]')
if [ -z "$VER" ]; then echo "无法从 manifest 读取 version"; exit 1; fi

echo "== 清理构建缓存 (__pycache__/*.pyc) =="
find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

echo "== fnpack build (version=$VER) =="
fnpack build

OUT="diskmounter_${VER}.fpk"
cp -f diskmounter.fpk "$OUT"
echo "== 打包完成: $(pwd)/$OUT =="
ls -la "$OUT"
