"""Independent background policies, retained indexes, and in-flight setting changes."""
from datetime import timedelta
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from commerce.db import SessionLocal
from commerce.models import User, ChatRun, Audit, now
from commerce.auth import scoped_actor
from commerce.memory import worker, providers, service, context, hierarchy
from commerce.memory.models import MemoryItem, MemorySpace, MemoryJob, ContextSegment, ChatSession
from commerce.memory.policy import preferences
from test_memory import person, model, ask, save
from test_tiered_context import history, tool_headers
from test_indexed_context import index_for, read, snapshot_for


def patch(client, headers, **values):
    response = client.patch('/api/memory/settings', headers=headers, json=values)
    assert response.status_code == 200, response.text
    return response.json()


def lease(name, kind, rid):
    with SessionLocal() as db:
        row = db.scalar(select(MemoryJob).where(MemoryJob.user_id==name,
            MemoryJob.kind==kind, MemoryJob.source_id==rid, MemoryJob.state=='queued'))
        assert row
        row.state='running'; row.lease_owner=worker.OWNER
        row.lease_until=now()+timedelta(minutes=3); row.attempts=1
        db.commit()
        return {k:getattr(row,k) for k in ['id','kind','source_id','user_id','epoch','attempts']}


def maintenance(calls):
    def run(kind, data):
        calls.append(kind)
        if kind=='compress_segment':
            return providers.MaintenanceResult(title='历史活动', summary='历史活动的原文索引', overview='活动安排待确认')
        turn=data['turns'][0]
        return providers.MaintenanceResult(memories=[providers.Extracted(source_id=turn['source_id'],
            quote=turn['user'], subject='answer_style', predicate='brevity', title='回答简洁', content='请保持简洁回答')])
    return run


@pytest.mark.parametrize('auto_write,indexing', [(True,True),(False,True),(True,False),(False,False)])
def test_independent_queue_and_worker_results(client, person, model, monkeypatch, auto_write, indexing):
    name, headers=person
    prefs=patch(client, headers, auto_write=auto_write, context_index_enabled=indexing)
    assert prefs['auto_write'] is auto_write and prefs['context_index_enabled'] is indexing
    sid, ids=history(name, count=12, answer_size=1)
    events=ask(client, headers, '请保持简洁回答', session_id=sid)
    rid=events['start']['run_id']; calls=[]
    monkeypatch.setattr(providers, 'maintain', maintenance(calls))
    with SessionLocal() as db:
        kinds=set(db.scalars(select(MemoryJob.kind).where(MemoryJob.source_id==rid)))
        assert kinds==({'extract'} if auto_write else set()) | ({'compress'} if indexing else set())
    for kind, enabled in [('extract',auto_write), ('compress',indexing)]:
        if enabled:
            assert worker.execute(lease(name,kind,rid))=='succeeded'
    with SessionLocal() as db:
        assert bool(db.scalar(select(MemoryItem.id).join(MemorySpace).where(MemorySpace.owner_id==name))) is auto_write
        assert bool(db.scalar(select(ContextSegment.id).where(ContextSegment.session_id==sid))) is indexing
    assert len(calls)==int(auto_write)+int(indexing)
    assert snapshot_for(events).timings['levels']['raw']==3


def test_write_switches_preserve_memories_indexes_and_snapshot(client, person, model):
    name, headers=person
    saved=save(client,headers).json()
    sid, ids=history(name,count=20,answer_size=1)
    idx=index_for(sid,ids[:8],age_days=0)
    events=ask(client,headers,session_id=sid)
    with SessionLocal() as db:
        epoch=preferences(db,name).epoch
        session_epoch=db.get(ChatSession,sid).epoch
    patch(client,headers,auto_write=False,context_index_enabled=False)
    assert client.get('/api/memory/items/'+saved['id'],headers=headers).status_code==200
    with SessionLocal() as db:
        assert db.get(ContextSegment,idx)
        assert preferences(db,name).epoch==epoch and db.get(ChatSession,sid).epoch==session_epoch
        assert context.validate(db,events['start']['run_id'])
    assert read(client,model,index_id=idx).status_code==200
    next_event=ask(client,headers,session_id=sid)
    assert idx in snapshot_for(next_event).timings['selected_indexes']
    assert saved['id'] in [r['id'] for r in snapshot_for(next_event).references]
    # Old three-field clients do not reset a separately disabled indexing preference.
    prefs=patch(client,headers,read_enabled=True,auto_write=True,reflection_enabled=True)
    assert prefs['context_index_enabled'] is False


@pytest.mark.parametrize('kind,setting,other', [
    ('compress','context_index_enabled','auto_write'), ('extract','auto_write','context_index_enabled')])
