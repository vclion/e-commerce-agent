from datetime import date as DateValue
from typing import Literal
from pydantic import Field, model_validator
from .schemas import Strict


class ImportMapping(Strict):
    columns: dict[str, str] = Field(default_factory=dict, max_length=100)
    defaults: dict[str, str] = Field(default_factory=dict, max_length=100)
    money_units: dict[str, Literal['fen', 'yuan']] = Field(default_factory=dict, max_length=30)
    value_maps: dict[str, dict[str, str]] = Field(default_factory=dict, max_length=30)
    shop_id: str | None = Field(default=None, min_length=1, max_length=20)
    timezone: Literal['Asia/Shanghai', 'UTC'] = 'Asia/Shanghai'


class ImportFile(Strict):
    kind: str = Field(max_length=30)
    filename: str = Field(min_length=1, max_length=180)
    text: str | None = Field(default=None, min_length=1, max_length=24000000)
    content_base64: str | None = Field(default=None, min_length=1, max_length=24000000)
    sheet: str | None = Field(default=None, max_length=100)
    mapping: ImportMapping | None = None

    @model_validator(mode='after')
    def one_source(self):
        if (self.text is None) == (self.content_base64 is None):
            raise ValueError('请提供文本报表或Excel文件，二者只能选择一个')
        return self


class ImportTemplate(Strict):
    name: str = Field(min_length=1, max_length=80)
    kind: str = Field(min_length=1, max_length=30)
    mapping: ImportMapping


class ScriptVersion(Strict):
    expected_version: int = Field(ge=1)


class Day(Strict):
    date: DateValue | None = None


class TaskUpdate(Strict):
    expected_version: int = Field(ge=1)
    status: Literal['pending', 'in_progress', 'blocked', 'review', 'completed', 'cancelled'] | None = None
    owner: str | None = Field(default=None, min_length=1, max_length=100)
    evidence: str | None = Field(default=None, max_length=5000)
    note: str | None = Field(default=None, max_length=5000)


class Segment(Strict):
    asset_id: str = Field(min_length=1, max_length=100)
    in_seconds: float = Field(ge=0, le=180, allow_inf_nan=False)
    out_seconds: float = Field(gt=0, le=180, allow_inf_nan=False)
    caption: str = Field(min_length=1, max_length=100)
    locked: bool = False


class ScriptSave(Strict):
    product_id: str = Field(min_length=1, max_length=100)
    reference_id: str | None = Field(default=None, max_length=180)
    title: str = Field(min_length=1, max_length=180)
    segments: list[Segment] = Field(min_length=1, max_length=20)
    expected_version: int | None = Field(default=None, ge=1)


class ScriptGenerate(Strict):
    product_id: str = Field(min_length=1, max_length=100)
    reference_id: str | None = Field(default=None, max_length=180)
    brief: str = Field(default='通勤商品介绍', min_length=1, max_length=2000)


class RenderCreate(Strict):
    expected_version: int = Field(ge=1)


class PeerCreate(Strict):
    shop_id: str = Field(min_length=1, max_length=20)
    title: str = Field(min_length=1, max_length=180)
    asset_id: str | None = Field(default=None, max_length=100)
    transcript: str = Field(min_length=5, max_length=12000)
    source: str = Field(default='用户提供', max_length=500)


class Schedule(Strict):
    enabled: bool
    hour: int = Field(default=9, ge=0, le=23)
