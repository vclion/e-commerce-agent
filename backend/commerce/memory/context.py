from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, func
from ..auth import scope_fingerprint, scoped_actor
from ..config import settings
from ..models import ChatRun, Conversation, User, AccessToken, Audit, now
from .models import ChatSession, MemoryRunContext, MemoryItem
from .policy import preferences, allowed_spaces, source_visible
from .service import effective_version
from .retrieval import search
from . import hierarchy


def session_for(db, actor, body):
    session = db.get(ChatSession, body.session_id) if body.session_id else None
    if body.session_id and (not session or session.user_id != actor.id):
        raise HTTPException(404, '会话不存在或不可访问')
    if not session and body.conversation_id:
        conv = db.get(Conversation, body.conversation_id)
        if conv and conv.session_id:
            session = db.get(ChatSession, conv.session_id)
    if session and (session.user_id != actor.id or session.shop_id != actor.shop_id or
        session.workspace != body.workspace or session.scope_fingerprint != scope_fingerprint(actor)):
        raise HTTPException(409, '会话所属用户、店铺、栏目或数据环境已变化')
    if not session:
        session = ChatSession(user_id=actor.id, shop_id=actor.shop_id, workspace=body.workspace,
            scope_fingerprint=scope_fingerprint(actor), upstream_id=body.conversation_id or None)
        db.add(session); db.flush()
    return session


def build(db, actor, session, run, temporary=False):
    prefs = preferences(db, actor.id)
    original_epoch, original_session_epoch = prefs.epoch, session.epoch
    original_read_epoch = prefs.read_epoch
    entries, timings = [], {}
    status = 'temporary' if temporary else 'disabled'
    if settings.memory_enabled and prefs.read_enabled and not temporary:
        spaces = [s.id for s in allowed_spaces(db, actor, session.workspace)]
        pinned = list(db.scalars(select(MemoryItem).where(MemoryItem.space_id.in_(spaces),
            MemoryItem.kind.in_(['semantic','decision']), MemoryItem.status.in_(['active','cancelled']),
            (MemoryItem.expires_at.is_(None) | (MemoryItem.expires_at > now())))
            .order_by(MemoryItem.updated_at.desc()).limit(30)))
        facts = []
        for item in pinned:
            version = effective_version(db, item)
            if version and source_visible(db, actor, item, session.workspace):
                entry = {'id': item.id, 'version_id': version.id, 'revision': version.revision,
                    'title': item.title, 'kind': item.kind, 'status':item.status, 'content': version.content}
                facts.append(entry)
        found = search(db, actor, run.query[:500], session.workspace, limit=5)
        entries = facts + [x for x in found['items'] if x['id'] not in {v['id'] for v in facts}]
        status, timings = found['status'], found['timings']
    payload, selected, trace = hierarchy.assemble(db, session, run, entries, temporary)
    db.refresh(prefs); db.refresh(session)
    fresh_actor = scoped_actor(db, db.get(User, actor.id), session.shop_id)
    if prefs.epoch != original_epoch or prefs.read_epoch != original_read_epoch or session.epoch != original_session_epoch or scope_fingerprint(fresh_actor) != scope_fingerprint(actor):
        raise HTTPException(409, '记忆或授权已变化，本轮已停止，请重新提问')
    references = [{k: entry[k] for k in ['id', 'version_id', 'revision', 'title', 'kind']} for entry in selected]
    snapshot = MemoryRunContext(run_id=run.id, user_id=actor.id, session_id=session.id,
        epoch=prefs.epoch, scope_fingerprint=scope_fingerprint(actor), payload=payload,
        references=references, timings={**timings, **trace, 'memory_status': status, 'temporary': temporary,
                                       'read_epoch': original_read_epoch},
        expires_at=now()+timedelta(minutes=5))
    db.add(snapshot); db.flush()
    validate(db, run.id, fresh_actor)
    return snapshot


def bind_token(db, run_id, token):
    from ..auth import digest
    snapshot = db.get(MemoryRunContext, run_id)
    snapshot.timings = {**snapshot.timings, 'active_context_digest':digest(token)}
    db.commit()


def validate_token(db, token):
    snapshot = validate(db, token.request_id)
    if token.expires_at <= now():
        raise HTTPException(401, '本轮工具凭证已失效')
    if snapshot and snapshot.timings.get('active_context_digest') not in (None, token.digest):
        raise HTTPException(409, '本轮上下文已重建，旧工具凭证不再可用')
    return snapshot


