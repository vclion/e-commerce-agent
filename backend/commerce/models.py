from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


def uid():
    return uuid4().hex


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    display_name: Mapped[str] = mapped_column(String(80))


class AccessToken(Base):
    __tablename__ = 'access_tokens'
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(20), default='session')
    request_id: Mapped[str | None] = mapped_column(String(40))
    shop_id: Mapped[str | None] = mapped_column(String(20))
    scope_fingerprint: Mapped[str | None] = mapped_column(String(64))
    workspace: Mapped[str] = mapped_column(String(30), default='general', server_default='general')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Membership(Base):
    __tablename__ = 'memberships'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(20), primary_key=True)


class Product(Base):
    __tablename__ = 'products'
    __table_args__ = (CheckConstraint('price_fen > 0 AND cost_fen >= 0 AND stock >= 0'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    group_id: Mapped[str] = mapped_column(String(40))
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40), index=True)
    brand: Mapped[str] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text)
    price_fen: Mapped[int] = mapped_column(Integer)
    cost_fen: Mapped[int] = mapped_column(Integer)
    stock: Mapped[int] = mapped_column(Integer)
    specs: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='published')
    version: Mapped[int] = mapped_column(Integer, default=1)
    stock_days: Mapped[int | None] = mapped_column(Integer)
    quality_samples: Mapped[int | None] = mapped_column(Integer)
    quality_issues: Mapped[int | None] = mapped_column(Integer)


class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey('products.id'))
    title: Mapped[str] = mapped_column(String(160))
    dataset_version: Mapped[str] = mapped_column(String(30), default='apparel-v1', index=True)
    specs_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    refund_reason: Mapped[str | None] = mapped_column(String(80))
    quantity: Mapped[int] = mapped_column(Integer)
    paid_fen: Mapped[int] = mapped_column(Integer)
    cost_fen: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_fen: Mapped[int] = mapped_column(Integer, default=0)
    refund_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)


class AfterSale(Base):
    __tablename__ = 'after_sales'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    order_id: Mapped[str] = mapped_column(ForeignKey('orders.id'), unique=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    shop_id: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default='submitted')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Draft(Base):
    __tablename__ = 'drafts'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    shop_id: Mapped[str | None] = mapped_column(String(20), index=True)
    source_scope: Mapped[dict[str, str] | None] = mapped_column(JSON(none_as_null=True))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    kind: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default='draft')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Action(Base):
    __tablename__ = 'actions'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    shop_id: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(40))
    expected_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    confirmation_digest: Mapped[str] = mapped_column(String(64))
    # Only this actor's authenticated UI can retrieve the confirmation token.
    confirmation_token: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default='prepared')
    result: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Confirmation(Base):
    __tablename__ = 'confirmations'
    __table_args__ = (UniqueConstraint('user_id', 'key'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    key: Mapped[str] = mapped_column(String(100))
    action_id: Mapped[str] = mapped_column(ForeignKey('actions.id'))
    payload_hash: Mapped[str] = mapped_column(String(64))


class Competitor(Base):
    __tablename__ = 'competitors'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    product_id: Mapped[str | None] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(40))
    specs: Mapped[dict] = mapped_column(JSON)
    price_fen: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(10), default='CNY')
    source: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_demo: Mapped[bool] = mapped_column(default=True)


class Audit(Base):
    __tablename__ = 'audit_events'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    request_id: Mapped[str] = mapped_column(String(40), index=True)
    user_id: Mapped[str | None] = mapped_column(String(40))
    operation: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChatRun(Base):
    __tablename__ = 'chat_runs'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    shop_id: Mapped[str | None] = mapped_column(String(20), index=True)
    scope_fingerprint: Mapped[str | None] = mapped_column(String(64))
    conversation_id: Mapped[str | None] = mapped_column(String(100), index=True)
    session_id: Mapped[str | None] = mapped_column(String(40), index=True)
    turn_seq: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    memory_excluded: Mapped[bool] = mapped_column(default=False, server_default='false')
    workspace: Mapped[str] = mapped_column(String(30), default='general', server_default='general', index=True)
    effects: Mapped[list] = mapped_column(JSON, default=list, server_default='[]')
    sources: Mapped[list] = mapped_column(JSON, default=list)
    query: Mapped[str] = mapped_column(Text)
    answer: Mapped[str] = mapped_column(Text, default='')
    status: Mapped[str] = mapped_column(String(30), default='running')
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_request_id: Mapped[str | None] = mapped_column(String(64))
    retry_of: Mapped[str | None] = mapped_column(String(40))


