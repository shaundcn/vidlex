"""开发用：给前端人工验证造一批任务（不是测试，不进镜像）。

在容器停止时，用同样的两个卷跑一次：
    docker run --rm -v <视频库>:/library -v <data>:/data -v $PWD/tests:/seed:ro vidlex/vidlex:latest \\
        python /seed/ui_seed.py

6 种状态里除“处理中”外都有（处理中要真的在跑）；共 260 条，用来看“显示更多”和拼音排序。
每个任务都有真实文件，扫描器不会把它们当成“视频已删除”。队列设为已暂停，worker 不会去处理它们。
"""

import json
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app/backend")

from app.config import load_config
from app.db import connection, repo
from app.domain import naming
from app.domain.mdrender import render_md
from app.domain.prompt import DEFAULT_PROMPTS
from app.domain.result import parse_result
from app.services.fsops import Library
from app.services.selftest import ASSET

WORDS = ["安装", "北京烤鸭", "长城", "大理", "鄂尔多斯", "佛山", "广州早茶", "杭州西湖", "济南", "昆明",
         "拉萨", "绵阳", "南京", "盘锦", "青岛啤酒", "瑞丽", "深圳", "天津", "乌鲁木齐", "厦门", "延吉", "珠海",
         "Photoshop", "iPhone 拍摄", "3 步", "10 分钟", "零基础", "啊哈"]
AUTHORS = ["设计师小王", "老张", "Amy", "数码君"]
PLATFORMS = ["抖音", "B站", "小红书"]
FAIL_REASONS = ["内容审核没通过", "文件不完整或已损坏，请重新复制", "视频预处理超时", "内容过长",
                "音频过短或无法解析：The request failed because the duration of the input audio (8ms) is shorter "
                "than the limit (50ms).",
                # 真实格式：带方舟的 Request id，一长串不带空格的字符（2026-10-24 NAS 上撑宽了任务列表）
                "音频过短或无法解析：The request failed because the duration of the input audio (39.8125ms) is "
                "shorter than the limit (50ms). Request id: 0217928366451153d763aaa7a5e423f9e5323a8c500bc9f287916"]


def main() -> None:
    config = load_config()
    lib = Library(config.library_dir)
    conn = connection.open_database(config.db_path)
    repo.ensure_singletons(conn, DEFAULT_PROMPTS, 1)
    lib.ensure_layout()
    random.seed(7)
    now = int(time.time() * 1000)
    plan = (["completed"] * 100 + ["queued"] * 80 + ["failed"] * 40 + ["stopped"] * 20 + ["ignored"] * 20)
    random.shuffle(plan)
    for i, status in enumerate(plan):
        words = random.sample(WORDS, 2)
        stem = f"{random.choice(AUTHORS)}&{random.choice(PLATFORMS)}&{words[0]}{words[1]}教程 {i}"
        if i == 3:
            stem = "黑客&抖音&<img src=x onerror=alert(1)>"
        orig = f"{random.choice(['教程', '美食', '旅行'])}/{stem}.mp4"
        created = now - random.randint(0, 30 * 86400_000)
        seed_task(conn, lib, orig, status, created, i)
    seed_special(conn, lib, now)
    seed_logs(conn, now)
    repo.set_queue_state(conn, "paused", None, False, False, now)
    print("造了", len(plan), "个任务；队列已暂停", json.dumps(repo.count_by_status(conn), ensure_ascii=False))
    conn.close()


def seed_task(conn: sqlite3.Connection, lib: Library, orig: str, status: str, created: int, i: int) -> None:
    if status == "ignored":
        video = naming.ignore_path(orig, 0)
    elif status == "completed":
        video = None  # 稍后按标题决定
    else:
        video = f"input/{orig}"
    title = None
    if status == "completed":
        title = f"{orig.split('&')[-1].removesuffix('.mp4')} 的要点"
        paths = naming.commit_paths(orig, naming.clean_title(title), 0, ".mp4")
        title, video = naming.clean_title(title), paths.video_path
        source = naming.split_source(naming.original_filename(orig))
        result = parse_result(json.dumps({
            "title": title,
            "main_content": {"overview": "这是造出来的示例内容。", "sections": ["第一段总结。", "第二段总结。"],
                             "conclusion": "示例结论。"},
            "highlight": "示例亮点", "transcript": ["示例转写第一段。", "示例转写第二段。"],
        }, ensure_ascii=False))
        md = render_md(title=title, platform=source.platform, author=source.author, original_file=orig,
                       video_name=paths.video_name, result=result)
        lib.make_dirs(str(Path(paths.md_path).parent))
        lib.path(paths.md_path).write_text(md, encoding="utf-8")
    full = lib.path(video)
    full.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ASSET, full)
    task_id = repo.enqueue_new_task(conn, orig, video, lib.ident(video), created)
    if status == "queued":
        return
    conn.execute(
        "UPDATE tasks SET status = ?, queue_seq = NULL, title = ?, md_path = ?, fail_reason = ?, notice = ?"
        " WHERE id = ?",
        (status, title, paths.md_path if status == "completed" else None,
         random.choice(FAIL_REASONS) if status == "failed" else None,
         "超过 512MB 且长于 120 分钟，不处理" if status == "ignored" and i % 3 == 0 else None, task_id),
    )