def test_disabled_inflight_results_cannot_return_after_reenable(client, person, model, monkeypatch, kind, setting, other):
    name, headers=person
    sid, ids=history(name,count=12,answer_size=1)
    events=ask(client,headers,'请保持简洁回答',session_id=sid)
    rid=events['start']['run_id']; job=lease(name,kind,rid)
    calls=[]; original=maintenance(calls)
    def during_call(operation,data):
        patch(client,headers,**{setting:False})
        patch(client,headers,**{setting:True})
        return original(operation,data)
    monkeypatch.setattr(providers,'maintain',during_call)
    assert worker.execute(job)=='obsolete'
    with SessionLocal() as db:
        assert not db.scalar(select(ContextSegment.id).where(ContextSegment.session_id==sid))
        assert not db.scalar(select(MemoryItem.id).join(MemorySpace).where(MemorySpace.owner_id==name))
        sibling='extract' if kind=='compress' else 'compress'
        assert db.scalar(select(MemoryJob.state).where(MemoryJob.source_id==rid,MemoryJob.kind==sibling))=='queued'
        assert getattr(preferences(db,name),other) is True


def test_read_toggle_invalidates_snapshot_but_retains_indexes_and_background_jobs(client,person,model):
    name,headers=person
    saved=save(client,headers).json()
    sid,ids=history(name,count=12,answer_size=1)
    idx=index_for(sid,ids[:8],age_days=0)
    events=ask(client,headers,session_id=sid)
    rid=events['start']['run_id']
    patch(client,headers,read_enabled=False)
    with SessionLocal() as db:
        assert db.get(ContextSegment,idx)
        assert set(db.scalars(select(MemoryJob.state).where(MemoryJob.source_id==rid)))=={'queued'}
        with pytest.raises(HTTPException):context.validate(db,rid)
    next_event=ask(client,headers,session_id=sid)
    assert not snapshot_for(next_event).references
    assert idx in snapshot_for(next_event).timings['selected_indexes']
    assert client.get('/api/memory/items/'+saved['id'],headers=headers).status_code==200


def test_read_off_on_during_build_is_fenced(client,person,model,monkeypatch):
    name,headers=person
    original=hierarchy.assemble
    def changed(*args,**kwargs):
        # Release the session's read transaction before the independent preference updates.
        args[0].commit()
        patch(client,headers,read_enabled=False)
        patch(client,headers,read_enabled=True)
        return original(*args,**kwargs)
    monkeypatch.setattr(hierarchy,'assemble',changed)
    response=client.post('/api/chat',headers=headers,json={'query':'继续','workspace':'today'})
    assert response.status_code==409


def test_reactive_compaction_is_independent_of_background_switches(client,person,model,monkeypatch):
    from commerce.config import settings
    name,headers=person
    patch(client,headers,auto_write=False,context_index_enabled=False)
    sid,ids=history(name,count=3,answer_size=200)
    monkeypatch.setattr(settings,'memory_context_tokens',6000)
    calls=[]
    def maintain(kind,data):
        calls.append(kind)
        return providers.MaintenanceResult(summary='较早讨论活动，最新问题待回答。')
    monkeypatch.setattr(providers,'maintain',maintain)
    events=ask(client,headers,'继续',session_id=sid)
    assert events['done']['status']=='succeeded' and calls==['reactive_context']
    assert snapshot_for(events).timings['levels']['reactive_summary']==1


def test_automatic_receipt_memory_stops_but_audit_and_manual_saves_remain(client,person):
    from commerce.business import audit
    name,headers=person
    patch(client,headers,auto_write=False)
    with SessionLocal() as db:
        actor=scoped_actor(db,db.get(User,name),'a')
        audit(db,actor,'ops.task.update',{'id':'synthetic-settings-check','to':'pending'})
        db.commit()
        assert db.scalar(select(Audit.id).where(Audit.user_id==name))
        assert not db.scalar(select(MemoryJob.id).where(MemoryJob.user_id==name,MemoryJob.kind=='receipt'))
    response=save(client,headers)
    assert response.status_code==200
    with SessionLocal() as db:
        assert db.scalar(select(MemoryJob.id).where(MemoryJob.user_id==name,MemoryJob.kind=='embed'))


@pytest.mark.parametrize('query,explicit', [
    ('回答简洁一点',False), ('分析我的回答偏好',False), ('不要记住我的回答偏好',False),
    ('记住：回答简洁一点',True)])
def test_agent_auto_proposal_respects_switch_but_explicit_save_works(client,person,model,query,explicit):
    name,headers=person
    patch(client,headers,auto_write=False)
    events=ask(client,headers,query)
    with SessionLocal() as db:
        db.get(ChatRun,events['start']['run_id']).status='running';db.commit()
    result=client.post('/tools/execute',headers=tool_headers(model),json={
        'operation':'memory_propose','arguments':{'quote':query,'subject':'style','predicate':'brevity',
            'scope':'workspace','title':'简洁回答','content':'回答简洁一点'}})
    assert result.status_code==200,result.text
    data=result.json()['data']
    if explicit:
        assert data['id'] and data['status']=='active'
    else:
        assert data['status']=='disabled'
        assert not client.get('/api/memory/items',headers=headers).json()['items']
