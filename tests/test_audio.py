"""上传前按音频编码决定是否转码（技术选型 11.5）。"""

import pytest

from app.domain.audio import AUDIO_ONLY_MAX_SECONDS, Probe, Stream, describe, needs_transcode, oversized_ignore_reason
from app.domain.errors import NOT_VIDEO, OVERSIZED_NO_AUDIO, OVERSIZED_TOO_LONG, TaskFailed

VIDEO = Stream("video", "h264", "High", False)


def audio(codec: str, profile: str | None = None) -> Stream:
    return Stream("audio", codec, profile, False)


@pytest.mark.parametrize("stream", [audio("aac", "LC"), audio("aac", "HE-AAC"), audio("aac", "HE-AACv2"),
                                    audio("opus"), audio("flac")])
def test_verified_codecs_upload_as_is(stream: Stream) -> None:
    assert needs_transcode([VIDEO, stream]) is None


# aac 没有 profile：ffprobe 读不出是哪种 AAC，按没验证过处理
@pytest.mark.parametrize("stream", [audio("eac3"), audio("amr_nb"), audio("aac"), audio("aac", "Main")])
def test_other_codecs_transcoded(stream: Stream) -> None:
    assert needs_transcode([stream, VIDEO]) == stream


def test_only_first_audio_counts() -> None:
    assert needs_transcode([VIDEO, audio("aac", "LC"), audio("eac3")]) is None


def test_no_audio_uploaded_as_is() -> None:
    assert needs_transcode([VIDEO]) is None


@pytest.mark.parametrize("streams", [[audio("mp3")], [audio("mp3"), Stream("video", "mjpeg", None, True)], []])
def test_no_video_fails(streams: list[Stream]) -> None:
    with pytest.raises(TaskFailed, match=NOT_VIDEO):
        needs_transcode(streams)


def test_describe() -> None:
    assert describe(audio("aac", "HE-AACv2")) == "aac（HE-AACv2）"
    assert describe(audio("eac3")) == "eac3"


@pytest.mark.parametrize(("streams", "duration", "reason"), [
    ([VIDEO, audio("aac", "HE-AACv2")], 1090.0, None),  # NAS 上 571MB、18 分钟的视频：只传音频
    ([VIDEO, audio("aac", "LC")], AUDIO_ONLY_MAX_SECONDS, None),  # 正好 120 分钟仍处理
    ([VIDEO, audio("aac", "LC")], AUDIO_ONLY_MAX_SECONDS + 1, OVERSIZED_TOO_LONG),
    ([VIDEO], 60.0, OVERSIZED_NO_AUDIO),
    ([VIDEO, audio("aac", "LC")], None, None),  # 读不出时长：交给后面的完整检查
])
def test_oversized_ignore_reason(streams: list[Stream], duration: float | None, reason: str | None) -> None:
    assert oversized_ignore_reason(Probe(streams, duration)) == reason
