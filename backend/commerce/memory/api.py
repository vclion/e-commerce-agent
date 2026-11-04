import secrets
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.orm import Session
from ..auth import current_user, resolve_token, scoped_actor
from ..config import settings
from ..db import get_db
from ..models import ChatRun, now
from . import service, context, retrieval, embeddings
from .models import MemorySettings, MemoryJob, MemoryRunContext, ChatSession
from .policy import preferences
from .schemas import MemoryCreate, MemoryUpdate, Forget, Preferences, MemorySearch
from ..schemas import Strict


class Publish(Strict):
    expected_revision:int

router = APIRouter()


@router.get('/api/memory/settings')
def get_settings(actor=Depends(current_user), db: Session=Depends(get_db)):
    row = preferences(db, actor.id)
    db.commit()
    counts = dict(db.execute(select(MemoryJob.state,func.count()).where(MemoryJob.user_id==actor.id)
                            .group_by(MemoryJob.state)).all())
    return {k: getattr(row, k) for k in ['read_enabled','auto_write','context_index_enabled','reflection_enabled','epoch']} | {
        'semantic_enabled': settings.memory_embedding_enabled, 'semantic_configured': embeddings.configured(),
        'background_pending': any(counts.get(k,0) for k in ['queued','running','retry']),
        'background_failed': counts.get('dead',0), 'background_waiting':counts.get('waiting',0)+counts.get('unconfigured',0)}


@router.post('/api/memory/jobs/retry')
def retry_jobs(actor=Depends(current_user), db:Session=Depends(get_db)):
    prefs=preferences(db,actor.id,lock=True)
    rows=list(db.scalars(select(MemoryJob).where(MemoryJob.user_id==actor.id,MemoryJob.state=='dead')
                         .with_for_update()))
    for row in rows:
        if not service.job_enabled(prefs, row.kind):
            row.state='obsolete';row.error=None
            continue
        row.state='queued';row.attempts=0;row.next_at=now();row.error=None;row.epoch=prefs.epoch
        row.lease_owner=None;row.lease_until=None
    db.commit()
    return {'queued':sum(row.state=='queued' for row in rows)}


@router.patch('/api/memory/settings')
def save_settings(body: Preferences, actor=Depends(current_user), db: Session=Depends(get_db)):
    row = preferences(db, actor.id, lock=True)
    changed = {k: v for k, v in body.model_dump(exclude_unset=True, exclude_none=True).items()
               if getattr(row, k) != v}
    for k, v in changed.items():
        setattr(row, k, v)
    # Read policy changes fence snapshots without deleting indexes or cancelling background work.
    if 'read_enabled' in changed:
        row.read_epoch += 1
        db.execute(update(MemoryRunContext).where(MemoryRunContext.user_id==actor.id)
                   .values(status='invalid', payload=''))
        db.execute(update(ChatSession).where(ChatSession.user_id==actor.id).values(upstream_id=None))
    kinds = set()
    if 'auto_write' in changed:
        kinds.update(('extract', 'receipt'))
    if 'context_index_enabled' in changed:
        kinds.add('compress')
    if 'reflection_enabled' in changed:
        kinds.add('extract')
    if kinds:
        # Also fence jobs already inside a model call, including an off/on transition.
        db.execute(update(MemoryJob).where(MemoryJob.user_id==actor.id, MemoryJob.kind.in_(kinds),
            MemoryJob.state.in_(['queued','running','retry','waiting','unconfigured','dead']))
            .values(state='obsolete', error=None))
    db.commit()
    return get_settings(actor, db)


@router.get('/api/memory/items')
def items(workspace: str | None=None, status: str | None=None, actor=Depends(current_user), db: Session=Depends(get_db)):
    return {'items': service.list_items(db, actor, workspace, status)}


@router.get('/api/memory/items/{item_id}')
def detail(item_id: str, actor=Depends(current_user), db: Session=Depends(get_db)):
    return service.item_view(db, actor, service.get_item(db, actor, item_id), True)


@router.post('/api/memory/items')
def create(body: MemoryCreate, actor=Depends(current_user), db: Session=Depends(get_db)):
    row = service.create(db, actor, body)
    db.commit()
    return service.item_view(db, actor, row, True)


@router.patch('/api/memory/items/{item_id}')
def revise(item_id: str, body: MemoryUpdate, actor=Depends(current_user), db: Session=Depends(get_db)):
    row = service.revise(db, actor, item_id, body)
    db.commit()
    return service.item_view(db, actor, row, True)


@router.post('/api/memory/forget')
def forget(body: Forget, actor=Depends(current_user), db: Session=Depends(get_db)):
    result = service.forget(db, actor, body.item_ids)
    db.commit()
    return result


@router.post('/api/memory/items/{item_id}/publish')
def publish(item_id:str,body:Publish,actor=Depends(current_user),db:Session=Depends(get_db)):
    row=service.publish(db,actor,item_id,body.expected_revision)
    db.commit()
    return service.item_view(db,actor,row)


@router.post('/api/memory/search')
def search(body: MemorySearch, actor=Depends(current_user), db: Session=Depends(get_db)):
    if not preferences(db, actor.id).read_enabled:
        return {'items': [], 'status': 'disabled'}
    return retrieval.search(db, actor, body.query, body.workspace, body.limit)


@router.post('/internal/agent-context/bootstrap')
def bootstrap(authorization: str=Header(''), x_context_ref: str=Header(''), db: Session=Depends(get_db)):
    if not secrets.compare_digest(authorization, f'Bearer {settings.tool_service_key}'):
        raise HTTPException(401, '工具服务认证失败')
    user, token = resolve_token(db, x_context_ref, 'tool')
    actor = scoped_actor(db, user, token.shop_id)
    snapshot = context.validate_token(db, token)
    if not snapshot:
        raise HTTPException(404, '本轮上下文不存在')
    return {'context': snapshot.payload, 'snapshot_id': snapshot.run_id,
        'memory_status': snapshot.timings.get('memory_status'), 'epoch': snapshot.epoch}


@router.get('/api/chat/runs/{run_id}/memory')
def trace(run_id: str, actor=Depends(current_user), db: Session=Depends(get_db)):
    from .models import MemoryRunContext
    run = db.get(ChatRun, run_id)
    snap = db.get(MemoryRunContext, run_id)
    if not run or run.user_id != actor.id or not snap:
        raise HTTPException(404, '记录不存在或不可访问')
    from ..auth import scope_fingerprint
    if snap.scope_fingerprint != scope_fingerprint(actor):
        raise HTTPException(404, '记录不存在或不可访问')
    refs = []
    if snap.status == 'active':
        for ref in snap.references:
            try:
                service.get_item(db, actor, ref['id'], run.workspace)
                refs.append(ref)
            except HTTPException:
                pass
    return {'run_id': run_id, 'status': snap.status, 'references': refs, 'timings': snap.timings}
