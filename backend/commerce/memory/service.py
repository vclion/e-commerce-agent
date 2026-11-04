import json
import re
from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import delete, select, update, or_
from sqlalchemy.dialects.postgresql import insert
from .. import business as b
from ..auth import digest
from ..models import ChatRun, now
from .models import (ChatSession, MemoryItem, MemorySpace, MemoryVersion, MemoryEvidence,
                     MemoryChunk, MemoryJob, MemoryLink, MemorySuppression, MemoryRunContext, ContextSegment)
from .policy import preferences, space_for, allowed_spaces, source_visible
from .schemas import MemoryCreate, MemoryUpdate


def job_enabled(prefs, kind):
    if kind == 'compress':
        return prefs.context_index_enabled
    if kind in ('extract', 'receipt'):
        return prefs.auto_write
    return True  # Explicitly saved memories can still be embedded.


def enqueue(db, user_id, kind, source_id, epoch, suffix='v1'):
    if not job_enabled(preferences(db, user_id), kind):
        return
    if kind == 'embed':
        from .embeddings import identity
        suffix = identity()
    key = f'{kind}:{source_id}:{suffix}:{epoch}'
    db.execute(insert(MemoryJob).values(user_id=user_id, kind=kind, source_id=source_id,
        epoch=epoch, dedup_key=key, state='queued', attempts=0, next_at=now(), created_at=now())
        .on_conflict_do_nothing(index_elements=['dedup_key']))


def logical_key(subject, predicate):
    return digest(subject.strip().casefold() + '\0' + predicate.strip().casefold())


def validate_content(content):
    if re.search(r'(?i)(?:api[_ -]?key|access[_ -]?token|secret|password|密码|密钥)\s*[:：=]\s*\S+',content):
        raise HTTPException(422,'请将凭据保存在平台配置中，记忆不保存密码或密钥')


def effective_version(db, item, at=None):
    at = at or now()
    return db.scalar(select(MemoryVersion).where(MemoryVersion.item_id == item.id,
        MemoryVersion.valid_from <= at,
        (MemoryVersion.valid_to.is_(None) | (MemoryVersion.valid_to > at)))
        .order_by(MemoryVersion.revision.desc()).limit(1))


def get_item(db, actor, item_id, workspace=None, manage=False, lock=False):
    spaces = {s.id for s in allowed_spaces(db, actor, workspace, manage=manage)}
    row = (db.scalar(select(MemoryItem).where(MemoryItem.id==item_id).with_for_update()
                     .execution_options(populate_existing=True)) if lock else db.get(MemoryItem, item_id))
    if not row or row.space_id not in spaces or row.status == 'forgotten' or not source_visible(db, actor, row, workspace):
        raise HTTPException(404, '记忆不存在或不可访问')
    return row


def item_view(db, actor, row, detail=False):
    from .models import MemoryGrant
    space = db.get(MemorySpace, row.space_id)
    grant = db.get(MemoryGrant,(space.id,actor.id))
    version = effective_version(db, row)
    latest = db.scalar(select(MemoryVersion).where(MemoryVersion.item_id == row.id)
                       .order_by(MemoryVersion.revision.desc()).limit(1))
    status = 'expired' if row.expires_at and row.expires_at <= now() else row.status
    result = {k: getattr(row, k) for k in ['id', 'kind', 'subject', 'predicate', 'title', 'revision', 'source_level', 'created_at', 'updated_at', 'expires_at']}
    result.update(status=status, content=version.content if version else '',
        current_revision=version.revision if version else None, scope=space.kind,
        shop_ids=list(space.required_shops), workspace=space.workspace,
        scheduled_content=latest.content if latest and latest.valid_from > now() else None,
        valid_from=latest.valid_from if latest else None,
        can_manage=space.owner_id==actor.id or bool(grant and grant.can_manage),
        can_publish=space.kind!='shop_shared' and bool(publish_target(db,actor)) and not db.scalar(
            select(MemoryEvidence.id).where(MemoryEvidence.item_id==row.id,MemoryEvidence.source_type!='manual').limit(1)))
    if detail:
        result['versions'] = [{k: getattr(v, k) for k in ['id', 'revision', 'content', 'valid_from', 'valid_to', 'recorded_at', 'reason', 'pipeline_version']}
                              for v in db.scalars(select(MemoryVersion).where(MemoryVersion.item_id == row.id).order_by(MemoryVersion.revision.desc()))]
        result['evidence'] = [{'source_type': e.source_type, 'source_id': e.source_id,
            'source_role': e.source_role, 'quote': e.quote if e.source_type == 'manual' else '',
            'note': '原始对话仅在原栏目查看' if e.source_type == 'chat' else '显式保存'}
            for e in db.scalars(select(MemoryEvidence).where(MemoryEvidence.item_id == row.id))]
    return result


