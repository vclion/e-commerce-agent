from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .chat_workspaces import Workspace
from .tool_arguments import parse_tool_arguments


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Login(Strict):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


class Search(Strict):
    query: str = Field(default='', max_length=100)
    category: str = Field(default='', max_length=100)
    min_price_fen: int = Field(default=0, ge=0)
    max_price_fen: int = Field(default=10000000, ge=0)
    size: str = Field(default='', max_length=10)
    color: str = Field(default='', max_length=30)
    fit: str = Field(default='', max_length=20)
    material: str = Field(default='', max_length=40)
    season: str = Field(default='', max_length=20)
    occasion: str = Field(default='', max_length=20)
    group_styles: bool = False
    in_stock: bool = True
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=24, ge=1, le=100)

    @model_validator(mode='after')
    def check_range(self):
        if self.min_price_fen > self.max_price_fen:
            raise ValueError('最低价格不能大于最高价格')
        return self


class Content(Strict):
    kind: Literal['marketing', 'report']
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=5, max_length=20000)
    source_ids: list[str] = Field(default_factory=list, max_length=100)


class Compare(Strict):
    product_ids: list[str] = Field(min_length=1, max_length=3)


class Chat(Strict):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str = Field(default='', max_length=100)
    session_id: str = Field(default='', max_length=40)
    temporary: bool = False
    workspace: Workspace = 'general'
    client_request_id: str = Field(default='', max_length=64, pattern=r'^[a-zA-Z0-9_-]*$')
    retry_of: str = Field(default='', max_length=40)


class ToolCall(Strict):
    operation: Literal['products', 'compare', 'orders', 'after_sales', 'analytics', 'selection', 'competitors', 'product_detail', 'knowledge', 'clarify',
        'ops_overview', 'ops_videos', 'ops_peers', 'ops_script_context', 'ops_peer_context', 'ops_tasks', 'ops_report', 'ops_scripts', 'ops_jobs',
        'ops_sop_generate', 'ops_task_update', 'ops_script_save', 'ops_render',
        'workspace_context', 'ops_schedules', 'ops_schedule_set', 'ops_assets',
        'ops_peer_create', 'ops_peer_analyze', 'ops_script_generate', 'ops_script_review',
        'ops_script_rewrite', 'ops_render_retry', 'ops_imports', 'ops_import_batch',
        'ops_import_fields', 'ops_import_mappings', 'ops_import_inspect', 'ops_import_preview',
        'ops_import_commit', 'ops_import_mapping_save', 'content_list', 'content_save',
        'competitors_import', 'platform_connections', 'platform_syncs', 'platform_connect',
        'platform_sync', 'platform_schedule_set', 'platform_product_facts_set', 'audit_events',
        'ops_demo_import', 'platform_products', 'platform_disconnect', 'chat_run_records',
        'memory_search', 'memory_get', 'memory_propose', 'memory_forget', 'history_read']
    arguments: dict = Field(default_factory=dict)

    @field_validator('arguments', mode='before')
    @classmethod
    def parse_tool_object(cls, value):
        return parse_tool_arguments(value)


class Snapshot(Strict):
    product_id: str
    title: str = Field(min_length=1, max_length=160)
    price_fen: int = Field(gt=0, le=10000000)
    specs: dict[str, str | int | float]
    source: str = Field(min_length=5, max_length=500)
    captured_at: datetime
    currency: Literal['CNY'] = 'CNY'
