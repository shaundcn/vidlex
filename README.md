# vidlex

把 NAS 上的短视频和短视频教程变成可读、可搜的 md 笔记。产品规则见 `立项文档v3.md`，技术路线见 `技术选型v1.md`。

> 版本号见 `pyproject.toml`；已在飞牛 NAS 上验收（进度和待办见 `设计阶段交接文档.md`）。

## 部署

1. 把 `compose.yaml` 复制到 NAS 上的一个文件夹，把里面的两个路径改成 NAS 上的实际文件夹：
   - `/视频文件夹`：软件在里面自动建 `input` 和 `output`；
   - `/数据文件夹`：设置、任务记录和日志。**必须在 NAS 本地磁盘上，不能是网络共享**（SQLite 在网络共享上可能损坏）。
2. 在该目录执行 `docker compose up -d`，浏览器打开 `http://NAS的IP:8462`。
3. 升级：`docker compose pull && docker compose up -d`。

NAS 的 Docker 界面不支持 compose 时，用下面这条和 `compose.yaml` 等价的命令（同样的自动重启、健康检查和日志滚动）：

```sh
docker run -d --name vidlex --restart unless-stopped -p 8462:8462 \
  -v /视频文件夹:/library -v /数据文件夹:/data \
  --health-cmd "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8462/health', timeout=5)\"" \
  --health-interval 30s --health-timeout 10s --health-retries 3 --health-start-period 30s \
  --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
  shaundcn/vidlex:latest
```

只映射一个视频库文件夹，不要分别映射 input 和 output：分开映射后，剪切会变成“复制+删除”，大文件慢、临时占双份空间。

端口默认 8462。要换成别的端口（比如和其他程序冲突），在 `compose.yaml` 旁边建一个 `.env` 文件写 `VIDLEX_PORT=9000`，或直接运行 `VIDLEX_PORT=9000 docker compose up -d`，然后用 `http://NAS的IP:9000` 访问；用 `docker run` 时把 `-p 8462:8462` 改成 `-p 9000:8462`。容器里的端口始终是 8462，只改冒号左边。用 IP、域名或反向代理访问都可以，程序不做访问限制。

**安全：** 界面没有密码，也不做任何访问限制，能访问到这个端口的人都能操作、消耗你的 API 额度。不要把它暴露到外网；需要外网访问时，在反向代理上加密码。

## 固定版本与回滚

compose 默认用 `latest`。想固定版本，把 `image` 改成 `shaundcn/vidlex:vX.Y.Z`；回滚时改成上一个版本号再 `docker compose up -d`。

如果新版本升级过数据库结构，旧版本会拒绝启动（提示“数据库版本比程序新”），需要先用 NAS 的备份恢复 data 文件夹，再换回旧镜像。

## 备份

vidlex 不自带备份。请把 data 文件夹纳入 NAS 的快照或备份任务。用复制文件的方式备份时，先停止容器（`docker compose stop`），避免复制到写了一半的数据库。

md 和视频都在视频库里，不依赖数据库。数据库丢失只会丢设置和任务记录：设置可以重填，input 里的视频会被重新扫描；找不回的只有已完成任务的列表和“重新处理”能力。

## 数据库损坏

启动时如果数据库文件损坏，页面会显示“数据库文件损坏”，软件不移动任何文件、不自动重建。两种处理方式：

1. 停止容器，用 NAS 的备份恢复 data 文件夹，再启动；
2. 停止容器，删除 data 里的 `vidlex.db`、`vidlex.db-wal`、`vidlex.db-shm`，再启动，程序会新建数据库。设置要重新填写，已完成任务的列表会丢失，md 和视频不受影响。

## FFmpeg 许可证

镜像中的 FFmpeg 来自 Debian 的 `ffmpeg` 包，以 GPL 等许可证发布，许可证全文在镜像的 `/usr/share/doc/ffmpeg/copyright`。源码可从 Debian 获取：`apt-get source ffmpeg`，或 <https://packages.debian.org/source/trixie/ffmpeg>。

## 开发

需要 [uv](https://docs.astral.sh/uv/)、FFmpeg、Docker，前端测试另需 Node 22 以上。

```sh
uv sync                                   # 安装依赖（按 uv.lock）
scripts/check.sh                          # 全部检查：后端测试、前端测试、JS 语法和导入（不含真实方舟测试）
uv run pytest -q                          # 只跑后端测试
uv run --env-file .env pytest -m real -q  # 真实方舟测试：会产生少量费用，需要在 .env 里填 Key
cp .env.example .env                      # 填写开发用的 data 和视频库路径（绝对路径）
cd backend && uv run --env-file ../.env python -m app.main   # 启动，端口 8462
docker build -t vidlex/vidlex:latest .    # 构建镜像
scripts/push-image.sh                     # 构建并推到 Docker Hub（shaundcn/vidlex:v版本号 和 latest），推送前先 docker login
scripts/deploy.sh 用户@NAS地址            # 部署到 NAS（第一次部署加参数：视频库路径 data路径 [端口]）：先备份 data、保留旧容器，结束时打印回滚命令（技术选型第 6 节）
```

依赖只用发布满两周的版本：`pyproject.toml` 的 `exclude-newer` 写着截止日期，升级依赖时把它改成当天往前 14 天，再执行 `uv lock --upgrade`。