def list_items(db, actor, workspace=None, status=None):
    spaces = [s.id for s in allowed_spaces(db, actor, workspace)]
    stmt = select(MemoryItem).where(MemoryItem.space_id.in_(spaces), MemoryItem.status != 'forgotten')
    if status:
        stmt = stmt.where(MemoryItem.status == status)
    rows = list(db.scalars(stmt.order_by(MemoryItem.updated_at.desc()).limit(500)))
    return [item_view(db, actor, r) for r in rows if source_visible(db, actor, r, workspace)]


def add_version(db, actor, row, content, valid_from=None, reason='明确保存', pipeline='manual-v1', source=None):
    valid_from = valid_from or now()
    prior = db.scalar(select(MemoryVersion).where(MemoryVersion.item_id == row.id)
        .order_by(MemoryVersion.revision.desc()).limit(1))
    if prior and valid_from < prior.valid_from:
        raise HTTPException(409, '存在更晚生效版本，请先处理该版本再设置更早的生效时间')
    if prior:
        prior.valid_to = valid_from
    v = MemoryVersion(item_id=row.id, revision=row.revision, content=content, valid_from=valid_from,
        actor_id=actor.id, reason=reason, pipeline_version=pipeline)
    db.add(v); db.flush()
    from .retrieval import tokenize
    db.add(MemoryChunk(item_id=row.id, version_id=v.id, content=content, tokens=tokenize(row.title + ' ' + content)))
    source = source or {'type': 'manual', 'id': row.id, 'role': 'user', 'quote': content}
    db.add(MemoryEvidence(item_id=row.id, version_id=v.id, source_type=source['type'], source_id=source['id'],
        source_role=source['role'], quote=source.get('quote', '')[:500], fingerprint=digest(content)))
    enqueue(db, actor.id, 'embed', v.id, preferences(db, actor.id).epoch)
    return v


def create(db, actor, body: MemoryCreate, *, source=None, candidate=False, pipeline='manual-v1', explicit=True, target_space=None):
    validate_content(body.content)
    prefs = preferences(db, actor.id, lock=True)
    if not explicit and not prefs.auto_write:
        return None
    space = target_space or space_for(db, actor, body.scope, body.workspace)
    key = logical_key(body.subject, body.predicate)
    suppression = f'item:{space.id}:{key}'
    if db.scalar(select(MemorySuppression.id).where(MemorySuppression.user_id == actor.id, MemorySuppression.key == suppression)):
        if not explicit:
            return None
        db.execute(delete(MemorySuppression).where(MemorySuppression.user_id == actor.id, MemorySuppression.key == suppression))
    row = db.scalar(select(MemoryItem).where(MemoryItem.space_id == space.id, MemoryItem.logical_key == key).with_for_update())
    if row and row.status != 'forgotten':
        current = effective_version(db, row)
        if current and current.content == body.content:
            return row
        # Background inference cannot overwrite a user's explicit setting.
        if not explicit:
            return None
        raise HTTPException(409, '该主题已有记忆，请读取版本后纠正，避免覆盖')
    if not row:
        row = MemoryItem(space_id=space.id, logical_key=key, kind=body.kind, subject=body.subject,
            predicate=body.predicate, title=body.title, status='candidate' if candidate else 'active',
            source_level='inferred' if candidate else 'explicit', expires_at=body.expires_at)
        db.add(row); db.flush()
    else:
        row.revision += 1; row.status = 'candidate' if candidate else 'active'
        row.title, row.subject, row.predicate = body.title, body.subject, body.predicate
        row.expires_at = body.expires_at
        row.source_level = 'inferred' if candidate else 'explicit'
    if body.expires_at and body.expires_at <= (body.valid_from or now()):
        raise HTTPException(422, '失效时间必须晚于生效时间')
    add_version(db, actor, row, body.content, body.valid_from, pipeline=pipeline, source=source)
    space.epoch += 1
    b.audit(db, actor, 'memory.created', {'memory_id': row.id, 'revision': row.revision})
    return row


def revise(db, actor, item_id, body: MemoryUpdate):
    validate_content(body.content)
    preferences(db, actor.id, lock=True)
    row = get_item(db, actor, item_id, manage=True, lock=True)
    if row.revision != body.expected_revision:
        raise HTTPException(409, '记忆版本已变化，请刷新后重试')
    row.revision += 1; row.updated_at = now(); row.status = body.status
    if body.title:
        row.title = body.title
    row.expires_at = body.expires_at
    if row.expires_at and row.expires_at <= (body.valid_from or now()):
        raise HTTPException(422, '失效时间必须晚于生效时间')
    invalidate(db, actor.id)
    add_version(db, actor, row, body.content, body.valid_from, reason='用户纠正')
    db.get(MemorySpace, row.space_id).epoch += 1
    b.audit(db, actor, 'memory.updated', {'memory_id': row.id, 'revision': row.revision})
    return row


def invalidate(db, user_id):
    prefs = preferences(db, user_id)
    prefs.epoch += 1
    db.execute(update(MemoryRunContext).where(MemoryRunContext.user_id == user_id).values(status='invalid', payload=''))
    for session in db.scalars(select(ChatSession).where(ChatSession.user_id == user_id)):
        db.execute(delete(ContextSegment).where(ContextSegment.session_id == session.id))
        session.epoch += 1; session.summary = ''; session.summary_sources = []; session.through_seq = 0; session.upstream_id = None


