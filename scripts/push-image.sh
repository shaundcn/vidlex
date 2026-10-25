#!/bin/sh
# 构建当前提交的镜像并推到 Docker Hub：标签 v版本号 和 latest。先 docker login。
#
#   scripts/push-image.sh
set -eu
cd "$(dirname "$0")/.."

HUB=shaundcn/vidlex
TAG=v$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml)

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有没提交的改动，先提交再推送"
  exit 1
fi

echo "== 构建 $HUB:$TAG"
docker build -q -t "vidlex/vidlex:$TAG" -t "$HUB:$TAG" -t "$HUB:latest" .
docker push -q "$HUB:$TAG"
docker push -q "$HUB:latest"
echo "推送完成：$HUB:$TAG"
