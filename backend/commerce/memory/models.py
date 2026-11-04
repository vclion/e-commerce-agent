from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from ..db import Base
from ..models import now, uid


class MemorySettings(Base):
    __tablename__ = 'memory_settings'
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    read_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_write: Mapped[bool] = mapped_column(Boolean, default=True)
    context_index_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reflection_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    read_epoch: Mapped[int] = mapped_column(Integer, default=1)
    epoch: Mapped[int] = mapped_column(Integer, default=1)


class ChatSession(Base):
    __tablename__ = 'memory_sessions'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    shop_id: Mapped[str | None] = mapped_column(String(40))
    workspace: Mapped[str] = mapped_column(String(30))
    scope_fingerprint: Mapped[str] = mapped_column(String(64))
    epoch: Mapped[int] = mapped_column(Integer, default=1)
    next_seq: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str] = mapped_column(Text, default='')
    through_seq: Mapped[int] = mapped_column(Integer, default=0)
    summary_sources: Mapped[list] = mapped_column(JSONB, default=list)
    upstream_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContextSegment(Base):
    """Independent summaries made from originals, never from another summary."""
    __tablename__ = 'memory_context_segments'
    __table_args__ = (UniqueConstraint('session_id', 'epoch', 'start_seq'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey('memory_sessions.id', ondelete='CASCADE'), index=True)
    epoch: Mapped[int] = mapped_column(Integer)
    start_seq: Mapped[int] = mapped_column(Integer)
    end_seq: Mapped[int] = mapped_column(Integer)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    title: Mapped[str] = mapped_column(String(180))
    summary: Mapped[str] = mapped_column(Text)
    overview: Mapped[str] = mapped_column(Text)
    key_points: Mapped[list] = mapped_column(JSONB, default=list)
    entities: Mapped[list] = mapped_column(JSONB, default=list)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_seq: Mapped[int] = mapped_column(Integer, default=0)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemorySpace(Base):
    __tablename__ = 'memory_spaces'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    shop_id: Mapped[str | None] = mapped_column(String(40), index=True)
    workspace: Mapped[str | None] = mapped_column(String(30))
    required_shops: Mapped[dict] = mapped_column(JSONB, default=dict)
    epoch: Mapped[int] = mapped_column(Integer, default=1)


class MemoryGrant(Base):
    __tablename__ = 'memory_grants'
    space_id: Mapped[str] = mapped_column(ForeignKey('memory_spaces.id'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), primary_key=True)
    can_publish: Mapped[bool] = mapped_column(Boolean, default=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False)


class MemoryItem(Base):
    __tablename__ = 'memory_items'
    __table_args__ = (UniqueConstraint('space_id', 'logical_key'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    space_id: Mapped[str] = mapped_column(ForeignKey('memory_spaces.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    logical_key: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(180))
    predicate: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(180))
    status: Mapped[str] = mapped_column(String(30), default='active', index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_level: Mapped[str] = mapped_column(String(30), default='explicit')
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryVersion(Base):
    __tablename__ = 'memory_versions'
    __table_args__ = (UniqueConstraint('item_id', 'revision'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    item_id: Mapped[str] = mapped_column(ForeignKey('memory_items.id'), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    actor_id: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(200), default='明确保存')
    pipeline_version: Mapped[str] = mapped_column(String(120), default='manual-v1')


class MemoryEvidence(Base):
    __tablename__ = 'memory_evidence'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    item_id: Mapped[str] = mapped_column(ForeignKey('memory_items.id'), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey('memory_versions.id'))
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(100), index=True)
    source_role: Mapped[str] = mapped_column(String(30))
    quote: Mapped[str] = mapped_column(Text, default='')
    fingerprint: Mapped[str] = mapped_column(String(64))


class MemoryLink(Base):
    __tablename__ = 'memory_links'
    parent_id: Mapped[str] = mapped_column(ForeignKey('memory_items.id'), primary_key=True)
    child_id: Mapped[str] = mapped_column(ForeignKey('memory_items.id'), primary_key=True)
    relation: Mapped[str] = mapped_column(String(30), primary_key=True, default='derived_from')


class MemoryChunk(Base):
    __tablename__ = 'memory_chunks'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    item_id: Mapped[str] = mapped_column(ForeignKey('memory_items.id'), index=True)
    version_id: Mapped[str] = mapped_column(ForeignKey('memory_versions.id'), unique=True)
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[str] = mapped_column(Text)
    embedding_state: Mapped[str] = mapped_column(String(30), default='pending')


class MemoryJob(Base):
    __tablename__ = 'memory_jobs'
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[str] = mapped_column(String(100))
    dedup_key: Mapped[str] = mapped_column(String(180), unique=True)
    epoch: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(40))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MemoryRunContext(Base):
    __tablename__ = 'memory_run_contexts'
    run_id: Mapped[str] = mapped_column(ForeignKey('chat_runs.id'), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey('memory_sessions.id'))
    epoch: Mapped[int] = mapped_column(Integer)
    scope_fingerprint: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text)
    references: Mapped[list] = mapped_column(JSONB, default=list)
    timings: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(30), default='active')
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemorySuppression(Base):
    __tablename__ = 'memory_suppressions'
    __table_args__ = (UniqueConstraint('user_id', 'key'),)
    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(180))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
