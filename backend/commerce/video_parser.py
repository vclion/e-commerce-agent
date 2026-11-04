"""Stable video URL parsing contract; the actual parser will be supplied later."""
from typing import Annotated, Literal, Protocol

from pydantic import Field, HttpUrl, field_validator, model_validator

from .schemas import Strict

PARSER_TIMEOUT_SECONDS = 60


class VideoParseRequest(Strict):
    shop_id: str = Field(min_length=1, max_length=20, description='解析结果所属的授权店铺')
    url: Annotated[HttpUrl, Field(max_length=4096, description='视频页面网址或分享短链接，仅支持HTTP/HTTPS')]

    @field_validator('url')
    @classmethod
    def no_credentials(cls, value):
        if value.username is not None or value.password is not None:
            raise ValueError('视频网址不能包含用户名或密码')
        return value


class VideoSubtitle(Strict):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=5000)

    @model_validator(mode='after')
    def valid_range(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError('字幕结束时间必须晚于开始时间')
        return self


class ParsedVideo(Strict):
    platform: str | None = Field(default=None, max_length=100)
    video_id: str | None = Field(default=None, max_length=200)
    title: str | None = Field(default=None, max_length=2000)
    author: str | None = Field(default=None, max_length=500)
    duration_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    media_url: HttpUrl | None = Field(default=None, description='解析工具返回的视频地址；不等于已下载或已入素材库')
    cover_url: HttpUrl | None = None


class VideoParseContent(Strict):
    """The supplied tool adapter maps its output to this provider-neutral object."""
    resolved_url: HttpUrl | None = Field(default=None, description='工具解析短链接后返回的原视频页面网址')
    video: ParsedVideo = Field(default_factory=ParsedVideo)
    caption: str | None = Field(default=None, max_length=50000, description='视频发布时的标题/描述文案，不是口播转写')
    transcript: str | None = Field(default=None, max_length=200000, description='完整字幕/口播转写；未获取时为null')
    transcript_source: Literal['platform_subtitles', 'asr', 'provided'] | None = None
    subtitles: list[VideoSubtitle] | None = Field(default=None, max_length=10000, description='带时间轴的字幕；未获取时为null')
    warnings: list[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def has_content(self):
        # A URL echo or empty object is not a parsed video.
        text = [self.video.video_id, self.video.title, self.caption, self.transcript]
        if not (any(value and value.strip() for value in text) or self.video.media_url or self.subtitles):
            raise ValueError('解析工具没有返回视频信息、文案或字幕')
        if self.transcript_source and not (self.transcript and self.transcript.strip() or self.subtitles):
            raise ValueError('缺少字幕时不能标记字幕来源')
        if self.video.duration_seconds is not None and self.subtitles:
            if any(s.end_seconds > self.video.duration_seconds for s in self.subtitles):
                raise ValueError('字幕时间超出视频时长')
        return self


class VideoParseResponse(VideoParseContent):
    shop_id: str
    source_url: HttpUrl
    parser: str


class VideoParseError(Strict):
    code: Literal['video_parser_not_configured', 'video_url_unsupported', 'video_parse_failed', 'video_parse_timeout']
    detail: str


class ParserNotConfigured(Exception):
    pass


class UnsupportedVideoURL(Exception):
    pass


class VideoParser(Protocol):
    name: str

    async def parse(self, url: str, *, shop_id: str) -> VideoParseContent:
        """Return actual tool output, never generated replacement content."""
        ...


class UnconfiguredVideoParser:
    name = 'unconfigured'

    async def parse(self, url: str, *, shop_id: str) -> VideoParseContent:
        raise ParserNotConfigured


def get_video_parser() -> VideoParser:
    # Replace this factory with the adapter for the user-supplied tool.
    return UnconfiguredVideoParser()
