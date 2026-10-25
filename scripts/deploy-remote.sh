#!/bin/sh
# 在 NAS 上以 root 执行，由 deploy.sh 传过去调用：装入镜像 → 备份 data → 旧容器改名保留 → 启动新容器 → 检查。
# 参数：新标签 [视频库路径 data路径 [端口]]（只有第一次部署、还没有 vidlex 容器时才需要路径；端口默认 8462）
set -eu
TAG=$1
NEW_LIBRARY=${2:-}
NEW_DATA=${3:-}
PORT=${4:-8462}
NAME=vidlex
IMAGE=vidlex/vidlex
# 容器里固定 8462，宿主机端口可换；和 README 的 docker run 一致；单引号留给 python，外层双引号由 sh 去掉
HEALTH_CMD="python -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8462/health', timeout=5)\""

fail() {
  echo "部署中止：$1"
  exit 1
}

mount_of() {
  docker inspect "$NAME" --format "{{range .Mounts}}{{if eq .Destination \"$1\"}}{{.Source}}{{end}}{{end}}"
}

echo "== 装入镜像"
docker load -q -i "/tmp/vidlex-deploy-$TAG.tar.gz"
rm -f "/tmp/vidlex-deploy-$TAG.tar.gz" "$0"

OLD_TAG=
BACKUP=
set --  # 参数已存好；之后用来放旧容器的 VIDLEX_ 环境变量（值里不能有空格）
if docker inspect "$NAME" >/dev/null 2>&1; then
  OLD_TAG=$(docker image inspect "$(docker inspect "$NAME" --format '{{.Image}}')" \
    --format '{{range .RepoTags}}{{println .}}{{end}}' | sed -n "s#^$IMAGE:\(v[0-9][0-9.]*\)\$#\1#p" | head -n 1)
  [ -n "$OLD_TAG" ] || fail "认不出正在运行的容器是哪个版本（镜像没有 v 开头的标签）"
  [ "$OLD_TAG" != "$TAG" ] || fail "正在运行的已经是 $TAG"
  ! docker inspect "$NAME-$OLD_TAG" >/dev/null 2>&1 || fail "已有容器 $NAME-$OLD_TAG，先确认它是否还要保留"
  LIBRARY=$(mount_of /library)
  DATA=$(mount_of /data)
  [ -n "$LIBRARY" ] && [ -n "$DATA" ] || fail "读不出旧容器的视频库或 data 路径"
  PORT=$(docker inspect "$NAME" --format '{{range $p, $b := .HostConfig.PortBindings}}{{if eq $p "8462/tcp"}}{{(index $b 0).HostPort}}{{end}}{{end}}')
  [ -n "$PORT" ] || fail "读不出旧容器的端口"
  URL=http://127.0.0.1:$PORT
  if curl -s "$URL/api/tasks" | grep -q '"status":"processing"'; then
    fail "有任务正在处理，等它完成或点“暂停任务”等它做完后再部署"
  fi
  for kv in $(docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^VIDLEX_' || true); do
    set -- "$@" -e "$kv"
  done
  echo "== 停止 $OLD_TAG，备份 data"
  docker stop "$NAME" >/dev/null
  BACKUP="$DATA-backup-$OLD_TAG-$(date +%Y%m%d-%H%M%S)"
  cp -a "$DATA" "$BACKUP"
  docker rename "$NAME" "$NAME-$OLD_TAG"
else
  LIBRARY=$NEW_LIBRARY
  DATA=$NEW_DATA
  [ -n "$LIBRARY" ] && [ -n "$DATA" ] || fail "第一次部署要给出视频库路径和 data 路径"
  URL=http://127.0.0.1:$PORT
fi

echo "== 启动 $TAG"
docker tag "$IMAGE:$TAG" "$IMAGE:latest"
docker run -d --name "$NAME" --restart unless-stopped -p "$PORT:8462" \
  -v "$LIBRARY:/library" -v "$DATA:/data" "$@" \
  --health-cmd "$HEALTH_CMD" --health-interval 30s --health-timeout 10s --health-retries 3 --health-start-period 30s \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  "$IMAGE:latest" >/dev/null

rollback() {
  [ -n "$OLD_TAG" ] || return 0
  echo
  echo "回滚到 $OLD_TAG（data 恢复到部署前的备份；数据库结构升级过时必须恢复）："
  echo "  sudo docker rm -f $NAME"
  echo "  sudo rm -rf '$DATA' && sudo cp -a '$BACKUP' '$DATA'"
  echo "  sudo docker rename $NAME-$OLD_TAG $NAME && sudo docker start $NAME"
  echo "  sudo docker tag $IMAGE:$OLD_TAG $IMAGE:latest"
}

# 第一次健康检查在启动后 30 秒内；最多等 3 分钟
STATUS=starting
for _ in $(seq 36); do
  STATUS=$(docker inspect "$NAME" --format '{{.State.Health.Status}}')
  [ "$STATUS" = starting ] || break
  sleep 5
done
if [ "$STATUS" != healthy ]; then
  docker logs --tail 30 "$NAME" 2>&1
  rollback
  fail "健康检查是 $STATUS"
fi

echo "== 检查"
echo "健康检查：$(curl -s "$URL/health")"
SELFTEST=$(curl -s -X POST --max-time 120 "$URL/api/settings/test")
echo "自检（会调用一次方舟）：$SELFTEST"
case $SELFTEST in
  *'"ok":true'*) ;;
  *) echo "注意：自检没通过。看上面的原因，判断要不要回滚" ;;
esac
echo "任务数量：$(curl -s "$URL/api/summary")"
[ -z "$BACKUP" ] || echo "部署前的 data 备份：$BACKUP；旧容器保留为 $NAME-$OLD_TAG"
rollback
echo "部署完成：$TAG"
