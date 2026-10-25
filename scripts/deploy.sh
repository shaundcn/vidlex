#!/bin/sh
# 部署到 NAS（技术选型第 6 节）：检查 → 构建镜像 → 传到 NAS → 在 NAS 上换容器并检查。
#
#   scripts/deploy.sh 用户@主机                     # 升级：路径、环境变量从正在运行的旧容器读
#   scripts/deploy.sh 用户@主机 视频库路径 data路径 [端口]   # 第一次部署；端口默认 8462，升级时沿用旧容器的端口
#
# 脚本里不写密码：SSH 用你现有的登录方式（密钥或输入密码；也可设 SSH="sshpass -e ssh" SCP="sshpass -e scp"）；
# NAS 上执行 docker 要 sudo，密码从环境变量 VIDLEX_SUDO_PASS 读，没有就在这里提示输入（不显示）。
# 版本号只看 pyproject.toml；镜像标签是 v版本号。
set -eu
cd "$(dirname "$0")/.."

[ $# -eq 1 ] || [ $# -eq 3 ] || [ $# -eq 4 ] || { sed -n '4,5p' "$0"; exit 2; }
TARGET=$1
SSH=${SSH:-ssh}
SCP=${SCP:-scp}
TAG=v$(sed -n 's/^version = "\([^"]*\)".*/\1/p' pyproject.toml)

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有没提交的改动，先提交再部署"
  exit 1
fi
scripts/check.sh

echo "== 构建 vidlex/vidlex:$TAG"
docker build -q -t "vidlex/vidlex:$TAG" .
# 开发机的 /tmp 是内存盘，镜像包放 /var/tmp
TMP=$(mktemp -d "${TMPDIR:-/var/tmp}/vidlex-deploy.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
docker save "vidlex/vidlex:$TAG" | gzip -1 > "$TMP/vidlex-deploy-$TAG.tar.gz"
cp scripts/deploy-remote.sh "$TMP/vidlex-deploy-remote.sh"

echo "== 传到 $TARGET"
$SCP -q "$TMP/vidlex-deploy-$TAG.tar.gz" "$TMP/vidlex-deploy-remote.sh" "$TARGET:/tmp/"

if [ -z "${VIDLEX_SUDO_PASS:-}" ]; then
  printf 'NAS 的 sudo 密码：'
  stty -echo
  read -r VIDLEX_SUDO_PASS
  stty echo
  echo
fi
shift
# sudo 从标准输入的第一行读密码；路径和端口参数只在第一次部署时有
printf '%s\n' "$VIDLEX_SUDO_PASS" | $SSH "$TARGET" "sudo -S -p '' sh /tmp/vidlex-deploy-remote.sh $TAG $*"
