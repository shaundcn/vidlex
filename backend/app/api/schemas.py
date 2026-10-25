"""全部请求和返回的 Pydantic 模型（技术选型 13.11）。"""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# 去掉首尾空白后不能为空（与数据库的 CHECK 一致）
# 不能含 *：防止把设置页里打码的 Key 当成新 Key 保存
ApiKeyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200, pattern=r"^[^*]+$")]
PromptText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class HealthOut(BaseModel):
    status: str
    schema_version: int
    ffmpeg: str | None


class Counts(BaseModel):
    queued: int
    completed: int
    failed: int
    stopped: int
    ignored: int


class SummaryOut(BaseModel):
    queue_state: str
    error_message: str | None
    counts: Counts
    list_version: int


class TaskOut(BaseModel):
    id: int
    original_name: str
    title: str | None
    created_at: int
    updated_at: int
    status: str
    step: str | None
    fail_reason: str | None
    notice: str | None
    analysis_mode: str | None
    allowed_actions: list[str]


class FrontMatterItem(BaseModel):
    key: str
    value: str


class ViewOut(BaseModel):
    id: int
    title: str | None
    original_name: str
    status: str
    video_exists: bool
    md_exists: bool
    front_matter: list[FrontMatterItem]
    md_html: str | None


class Prompts(BaseModel):
    title: PromptText
    main_content: PromptText
    highlight: PromptText
    transcript: PromptText


class Sleep(BaseModel):
    enabled: bool
    idle_minutes: int = Field(ge=5, le=1440)
    interval_hours: int = Field(ge=1, le=24)


class FolderStatus(BaseModel):
    name: str
    ok: bool
    reason: str | None


class SettingsOut(BaseModel):
    has_api_key: bool
    api_key_masked: str | None
    prompts: Prompts
    default_prompts: Prompts
    sleep: Sleep
    folders: list[FolderStatus]


class ApiKeyIn(BaseModel):
    key: ApiKeyText


class ApiKeyOut(BaseModel):
    saved: bool
    masked: str


class LogItem(BaseModel):
    id: int
    ts: int
    task_id: int
    task_name: str
    level: str
    message: str


class LogsOut(BaseModel):
    items: list[LogItem]
    has_more: bool


class LogTask(BaseModel):
    task_id: int
    task_name: str


class SelfTestOut(BaseModel):
    ok: bool
    message: str