HOSTILE_MD = """
## 安全检查

<script>alert("md 里的脚本")</script>

<img src=x onerror=alert(1)>

![外部图片](https://example.com/tracker.png)

[javascript 链接](javascript:alert(1)) · [正常链接](https://www.volcengine.com/)

| 列 1 | 列 2 |
|---|---|
| 表格 | ~~删除线~~ |
"""


def seed_special(conn: sqlite3.Connection, lib: Library, now: int) -> None:
    """查看页的几种特殊情况，加入时间最新，默认排在列表最前面。"""
    work = Path("/tmp/seed")
    work.mkdir(exist_ok=True)
    hevc, audio_only = work / "hevc.mp4", work / "audio.mp4"
    lavfi = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi"]
    subprocess.run([*lavfi, "-i", "testsrc=size=640x360:rate=10:duration=3", "-c:v", "libx265", "-tag:v", "hvc1",
                    "-pix_fmt", "yuv420p", str(hevc)], check=True)
    subprocess.run([*lavfi, "-i", "sine=frequency=440:duration=3", "-c:a", "aac", str(audio_only)], check=True)
    cases = [
        ("特殊&抖音&HEVC 编码的视频", hevc, None),
        ("特殊&抖音&只有声音没有画面", audio_only, None),
        ("特殊&抖音&md 里有恶意内容", ASSET, "hostile"),
        ("特殊&抖音&md 已被删除", ASSET, "delete_md"),
        ("特殊&抖音&视频已被删除", ASSET, "delete_video"),
    ]
    for i, (stem, source, kind) in enumerate(cases):
        orig = f"特殊/{stem}.mp4"
        title = naming.clean_title(f"{stem.split('&')[-1]}（示例）")
        paths = naming.commit_paths(orig, title, 0, ".mp4")
        result = parse_result(json.dumps({
            "title": title,
            "main_content": {
                "overview": "查看页的特殊情况示例。", "sections": ["第一段。"], "conclusion": "示例结论。",
            },
            "highlight": "示例亮点", "transcript": ["示例转写。"],
        }, ensure_ascii=False))
        md = render_md(title=title, platform="抖音", author="特殊", original_file=orig,
                       video_name=paths.video_name, result=result)
        if kind == "hostile":
            md += HOSTILE_MD  # 直接写进文件，模拟用户或其他程序改过的 md
        lib.make_dirs(str(Path(paths.md_path).parent))
        lib.path(paths.md_path).write_text(md, encoding="utf-8")
        lib.make_dirs("output/video")
        shutil.copy(source, lib.path(paths.video_path))
        task_id = repo.enqueue_new_task(conn, orig, paths.video_path, lib.ident(paths.video_path), now + i)
        conn.execute("UPDATE tasks SET status = 'completed', queue_seq = NULL, title = ?, md_path = ? WHERE id = ?",
                     (title, paths.md_path, task_id))
        if kind == "delete_md":
            lib.remove(paths.md_path)
        if kind == "delete_video":
            lib.remove(paths.video_path)


def seed_logs(conn: sqlite3.Connection, now: int) -> None:
    """约 300 条日志（超过一页 100 条），含失败级别和名称里带 HTML 的任务。"""
    tasks = repo.list_tasks(conn)
    messages = [("info", "新视频加入队列"), ("info", "开始处理。提示词：\ntitle：视频的主题。20 字以内。"),
                ("warning", "第 1 次尝试失败：方舟服务端错误 503；第 2 次尝试将在 1 分钟后开始"),
                ("error", "处理失败：内容审核没通过"), ("info", "保存结果")]
    for n in range(300):
        task = tasks[n % len(tasks)]
        level, message = messages[n % len(messages)]
        name = naming.display_name(task["orig_rel_path"], task["title"])
        repo.add_log(conn, task["id"], name, now - (300 - n) * 60_000, level, message)


if __name__ == "__main__":
    main()