class ToolExecution(Base):
    """A durable write fence; an uncertain commit must never be blindly replayed."""
    __tablename__ = 'tool_executions'
    __table_args__ = (UniqueConstraint('run_id', 'operation', 'arguments_hash'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('chat_runs.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    arguments_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default='running')
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    context_ref: Mapped[str] = mapped_column(String(100))
    workspace: Mapped[str] = mapped_column(String(30), default='general', server_default='general')
    session_id: Mapped[str | None] = mapped_column(String(40), index=True)


class Manifest(Base):
    __tablename__ = 'manifests'
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON)


class OpsRecord(Base):
    __tablename__ = 'ops_records'
    __table_args__ = (UniqueConstraint('shop_id', 'kind', 'external_id'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    external_id: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON)
    batch_id: Mapped[str | None] = mapped_column(String(40))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsImport(Base):
    __tablename__ = 'ops_imports'
    __table_args__ = (UniqueConstraint('user_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    shop_scope: Mapped[list] = mapped_column(JSON)
    kind: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(180))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='preview')
    rows: Mapped[list] = mapped_column(JSON)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsTask(Base):
    __tablename__ = 'ops_tasks'
    __table_args__ = (UniqueConstraint('shop_id', 'work_date', 'template_id'),)
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    work_date: Mapped[datetime] = mapped_column(Date, index=True)
    template_id: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(200))
    owner: Mapped[str] = mapped_column(String(100), default='待分配')
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    priority: Mapped[str] = mapped_column(String(20), default='normal')
    status: Mapped[str] = mapped_column(String(20), default='pending')
    evidence: Mapped[str] = mapped_column(Text, default='')
    note: Mapped[str] = mapped_column(Text, default='')
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    history: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsIssue(Base):
    # Archived records only; the issue module has no active API, tools or imports.
    __tablename__ = 'ops_issues'
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey('ops_tasks.id'))
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text, default='')
    owner: Mapped[str] = mapped_column(String(100), default='待分配')
    status: Mapped[str] = mapped_column(String(20), default='open')
    resolution: Mapped[str] = mapped_column(Text, default='')
    history: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsAsset(Base):
    __tablename__ = 'ops_assets'
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    group_id: Mapped[str] = mapped_column(String(80), default='')
    title: Mapped[str] = mapped_column(String(180))
    relative_path: Mapped[str] = mapped_column(String(220))
    metadata_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsScript(Base):
    __tablename__ = 'ops_scripts'
    id: Mapped[str] = mapped_column(String(100), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    title: Mapped[str] = mapped_column(String(180))
    payload: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OpsJob(Base):
    __tablename__ = 'ops_jobs'
    __table_args__ = (UniqueConstraint('shop_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    shop_id: Mapped[str] = mapped_column(String(20), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    script_id: Mapped[str] = mapped_column(ForeignKey('ops_scripts.id'))
    fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpsSchedule(Base):
    __tablename__ = 'ops_schedules'
    shop_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    enabled: Mapped[bool] = mapped_column(default=True)
    hour: Mapped[int] = mapped_column(Integer, default=9)
    last_date: Mapped[datetime | None] = mapped_column(Date)
    error: Mapped[str | None] = mapped_column(Text)


class PlatformShop(Base):
    __tablename__ = 'platform_shops'
    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlatformConnection(Base):
    __tablename__ = 'platform_connections'
    __table_args__ = (UniqueConstraint('user_id', 'provider', 'mode', 'external_id'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey('platform_shops.id'), index=True)
    provider: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(16))
    external_id: Mapped[str] = mapped_column(String(120))
    app_key: Mapped[str] = mapped_column(String(120), default='')
    credentials: Mapped[str] = mapped_column(Text, default='')
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='unconfigured')
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(default=False)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlatformSync(Base):
    __tablename__ = 'platform_syncs'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    connection_id: Mapped[str] = mapped_column(ForeignKey('platform_connections.id'), index=True)
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    days: Mapped[int] = mapped_column(Integer, default=7)
    resources: Mapped[list] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