def suppress(db, user_id, key):
    db.execute(insert(MemorySuppression).values(user_id=user_id, key=key).on_conflict_do_nothing(index_elements=['user_id', 'key']))


def forget(db, actor, ids):
    preferences(db, actor.id, lock=True)
    rows = [get_item(db, actor, item_id, manage=True,lock=True) for item_id in sorted(set(ids))]
    pending, visited = list(rows), set()
    while pending:
        row = pending.pop()
        if row.id in visited:
            continue
        visited.add(row.id)
        for child in db.scalars(select(MemoryItem).join(MemoryLink, MemoryLink.child_id == MemoryItem.id).where(MemoryLink.parent_id == row.id)):
            pending.append(child)
        suppress(db, actor.id, f'item:{row.space_id}:{row.logical_key}')
        for ev in db.scalars(select(MemoryEvidence).where(MemoryEvidence.item_id == row.id)):
            if ev.source_type == 'chat':
                run = db.get(ChatRun, ev.source_id)
                if run:
                    run.memory_excluded = True
                suppress(db, actor.id, f'chat:{ev.source_id}')
            ev.quote = ''; ev.fingerprint = ''
        # Public vectors cascade from chunks. Version content is also erased, not just hidden.
        db.execute(delete(MemoryChunk).where(MemoryChunk.item_id == row.id))
        db.execute(update(MemoryVersion).where(MemoryVersion.item_id == row.id).values(content=''))
        row.title = ''; row.subject = ''; row.predicate = ''; row.status = 'forgotten'; row.revision += 1
        db.get(MemorySpace, row.space_id).epoch += 1
        b.audit(db, actor, 'memory.forgotten', {'memory_id': row.id})
    # A previous answer/summary may repeat a fact. Invalidate its entire dependency chain.
    snapshots={s.run_id:s for s in db.scalars(select(MemoryRunContext).where(or_(MemoryRunContext.user_id==actor.id,
        *[MemoryRunContext.references.contains([{'id':item_id}]) for item_id in visited])))}
    blocked=set(db.scalars(select(ChatRun.id).where(ChatRun.user_id==actor.id,ChatRun.memory_excluded.is_(True))))
    changed=True
    while changed:
        changed=False
        if blocked:
            for snap in db.scalars(select(MemoryRunContext).where(or_(
                    *[MemoryRunContext.timings['history_sources'].contains([rid]) for rid in blocked]))):
                snapshots[snap.run_id]=snap
        for snap in snapshots.values():
            if snap.run_id in blocked:continue
            if any(r['id'] in visited for r in snap.references) or blocked.intersection(snap.timings.get('history_sources',[])):
                blocked.add(snap.run_id);changed=True
    if blocked:
        db.execute(update(ChatRun).where(ChatRun.id.in_(blocked)).values(memory_excluded=True))
    affected_users={actor.id}
    for snap in snapshots.values():
        if snap.run_id in blocked:
            snap.status='invalid';snap.payload=''
            affected_users.add(snap.user_id)
        snap.references=[r for r in snap.references if r['id'] not in visited]
    for user_id in sorted(affected_users):
        invalidate(db, user_id)
    return {'ok': True, 'forgotten': len(visited), 'history_retained': True,
        'notice': '已停止使用并清除相关记忆及索引；原始聊天仍保留，相关来源不再进入后续上下文。'}


def publish_target(db,actor):
    from .models import MemoryGrant
    if not actor.shop_id:return None
    targets=[s for s in allowed_spaces(db,actor) if s.kind=='shop_shared' and s.shop_id==actor.shop_id]
    return next((s for s in targets if s.owner_id==actor.id or (db.get(MemoryGrant,(s.id,actor.id)) and db.get(MemoryGrant,(s.id,actor.id)).can_publish)),None)


def publish(db,actor,item_id,expected_revision):
    preferences(db,actor.id,lock=True)
    source=get_item(db,actor,item_id,manage=True,lock=True)
    if source.revision!=expected_revision:raise HTTPException(409,'记忆版本已变化')
    if not actor.shop_id:raise HTTPException(422,'请选择共享的目标店铺')
    if any(e.source_type!='manual' for e in db.scalars(select(MemoryEvidence).where(MemoryEvidence.item_id==source.id))):
        raise HTTPException(403,'聊天与业务来源不能自动发布；请先独立编写可共享的明确约定')
    target=publish_target(db,actor)
    if not target:raise HTTPException(403,'当前账号没有发布店铺共享记忆的授权')
    version=effective_version(db,source)
    if not version:raise HTTPException(422,'该记忆尚未生效')
    body=MemoryCreate(kind=source.kind,subject=source.subject,predicate=source.predicate,title=source.title,
        content=version.content,scope='personal_shop')
    result=create(db,actor,body,target_space=target)
    db.merge(MemoryLink(parent_id=source.id,child_id=result.id))
    b.audit(db,actor,'memory.published',{'memory_id':result.id,'source_id':source.id})
    return result
