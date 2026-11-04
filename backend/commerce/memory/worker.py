"""Independent leased worker; model requests run outside database transactions."""
import asyncio
import json
import logging
import secrets
import time
from datetime import timedelta
from sqlalchemy import or_, select, text
from ..auth import scoped_actor, scope_fingerprint
from ..config import settings
from ..db import SessionLocal, initialize_schema
from ..models import User, ChatRun, Audit, now
from .models import (MemoryJob, MemoryChunk, MemoryVersion, MemoryItem, ChatSession,
                      MemoryLink, MemoryRunContext, ContextSegment)
from .policy import preferences
from .schemas import MemoryCreate
from . import service, providers, retrieval
from . import embeddings
from . import hierarchy
from .indexes import entities_for

log = logging.getLogger('commerce.memory')
OWNER = secrets.token_hex(16)


def reconcile_embeddings(user_id=None, limit=200):
    """Backfill existing facts after API configuration/model/epoch changes. Never reanimate forgotten content."""
    if not embeddings.configured():
        return 0
    model = embeddings.identity()
    with SessionLocal() as db:
        if not db.scalar(text("SELECT to_regclass('memory_vectors') IS NOT NULL")):
            return 0
        rows = db.execute(text('''SELECT v.id, v.actor_id, p.epoch FROM memory_chunks c
            JOIN memory_versions v ON v.id=c.version_id JOIN memory_items i ON i.id=c.item_id
            JOIN memory_settings p ON p.user_id=v.actor_id
            LEFT JOIN memory_vectors x ON x.chunk_id=c.id
            WHERE i.status!='forgotten' AND (v.valid_to IS NULL OR v.valid_to>now())
              AND (i.expires_at IS NULL OR i.expires_at>now())
              AND (CAST(:user_id AS varchar) IS NULL OR v.actor_id=CAST(:user_id AS varchar))
              AND (x.chunk_id IS NULL OR x.model!=:model)
              AND NOT EXISTS (SELECT 1 FROM memory_jobs j WHERE j.dedup_key=
                  'embed:' || v.id || ':' || :model || ':' || p.epoch::text)
            ORDER BY v.recorded_at LIMIT :limit'''),
            {'model':model,'user_id':user_id,'limit':limit}).all()
        for row in rows:
            service.enqueue(db,row.actor_id,'embed',row.id,row.epoch)
        # Old configuration/epoch jobs must not consume retries or display as pending forever.
        for job in db.scalars(select(MemoryJob).where(MemoryJob.kind=='embed',
                MemoryJob.state.in_(['queued','retry','waiting','unconfigured']))):
            if user_id and job.user_id!=user_id:continue
            if job.epoch!=preferences(db,job.user_id).epoch or f':{model}:' not in job.dedup_key:
                job.state='obsolete';job.error=None
        db.commit()
        return len(rows)