def retry_context(run_id, old_ref):
    """Reserve one retry and revoke its predecessor before calling a compactor."""
    from ..db import SessionLocal
    from ..auth import digest, issue_token
    from . import indexes, hierarchy as h
    from .reactive import ContextLimitError, minimum_retry_budget
    with SessionLocal() as db:
        token = db.scalar(select(AccessToken).where(AccessToken.digest == digest(old_ref)).with_for_update())
        if not token or token.request_id != run_id:
            raise ContextLimitError()
        snapshot = validate_token(db, token)
        run = db.get(ChatRun, run_id)
        attempted = db.scalar(select(func.count()).select_from(Audit).where(Audit.request_id == run_id,
            Audit.user_id == token.user_id, Audit.operation == 'tool.attempt'))
        if not snapshot or snapshot.timings.get('reactive_retry_count', 0) or attempted or run.status != 'running':
            raise ContextLimitError('模型上下文超限，本轮已调用工具或已重试，停止自动重试；请查看运行记录')
        old_size = h.token_bound(snapshot.payload)
        token.expires_at = now()
        snapshot.timings = {**snapshot.timings,'reactive_retry_count':1,'active_context_digest':'retry-preparing'}
        session = db.get(ChatSession, snapshot.session_id)
        session.upstream_id = None
        run.conversation_id = None
        db.commit()
    with SessionLocal() as db:
        snapshot = validate(db, run_id)
        original_epoch = snapshot.epoch
        session = db.get(ChatSession, snapshot.session_id)
        session_epoch = session.epoch
        run = db.get(ChatRun, run_id)
        actor = scoped_actor(db, db.get(User, run.user_id), session.shop_id)
        recent = list(db.scalars(select(ChatRun).where(*indexes.history_scope(session,run.turn_seq))
            .order_by(ChatRun.turn_seq.desc()).limit(settings.memory_recent_turns)))
        recent.reverse()
        target = min(h.context_budget(run.query), max(1024, minimum_retry_budget(recent),
            int(old_size*settings.memory_reactive_retry_ratio)))
        payload, selected, trace = indexes.assemble(db, session, run, [], snapshot.timings.get('temporary',False),
            force_budget=target, drop_optional=True, trigger='provider_context_limit')
        # Compression may have yielded to forget/revoke. Never reactivate an invalid snapshot.
        db.expire_all()
        snapshot = validate(db, run_id)
        session = db.get(ChatSession, snapshot.session_id)
        if db.get(ChatRun, run_id, populate_existing=True).status != 'running':
            raise HTTPException(409, '本轮对话已结束，停止重建上下文')
        fresh_actor = scoped_actor(db, db.get(User, run.user_id), session.shop_id)
        if snapshot.epoch != original_epoch or session.epoch != session_epoch or scope_fingerprint(actor) != scope_fingerprint(fresh_actor):
            raise HTTPException(409, '记忆或授权已变化，本轮已停止，请重新提问')
        if h.token_bound(payload) >= old_size:
            raise ContextLimitError('模型上下文超限，已无法在保留最新原文的同时进一步缩减；本轮已停止')
        snapshot.payload = payload
        snapshot.references = []
        snapshot.timings = {**snapshot.timings,**trace,'reactive_retry_count':1,'active_context_digest':'retry-preparing'}
        snapshot.expires_at = now()+timedelta(minutes=5)
        db.flush(); validate(db, run_id, fresh_actor)
        new_ref = issue_token(db, fresh_actor, 'tool', run_id, workspace=run.workspace)
        bind_token(db, run_id, new_ref)
        return new_ref


def validate(db, run_id, actor=None):
    if not run_id:return None
    snapshot = db.get(MemoryRunContext, run_id)
    if not snapshot:
        return None  # legacy non-chat workflow credentials
    prefs = preferences(db, snapshot.user_id)
    if actor is None:
        session = db.get(ChatSession, snapshot.session_id)
        actor = scoped_actor(db, db.get(User, snapshot.user_id), session.shop_id)
    if (snapshot.status != 'active' or snapshot.epoch != prefs.epoch or snapshot.expires_at <= now()
            or snapshot.scope_fingerprint != scope_fingerprint(actor)
            or snapshot.timings.get('read_epoch', prefs.read_epoch) != prefs.read_epoch):
        raise HTTPException(409, '记忆或授权已变化，本轮已停止，请重新提问')
    history_ids = snapshot.timings.get('history_sources', [])
    if history_ids:
        visible = db.scalar(select(func.count()).select_from(ChatRun).where(
            ChatRun.id.in_(history_ids), ChatRun.user_id == snapshot.user_id,
            ChatRun.session_id == snapshot.session_id, ChatRun.memory_excluded.is_(False)))
        if visible != len(set(history_ids)):
            raise HTTPException(409, '历史来源已失效，本轮已停止，请重新提问')
    from .service import get_item
    for ref in snapshot.references:
        try:
            item=get_item(db,actor,ref['id'],db.get(ChatSession,snapshot.session_id).workspace)
            version=effective_version(db,item)
            if item.status not in ('active','cancelled') or not version or version.id!=ref['version_id']:
                raise HTTPException(409,'记忆版本已变化')
        except HTTPException:
            raise HTTPException(409,'记忆或授权已变化，本轮已停止，请重新提问')
    return snapshot
