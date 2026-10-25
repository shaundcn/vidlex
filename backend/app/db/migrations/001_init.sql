-- 001 初始结构：6 张表、索引、触发器。依据技术选型第 4.4、4.6 节。
-- 已执行的迁移文件不再修改；结构变更新建编号文件并写明原因（技术选型 4.8）。
-- 时间都是 UTC 毫秒；路径都是相对路径（技术选型 4.3）。

-- 任务：一个视频一条
CREATE TABLE tasks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    orig_rel_path      TEXT    NOT NULL,           -- 相对 input，写入后不能改
    current_video_path TEXT,                       -- 相对视频库；只有已完成可以为空
    md_path            TEXT,                       -- 相对视频库；已完成必填，其他为空
    title              TEXT,                       -- 已完成必填，其他为空
    status             TEXT    NOT NULL
        CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'stopped', 'ignored')),
    step               TEXT
        CHECK (step IN ('transcode', 'compress', 'upload', 'analyze', 'render_md', 'commit')),
    fail_reason        TEXT,
    notice             TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    queue_seq          INTEGER,
    src_dev            INTEGER NOT NULL,
    src_ino            INTEGER NOT NULL,
    src_size           INTEGER NOT NULL CHECK (src_size >= 0),
    src_mtime_ns       INTEGER NOT NULL,
    created_at         INTEGER NOT NULL,           -- 加入时间，写入后不能改
    updated_at         INTEGER NOT NULL,
    CHECK ((status = 'processing') = (step IS NOT NULL)),
    CHECK ((status = 'failed') = (fail_reason IS NOT NULL)),
    CHECK ((status = 'completed') = (title IS NOT NULL)),
    CHECK ((status = 'completed') = (md_path IS NOT NULL)),
    CHECK (status = 'completed' OR current_video_path IS NOT NULL),
    CHECK (status <> 'queued' OR queue_seq IS NOT NULL)
) STRICT;

CREATE UNIQUE INDEX tasks_current_video_path ON tasks (current_video_path);
CREATE UNIQUE INDEX tasks_md_path ON tasks (md_path);
CREATE INDEX tasks_queue ON tasks (queue_seq, id) WHERE status = 'queued';
-- 同一时间只处理一个视频
CREATE UNIQUE INDEX tasks_one_processing ON tasks (status) WHERE status = 'processing';

CREATE TRIGGER tasks_immutable_fields
BEFORE UPDATE OF orig_rel_path, created_at ON tasks
WHEN NEW.orig_rel_path IS NOT OLD.orig_rel_path OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'orig_rel_path 和 created_at 写入后不能修改');
END;

-- 文件操作进度记录：一个任务同时最多一条
CREATE TABLE task_ops (
    task_id           INTEGER PRIMARY KEY REFERENCES tasks (id) ON DELETE RESTRICT,
    kind              TEXT    NOT NULL CHECK (kind IN ('commit', 'redo', 'ignore', 'unignore')),
    phase             TEXT    NOT NULL,
    src_path          TEXT    NOT NULL,
    src_dev           INTEGER NOT NULL,
    src_ino           INTEGER NOT NULL,
    src_size          INTEGER NOT NULL,
    src_mtime_ns      INTEGER NOT NULL,
    video_target_path TEXT    NOT NULL,
    md_tmp_path       TEXT,
    md_target_path    TEXT,
    title             TEXT,
    old_md_path       TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    CHECK (
        (kind = 'commit'   AND phase IN ('recorded', 'md_placed', 'video_moved')) OR
        (kind = 'redo'     AND phase IN ('recorded', 'video_restored', 'old_md_deleted')) OR
        (kind = 'ignore'   AND phase IN ('recorded', 'video_moved')) OR
        (kind = 'unignore' AND phase IN ('recorded', 'video_restored'))
    ),
    CHECK (CASE WHEN kind = 'commit'
                THEN md_tmp_path IS NOT NULL AND md_target_path IS NOT NULL AND title IS NOT NULL
                ELSE md_tmp_path IS NULL AND md_target_path IS NULL AND title IS NULL
           END),
    CHECK (kind = 'redo' OR old_md_path IS NULL)
) STRICT;

-- 任务日志：只靠 task_id 对应，不设外键；顺序按 id
CREATE TABLE task_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL,
    task_name TEXT    NOT NULL,
    ts        INTEGER NOT NULL,
    level     TEXT    NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    message   TEXT    NOT NULL
) STRICT;

CREATE INDEX task_logs_task ON task_logs (task_id, id);
CREATE INDEX task_logs_ts ON task_logs (ts);

-- 云端副本登记
CREATE TABLE cloud_files (
    file_id    TEXT    NOT NULL PRIMARY KEY,
    expire_at  INTEGER NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

-- 设置：只有一行；提示词的默认值由程序写入（只在程序里保存一份）
-- 去首尾空白时去掉空格、制表符、换行和全角空格
CREATE TABLE settings (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    api_key              TEXT CHECK (api_key IS NULL OR trim(api_key, char(9, 10, 13, 32, 12288)) <> ''),
    prompt_title         TEXT    NOT NULL CHECK (trim(prompt_title, char(9, 10, 13, 32, 12288)) <> ''),
    prompt_main_content  TEXT    NOT NULL CHECK (trim(prompt_main_content, char(9, 10, 13, 32, 12288)) <> ''),
    prompt_highlight     TEXT    NOT NULL CHECK (trim(prompt_highlight, char(9, 10, 13, 32, 12288)) <> ''),
    prompt_transcript    TEXT    NOT NULL CHECK (trim(prompt_transcript, char(9, 10, 13, 32, 12288)) <> ''),
    sleep_enabled        INTEGER NOT NULL DEFAULT 1 CHECK (sleep_enabled IN (0, 1)),
    sleep_idle_minutes   INTEGER NOT NULL DEFAULT 30 CHECK (sleep_idle_minutes BETWEEN 5 AND 1440),
    sleep_interval_hours INTEGER NOT NULL DEFAULT 2 CHECK (sleep_interval_hours BETWEEN 1 AND 24),
    updated_at           INTEGER NOT NULL
) STRICT;

-- 运行状态：只有一行
CREATE TABLE runtime_state (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    queue_state          TEXT    NOT NULL DEFAULT 'running'
        CHECK (queue_state IN ('running', 'paused', 'sleeping', 'error')),
    error_message        TEXT,
    error_auto_resume    INTEGER NOT NULL DEFAULT 0 CHECK (error_auto_resume IN (0, 1)),
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    library_id           TEXT,
    updated_at           INTEGER NOT NULL,
    CHECK ((queue_state = 'error') = (error_message IS NOT NULL)),
    CHECK (queue_state = 'error' OR error_auto_resume = 0)
) STRICT;