def claim(user_id=None):
    with SessionLocal() as db:
        stmt=select(MemoryJob).where(MemoryJob.next_at <= now(), or_(
            MemoryJob.state.in_(['queued','retry']),
            (MemoryJob.state == 'running') & (MemoryJob.lease_until < now())))
        if user_id:stmt=stmt.where(MemoryJob.user_id==user_id)
        job = db.scalar(stmt.order_by(MemoryJob.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return None
        job.state='running'; job.lease_owner=OWNER; job.lease_until=now()+timedelta(minutes=3); job.attempts+=1
        db.commit()
        return {k: getattr(job,k) for k in ['id','kind','source_id','user_id','epoch','attempts']}


def source(db, job):
    if not settings.memory_enabled:
        return None
    prefs = preferences(db, job['user_id'])
    if prefs.epoch != job['epoch']:
        return None
    if not service.job_enabled(prefs, job['kind']):
        return None
    if job['kind']=='receipt':
        record=db.get(Audit,job['source_id'])
        if not record or record.user_id!=job['user_id']:return None
        actor=scoped_actor(db,db.get(User,job['user_id']),record.details.get('shop_id'))
        if record.details.get('data_scope')!=scope_fingerprint(actor):return None
        return {'operation':record.operation,'details':record.details,'shop_id':actor.shop_id}
    if job['kind'] == 'embed':
        version = db.get(MemoryVersion, job['source_id'])
        item = db.get(MemoryItem, version.item_id) if version else None
        if not item or item.status == 'forgotten':
            return None
        return {'text': version.content}
    run = db.get(ChatRun, job['source_id'])
    if not run or run.memory_excluded or not run.session_id:
        return None
    actor = scoped_actor(db, db.get(User, run.user_id), run.shop_id)
    if run.scope_fingerprint != scope_fingerprint(actor):
        return None
    snap = db.get(MemoryRunContext, run.id)
    if snap and snap.timings.get('temporary'):
        return None
    session = db.get(ChatSession, run.session_id)
    if job['kind'] == 'compress':
        return hierarchy.compression_source(db, session, run.turn_seq)
    # Extraction is per turn; compression has a separate queue and independent blocks.
    return {'prior_summary': '', 'through_seq': 0,
        'turns': [{'source_id': run.id, 'user': run.query, 'assistant': run.answer[:1800],
                   'status': run.status, 'effects': run.effects}],
        'reflection': prefs.reflection_enabled, 'last_seq': run.turn_seq}


def execute(job):
    with SessionLocal() as db:
        data = source(db, job)
    if data is None:
        return 'obsolete'
    if job['kind']=='receipt':
        result=data
    elif job['kind'] == 'embed':
        model_identity = embeddings.identity()
        vector = retrieval.embedding(data['text'], timeout=settings.memory_embedding_index_timeout)
        if vector is None:
            return 'waiting'
        result = vector
    else:
        result = providers.maintain('compress_segment' if job['kind'] == 'compress' else 'extract_memories', data)
    with SessionLocal() as db:
        # Serialize with explicit edits/forget and check the lease before persisting results.
        prefs = preferences(db, job['user_id'], lock=True)
        stored = db.get(MemoryJob, job['id'], populate_existing=True)
        current = source(db, job)
        if not stored or stored.lease_owner != OWNER or stored.state != 'running' or stored.lease_until <= now() or current is None:
            return 'obsolete'
        if job['kind']=='receipt':
            actor=scoped_actor(db,db.get(User,job['user_id']),result['shop_id'])
            body=MemoryCreate(kind='episodic',scope='personal_shop' if actor.shop_id else 'workspace',
                workspace='today',subject=job['source_id'],predicate='business_event',
                title='业务操作记录：'+result['operation'],content=json.dumps(result,ensure_ascii=False))
            service.create(db,actor,body,explicit=False,pipeline='business-receipt-v1',
                source={'type':'audit','id':job['source_id'],'role':'tool','quote':''})
        elif job['kind'] == 'embed':
            if model_identity != embeddings.identity():
                return 'obsolete'
            chunk = db.scalar(select(MemoryChunk).where(MemoryChunk.version_id == job['source_id']))
            if chunk:
                db.execute(text('''INSERT INTO memory_vectors(chunk_id,model,embedding)
                    VALUES (:id,:model,CAST(:vector AS public.vector)) ON CONFLICT(chunk_id)
                    DO UPDATE SET model=EXCLUDED.model,embedding=EXCLUDED.embedding'''),
                    {'id':chunk.id,'model':model_identity,'vector':json.dumps(result)})
                chunk.embedding_state='ready'
        elif job['kind'] == 'compress':
            if current['source_ids'] != data['source_ids'] or current['session_epoch'] != data['session_epoch']:
                return 'obsolete'
            if not result.summary.strip() or not result.overview.strip():
                raise ValueError('分段摘要和基于原文生成的概要不能为空')
            for content in (result.summary, result.overview, result.title):
                service.validate_content(content)
            session = db.get(ChatSession, data['session_id'])
            segment = ContextSegment(session_id=session.id, epoch=session.epoch,
                start_seq=data['start_seq'], end_seq=data['last_seq'], source_ids=data['source_ids'],
                title=result.title or '第%d至%d轮' % (data['start_seq'], data['last_seq']),
                summary=result.summary, overview=result.overview,
                key_points=data['key_points'], entities=entities_for(result.title+' '+result.overview+' '+
                    ' '.join(t['user'] for t in data['turns'])))
            db.add(segment); db.flush()
            # Follow-up jobs drain legacy/backlogged histories one independent block at a time.
            if hierarchy.compression_source(db, session, db.get(ChatRun, job['source_id']).turn_seq):
                service.enqueue(db, job['user_id'], 'compress', job['source_id'], prefs.epoch,
                    suffix='after-' + str(data['last_seq']))
        else:
            run = db.get(ChatRun, job['source_id'])
            actor = scoped_actor(db, db.get(User,run.user_id),run.shop_id)
            allowed = {t['source_id']:t for t in data['turns']}
            created = []
            for candidate in result.memories:
                evidence = allowed.get(candidate.source_id)
                if not evidence or candidate.quote not in evidence['user']:
                    continue
                if candidate.kind not in ('semantic','decision'):
                    continue
                body = MemoryCreate(kind=candidate.kind, scope='workspace', workspace=run.workspace,
                    subject=candidate.subject,predicate=candidate.predicate,title=candidate.title,content=candidate.content)
                item = service.create(db,actor,body,candidate=True,explicit=False,pipeline='memory-maintenance-v1',
                    source={'type':'chat','id':candidate.source_id,'role':'user','quote':candidate.quote})
                if item: created.append(item)
            # Actual receipts are evidence even when the overall model response failed.
            for turn in data['turns']:
                for effect in turn['effects']:
                    body = MemoryCreate(kind='episodic',scope='workspace',workspace=run.workspace,
                        subject=turn['source_id'],predicate='effect:'+effect['operation'], title=effect['label'],
                        content=json.dumps(effect,ensure_ascii=False))
                    item=service.create(db,actor,body,explicit=False,pipeline='receipt-v1',
                        source={'type':'chat','id':turn['source_id'],'role':'tool','quote':''})
                    if item: created.append(item)
            if data['reflection'] and created:
                for n,lesson in enumerate(result.lessons):
                    if not lesson.strip(): continue
                    body=MemoryCreate(kind='procedural',scope='workspace',workspace=run.workspace,
                        subject=run.id,predicate='lesson:'+str(n),title='经验候选（待验证）',content=lesson[:2000])
                    item=service.create(db,actor,body,candidate=True,explicit=False,pipeline='reflection-v1',
                        source={'type':'chat','id':run.id,'role':'inference','quote':''})
                    if item:
                        for parent in created:
                            db.merge(MemoryLink(parent_id=parent.id,child_id=item.id))
        db.commit()
    return 'succeeded'


def tick():
    job=claim()
    if not job: return False
    error=None
    try:
        state=execute(job)
    except Exception as exc:
        log.warning('Memory job %s failed (%s)',job['id'],type(exc).__name__)
        state='dead' if job['attempts']>=4 else 'retry'
        error='记忆整理暂未完成：'+type(exc).__name__
    with SessionLocal() as db:
        row=db.get(MemoryJob,job['id'])
        if row and row.lease_owner==OWNER:
            row.state=state; row.error=error; row.lease_until=None
            if state=='waiting':row.attempts=max(0,row.attempts-1)
            row.next_at=now()+timedelta(seconds=min(300,5*2**job['attempts']))
            db.commit()
    return True


async def run():
    initialize_schema()
    reconciled_at = 0
    while True:
        try:
            if time.monotonic()-reconciled_at > 30:
                await asyncio.to_thread(reconcile_embeddings)
                reconciled_at=time.monotonic()
            worked=await asyncio.to_thread(tick)
            # Context payloads contain private history: expire the copy, retain only IDs/timings.
            with SessionLocal() as db:
                for snap in db.scalars(select(MemoryRunContext).where(MemoryRunContext.expires_at<now(),MemoryRunContext.payload!='')):
                    snap.payload='';snap.status='expired'
                db.commit()
        except Exception:
            log.exception('Memory worker tick failed');worked=False
        await asyncio.sleep(.2 if worked else 3)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
