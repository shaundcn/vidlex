"""上传前按音频编码和文件大小决定怎么上传（立项第 3 节，技术选型 11.5、11.6）。

- 方舟只对实测能识别的编码原样上传；其他编码只把音频转成 AAC-LC，画面原样复制。
  （HE-AAC 以前也转码，理由“部分 HE-AACv2 解析不了”已被 11.6 推翻，2026-10-24 起原样上传；
  E-AC3 实测认不出，AMR 等未验证，仍然转码。）
- 超过 512MB 的视频只上传音频；长于 120 分钟或没有音轨时忽略。
"""

from dataclasses import dataclass
from typing import Literal

from app.domain.errors import NOT_VIDEO, OVERSIZED_NO_AUDIO, OVERSIZED_TOO_LONG, TaskFailed

AnalysisMode = Literal["video", "audio"]  # 传视频分析，或超过 512MB 只传音频

AUDIO_ONLY_MAX_SECONDS = 120 * 60  # 只传音频时的最长时长（方舟音频理解文档写明的上限，2026-10-24 用户确认）

# 实测方舟能识别的音频编码：(codec_name, profile)，profile 为 None 表示不看 profile（技术选型 11.5、11.7）
UPLOAD_AS_IS = frozenset({("aac", "LC"), ("aac", "HE-AAC"), ("aac", "HE-AACv2"), ("opus", None), ("flac", None)})


@dataclass(frozen=True)
class Stream:
    codec_type: str
    codec_name: str
    profile: str | None
    attached_pic: bool  # 封面图（mp3 等音频文件的专辑图）不算视频


@dataclass(frozen=True)
class Probe:
    streams: list[Stream]
    duration: float | None  # 秒；ffprobe 读不出时为 None


def require_video(streams: list[Stream]) -> None:
    """没有画面（封面图不算）时任务失败，例如扩展名是 .mp4 的 mp3。"""
    if not any(s.codec_type == "video" and not s.attached_pic for s in streams):
        raise TaskFailed(NOT_VIDEO)


def first_audio(streams: list[Stream]) -> Stream | None:
    return next((s for s in streams if s.codec_type == "audio"), None)


def needs_transcode(streams: list[Stream]) -> Stream | None:
    """返回需要转码的音频流；不需要时返回 None。没有视频流时任务失败。

    只看第一条音频流，转码时也只保留第一条视频和第一条音频。
    """
    require_video(streams)
    audio = first_audio(streams)
    if audio is None:
        return None
    if (audio.codec_name, audio.profile) in UPLOAD_AS_IS or (audio.codec_name, None) in UPLOAD_AS_IS:
        return None
    return audio


def describe(stream: Stream) -> str:
    """日志里显示的编码名，如 aac（HE-AACv2）。"""
    return f"{stream.codec_name}（{stream.profile}）" if stream.profile else stream.codec_name


def oversized_ignore_reason(probe: Probe) -> str | None:
    """超过 512MB 的视频：没有音轨或长于 120 分钟时返回忽略原因；可以只传音频时返回 None。

    读不出时长时按可以处理算，交给后面的完整检查。
    """
    if first_audio(probe.streams) is None:
        return OVERSIZED_NO_AUDIO
    if probe.duration is not None and probe.duration > AUDIO_ONLY_MAX_SECONDS:
        return OVERSIZED_TOO_LONG
    return None
