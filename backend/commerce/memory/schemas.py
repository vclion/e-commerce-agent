from datetime import datetime
from typing import Literal
from pydantic import Field, field_validator
from ..schemas import Strict
from ..chat_workspaces import Workspace


class MemoryCreate(Strict):
    kind: Literal['semantic', 'decision', 'episodic', 'procedural'] = 'semantic'
    scope: Literal['personal', 'personal_shop', 'workspace'] = 'personal_shop'
    workspace: Workspace = 'today'
    subject: str = Field(default='report', min_length=1, max_length=180)
    predicate: str = Field(default='preference', min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=4000)
    valid_from: datetime | None = None
    expires_at: datetime | None = None

    @field_validator('valid_from', 'expires_at')
    @classmethod
    def aware(cls, value):
        if value and value.tzinfo is None:
            raise ValueError('时间必须包含时区')
        return value


class MemoryUpdate(Strict):
    expected_revision: int = Field(ge=1)
    content: str = Field(min_length=1, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=180)
    status: Literal['active', 'candidate', 'disabled', 'cancelled'] = 'active'
    valid_from: datetime | None = None
    expires_at: datetime | None = None

    _aware = field_validator('valid_from', 'expires_at')(MemoryCreate.aware.__func__)


class Forget(Strict):
    item_ids: list[str] = Field(min_length=1, max_length=100)


class Preferences(Strict):
    read_enabled: bool | None = None
    auto_write: bool | None = None
    context_index_enabled: bool | None = None
    reflection_enabled: bool | None = None


class MemorySearch(Strict):
    query: str = Field(default='', max_length=500)
    workspace: Workspace = 'today'
    limit: int = Field(default=8, ge=1, le=20)


class HistoryRead(Strict):
    run_id: str = Field(default='', max_length=40)
    index_id: str = Field(default='', max_length=40)
    query: str = Field(default='', max_length=500)
    field: Literal['user', 'assistant', 'effects'] = 'assistant'
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=1200, ge=1, le=4000)
    after_turn: int = Field(default=0, ge=0)
    limit: int = Field(default=3, ge=1, le=5)
