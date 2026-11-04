"""Lifecycle, source authority, isolation, stale workers and real PostgreSQL vector queries."""
import json
from datetime import timedelta
from uuid import uuid4
import pytest
from sqlalchemy import select, text, update
from commerce.auth import password_hash, scoped_actor, digest
from fastapi import HTTPException
from commerce.config import settings, ROOT
from commerce.db import SessionLocal
from commerce.models import User, Membership, ChatRun, AccessToken, now
from commerce.memory.models import (MemoryItem,MemoryVersion,MemoryChunk,MemoryJob,MemoryRunContext,
                                   ChatSession,MemorySuppression,MemoryEvidence)
from commerce.memory import worker, service, providers, embeddings


@pytest.fixture
def person(client,auth):
    name='memory-'+uuid4().hex[:14]
    with SessionLocal() as db:
        db.add(User(id=name,username=name,role='operator',shop_id='a',display_name='记忆测试',password_hash=password_hash(settings.demo_password)))
        db.flush();db.add_all([Membership(user_id=name,shop_id='a'),Membership(user_id=name,shop_id='b')]);db.commit()
    return name,{**auth(name),'X-Shop-ID':'a'}


@pytest.fixture
def model(monkeypatch):
    from commerce import main
    requests=[]
    class Response:
        status_code=200
        def __init__(self,body):self.body=body
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def aiter_lines(self):
            cid=self.body['conversation_id'] or 'memory-cid-'+uuid4().hex
            for event in [{'event':'message','answer':'这是模型生成的建议，不代表执行成功。'},{'event':'message_end'}]:
                yield 'data: '+json.dumps({**event,'conversation_id':cid})
    class Client:
        def __init__(self,**k):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def stream(self,*a,**k):requests.append(k['json']);return Response(k['json'])
    monkeypatch.setattr(main.httpx,'AsyncClient',Client)
    monkeypatch.setattr(settings,'dify_operator_key','test-memory-placeholder')
    return requests


def save(client,headers,**extra):
    return client.post('/api/memory/items',headers=headers,json={
        'subject':uuid4().hex,'predicate':'report_order','scope':'personal_shop',
        'title':'报告先列异常','content':'先列退款和履约异常，再列行动建议。',**extra})


def ask(client,headers,query='分析今天的工作',**extra):
    r=client.post('/api/chat',headers=headers,json={'query':query,'workspace':'today',**extra})
    assert r.status_code==200,r.text
    events={}
    for frame in r.text.split('\n\n'):
        lines=frame.splitlines()
        if len(lines)>1 and lines[0].startswith('event:'):
            events[lines[0][7:]]=json.loads(lines[1][6:])
    return events


def test_structured_version_cas_and_scope(client,person,auth):
    _,headers=person
    row=save(client,headers).json()
    assert row['revision']==1 and row['content'].startswith('先列')
    assert not client.get('/api/memory/items',headers={**headers,'X-Shop-ID':'b'}).json()['items']
    assert client.get('/api/memory/items/'+row['id'],headers=auth()).status_code==404
    changed=client.patch('/api/memory/items/'+row['id'],headers=headers,json={
        'expected_revision':1,'content':'先列库存异常。'})
    assert changed.status_code==200 and changed.json()['revision']==2
    assert client.patch('/api/memory/items/'+row['id'],headers=headers,json={
        'expected_revision':1,'content':'不能覆盖新版本'}).status_code==409
    assert len(client.get('/api/memory/items/'+row['id'],headers=headers).json()['versions'])==2


def test_future_version_and_disabled_candidates(client,person):
    _,headers=person
    row=save(client,headers).json()
    future=(now()+timedelta(days=1)).isoformat()
    changed=client.patch('/api/memory/items/'+row['id'],headers=headers,json={
        'expected_revision':1,'content':'明天开始采用的约定','valid_from':future}).json()
    assert changed['content']==row['content'] and changed['scheduled_content']=='明天开始采用的约定'
    assert save(client,headers,valid_from='2026-10-20T10:00:00').status_code==422


def test_snapshot_per_turn_and_immutable_tool_credential(client,person,model):
    name,headers=person
    row=save(client,headers).json()
    first=ask(client,headers)
    assert first['memory']['references'][0]['id']==row['id']
    sid=first['session']['id']
    second=ask(client,headers,'接着说',session_id=sid)
    assert first['start']['run_id']!=second['start']['run_id']
    assert model[0]['inputs']['context_ref']!=model[1]['inputs']['context_ref']
    for index,events in enumerate([first,second]):
        with SessionLocal() as db:
            token=db.get(AccessToken,digest(model[index]['inputs']['context_ref']))
            assert token.request_id==events['start']['run_id']
        boot=client.post('/internal/agent-context/bootstrap',headers={
            'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[index]['inputs']['context_ref']})
        assert boot.status_code==200 and '退款和履约异常' in boot.json()['context']
    assert '接着说' not in client.post('/internal/agent-context/bootstrap',headers={
        'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[0]['inputs']['context_ref']}).json()['context']


def test_forget_erases_versions_vectors_and_answer_dependencies(client,person,model):
    name,headers=person
    row=save(client,headers,content='只在这里出现的秘密偏好XYZ').json()
    first=ask(client,headers)
    second=ask(client,headers,'继续',session_id=first['session']['id'])
    result=client.post('/api/memory/forget',headers=headers,json={'item_ids':[row['id']]})
    assert result.status_code==200
    assert client.get('/api/memory/items/'+row['id'],headers=headers).status_code==404
    boot=client.post('/internal/agent-context/bootstrap',headers={
        'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[1]['inputs']['context_ref']})
    assert boot.status_code==409
    third=ask(client,headers,'现在继续',session_id=first['session']['id'])
    assert model[-1]['conversation_id']==''
    with SessionLocal() as db:
        assert all(not v.content for v in db.scalars(select(MemoryVersion).where(MemoryVersion.item_id==row['id'])))
        assert not db.scalar(select(MemoryChunk.id).where(MemoryChunk.item_id==row['id']))
        assert db.get(ChatRun,first['start']['run_id']).memory_excluded
        assert db.get(ChatRun,second['start']['run_id']).memory_excluded
        snap=db.get(MemoryRunContext,third['start']['run_id'])
        assert 'XYZ' not in snap.payload


def test_temporary_conversation_and_read_toggle(client,person,model):
    name,headers=person
    save(client,headers)
    events=ask(client,headers,temporary=True)
    assert not events['memory']['references'] and events['memory']['status']=='temporary'
    with SessionLocal() as db:
        assert db.get(ChatRun,events['start']['run_id']).memory_excluded
        assert not db.scalar(select(MemoryJob.id).where(MemoryJob.source_id==events['start']['run_id']))
    client.patch('/api/memory/settings',headers=headers,json={'read_enabled':False,'auto_write':False,'reflection_enabled':False})
    assert not ask(client,headers)['memory']['references']
    assert client.post('/api/memory/search',headers=headers,json={'query':'退款'}).json()['status']=='disabled'


def test_source_scope_and_membership_revocation(client,person,model):
    name,headers=person
    events=ask(client,{k:v for k,v in headers.items() if k!='X-Shop-ID'})
    with SessionLocal() as db:
        db.delete(db.get(Membership,(name,'b')));db.commit()
    boot=client.post('/internal/agent-context/bootstrap',headers={
        'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[-1]['inputs']['context_ref']})
    assert boot.status_code==409


def test_worker_cannot_promote_assistant_or_stale_sources(client,person,model,monkeypatch):
    name,headers=person
    events=ask(client,headers,'我希望报告简洁')
    rid=events['start']['run_id']
    def result(*args):
        return providers.MaintenanceResult(memories=[providers.Extracted(source_id=rid,
            quote='这是模型生成的建议',subject='bad',predicate='bad',title='不能从模型抽取用户偏好',content='错误')],summary='仍待核对',lessons=[])
    monkeypatch.setattr(providers,'maintain',result)
    job=worker.claim(name)
    assert job['kind']=='extract'
    assert worker.execute(job)=='succeeded'
    assert not client.get('/api/memory/items',headers=headers).json()['items']
    events=ask(client,headers,'另一个问题',session_id=events['session']['id'])
    job=worker.claim(name)
    # Expire the running lease and verify another worker can reclaim it.
    with SessionLocal() as db:
        db.get(MemoryJob,job['id']).lease_until=now()-timedelta(seconds=1);db.commit()
    reclaimed=worker.claim(name)
    assert reclaimed['id']==job['id']


def test_actual_receipt_outbox_is_transactional(client,person):
    from commerce.business import audit
    name,headers=person
    with SessionLocal() as db:
        actor=scoped_actor(db,db.get(User,name),'a')
        audit(db,actor,'ops.task.update',{'id':'synthetic-task','to':'pending'})
        db.rollback()
        assert not db.scalar(select(MemoryJob.id).where(MemoryJob.user_id==name))
        audit(db,actor,'ops.task.update',{'id':'synthetic-task','to':'pending'})
        db.commit()
    job=worker.claim(name)
    assert job['kind']=='receipt' and worker.execute(job)=='succeeded'
    rows=client.get('/api/memory/items',headers=headers).json()['items']
    assert len(rows)==1 and rows[0]['kind']=='episodic' and 'pending' in rows[0]['content']


def test_real_pgvector_query_only_uses_authorized_effective_versions(client,person,monkeypatch):
    name,headers=person
    row=save(client,headers).json()
    other=save(client,{**headers,'X-Shop-ID':'b'},content='另一个店铺').json()
    vector=[1.0]+[0.0]*1023
    with SessionLocal() as db:
        for item_id in [row['id'],other['id']]:
            chunk=db.scalar(select(MemoryChunk).where(MemoryChunk.item_id==item_id))
            db.execute(text('INSERT INTO memory_vectors(chunk_id,model,embedding) VALUES(:id,:model,CAST(:v AS public.vector))'),
                {'id':chunk.id,'model':embeddings.identity(),'v':json.dumps(vector)})
        db.commit()
    from commerce.memory import retrieval
    monkeypatch.setattr(retrieval,'embedding',lambda *a,**k:vector)
    result=client.post('/api/memory/search',headers=headers,json={'query':'退款'}).json()
    assert result['status']=='hybrid'
    assert {x['id'] for x in result['items']}=={row['id']}


def test_user_intent_and_quotes_are_required_for_model_writes(client,person,model):
    name,headers=person
    events=ask(client,headers,'查看我的记忆')
    # Exercise intent validation while the model's tool invocation is in flight.
    with SessionLocal() as db:
        db.get(ChatRun,events['start']['run_id']).status='running'
        db.commit()
    tool={'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[-1]['inputs']['context_ref']}
    assert client.post('/tools/execute',headers=tool,json={'operation':'memory_forget','arguments':{'item_ids':[]}}).status_code==403
    assert client.post('/tools/execute',headers=tool,json={'operation':'memory_propose','arguments':{'quote':'并不存在的用户话语'}}).status_code==422


def test_dify_uses_fixed_bootstrap_and_no_automatic_history():
    import yaml
    data=yaml.safe_load((ROOT/'dify/chatflows/operator.yml').read_text())
    nodes=data['workflow']['graph']['nodes']
    assert [n['id'] for n in nodes]==['start','bootstrap','memory_context','agent','answer']
    assert 'memory' not in next(n['data'] for n in nodes if n['id']=='agent')
    assert '/internal/agent-context/bootstrap' in nodes[1]['data']['url']


@pytest.fixture
def embedding_api(monkeypatch):
    from pydantic import SecretStr
    monkeypatch.setattr(settings,'memory_embedding_enabled',True)
    monkeypatch.setattr(settings,'memory_embedding_url','https://embedding.invalid/v1')
    monkeypatch.setattr(settings,'memory_embedding_api_key',SecretStr('test-secret'))
    monkeypatch.setattr(settings,'memory_embedding_model','provided-later')
    monkeypatch.setattr(settings,'memory_embedding_dimensions',3)
    return settings


def test_embedding_api_contract_and_secret_handling(embedding_api,monkeypatch):
    import httpx
    seen=[]
    def request(url,**kwargs):
        seen.append((url,kwargs))
        return httpx.Response(200,json={'data':[{'embedding':[.1,.2,.3]}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(embeddings.httpx,'post',request)
    first=embeddings.identity()
    assert embeddings.embed('测试偏好')==[.1,.2,.3]
    url,kwargs=seen[0]
    assert url=='https://embedding.invalid/v1/embeddings'
    assert kwargs['headers']=={'Authorization':'Bearer test-secret'} and kwargs['follow_redirects'] is False
    assert kwargs['json']=={'model':'provided-later','input':'测试偏好','encoding_format':'float'}
    assert 'test-secret' not in repr(settings)
    monkeypatch.setattr(settings,'memory_embedding_url',url)
    assert embeddings.identity()==first
    monkeypatch.setattr(settings,'memory_embedding_revision','v2')
    assert embeddings.identity()!=first


@pytest.mark.parametrize('payload',[
    {}, {'data':[]}, {'data':[{'embedding':[1,2]}]}, {'data':[{'embedding':[True,1,2]}]},
    {'data':[{'embedding':[0,0,0]}]}, {'data':[{'embedding':'123'}]},
    {'data':[{'embedding':[float('nan'),1,2]}]}, {'data':[{'embedding':[float('inf'),1,2]}]},
])
def test_embedding_rejects_malformed_api_vectors(embedding_api,monkeypatch,payload):
    import httpx
    monkeypatch.setattr(embeddings.httpx,'post',lambda url,**k:httpx.Response(200,content=json.dumps(payload),request=httpx.Request('POST',url)))
    with pytest.raises(ValueError):embeddings.embed('内容')


def test_embedding_unconfigured_never_calls_network(monkeypatch):
    monkeypatch.setattr(settings,'memory_embedding_url','')
    monkeypatch.setattr(embeddings.httpx,'post',lambda *a,**k:pytest.fail('Unexpected embedding request'))
    assert embeddings.embed('内容') is None
    startup=(ROOT/'scripts/start.py').read_text()
    assert "start_helper('memory-embedding'" not in startup


def test_embedding_pause_preserves_config_and_does_not_call_api(client,person,embedding_api,monkeypatch):
    _,headers=person
    monkeypatch.setattr(settings,'memory_embedding_enabled',False)
    monkeypatch.setattr(embeddings.httpx,'post',lambda *a,**k:pytest.fail('Embedding API is paused'))
    assert embeddings.embed('报告') is None
    assert settings.memory_embedding_url and settings.memory_embedding_api_key.get_secret_value()
    save(client,headers)
    result=client.post('/api/memory/search',headers=headers,json={'query':'报告'}).json()
    assert result['status']=='semantic_disabled' and result['items']
    prefs=client.get('/api/memory/settings',headers=headers).json()
    assert prefs['semantic_enabled'] is False and prefs['semantic_configured'] is False


def test_embedding_reconcile_after_configuration_and_model_change(client,person,embedding_api,monkeypatch):
    name,headers=person
    monkeypatch.setattr(settings,'memory_embedding_url','')
    row=save(client,headers).json()
    job=worker.claim(name)
    assert worker.execute(job)=='waiting'
    monkeypatch.setattr(settings,'memory_embedding_url','https://embedding.invalid/v1')
    assert worker.reconcile_embeddings(name)==1
    assert worker.reconcile_embeddings(name)==0
    from commerce.memory import retrieval
    monkeypatch.setattr(retrieval,'embedding',lambda *a,**k:[1.,0.,0.])
    job=worker.claim(name)
    assert worker.execute(job)=='succeeded'
    result=client.post('/api/memory/search',headers=headers,json={'query':'无词面重合XYZ'}).json()
    assert result['status']=='hybrid' and result['items'][0]['id']==row['id']
    # A new dimension/model cannot use old vectors and must backfill without losing facts.
    monkeypatch.setattr(settings,'memory_embedding_dimensions',4)
    monkeypatch.setattr(retrieval,'embedding',lambda *a,**k:[1.,0.,0.,0.])
    result=client.post('/api/memory/search',headers=headers,json={'query':'无词面重合XYZ'}).json()
    assert result['status']=='semantic_pending' and not result['items']
    assert worker.reconcile_embeddings(name)==1
    job=worker.claim(name)
    assert worker.execute(job)=='succeeded'
    assert client.post('/api/memory/search',headers=headers,json={'query':'无词面重合XYZ'}).json()['status']=='hybrid'
    client.post('/api/memory/forget',headers=headers,json={'item_ids':[row['id']]})
    monkeypatch.setattr(settings,'memory_embedding_revision','after-forget')
    assert worker.reconcile_embeddings(name)==0
    with SessionLocal() as db:
        assert not db.scalar(select(MemoryChunk.id).where(MemoryChunk.item_id==row['id']))


def test_worker_cannot_restore_memory_forgotten_during_embedding(client,person,embedding_api,monkeypatch):
    name,headers=person
    row=save(client,headers).json()
    from commerce.memory import retrieval
    def delayed(*a,**k):
        assert client.post('/api/memory/forget',headers=headers,json={'item_ids':[row['id']]}).status_code==200
        return [1.,0.,0.]
    monkeypatch.setattr(retrieval,'embedding',delayed)
    job=worker.claim(name)
    assert worker.execute(job)=='obsolete'
    assert not client.get('/api/memory/items',headers=headers).json()['items']


def test_memory_read_tool_accepts_dify_json_arguments(client,person,model):
    _,headers=person
    ask(client,headers,'检索我的报告偏好')
    tool={'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[-1]['inputs']['context_ref']}
    for args in [{'query':'报告'},json.dumps({'query':'报告'},ensure_ascii=False)]:
        response=client.post('/tools/read',headers=tool,json={'operation':'memory_search','arguments':args})
        assert response.status_code==200,response.text
        assert response.json()['data']['status']=='empty'


def test_shared_publish_requires_grant_and_forget_clears_reader_history(client,person,auth,model):
    from commerce.memory.models import MemorySpace,MemoryGrant
    from commerce.memory import context
    name,headers=person
    source=save(client,headers).json()
    viewer={**auth('operator-a'),'X-Shop-ID':'a'}
    assert client.post('/api/memory/items/'+source['id']+'/publish',headers=headers,json={'expected_revision':1}).status_code==403
    with SessionLocal() as db:
        shared=MemorySpace(key=uuid4().hex,owner_id=name,kind='shop_shared',shop_id='a',required_shops={'a':'demo'})
        db.add(shared);db.flush()
        db.add(MemoryGrant(space_id=shared.id,user_id='operator-a',can_publish=False,can_manage=False));db.commit()
    response=client.post('/api/memory/items/'+source['id']+'/publish',headers=headers,json={'expected_revision':1})
    assert response.status_code==200,response.text
    published=response.json()
    visible=client.get('/api/memory/items',headers=viewer).json()['items']
    assert any(v['id']==published['id'] and not v['can_manage'] for v in visible)
    assert not any(v['id']==source['id'] for v in visible)
    assert client.patch('/api/memory/items/'+published['id'],headers=viewer,json={
        'expected_revision':1,'content':'不能纠正别人的共享约定'}).status_code==404
    first=ask(client,viewer,'报告如何排序')
    second=ask(client,viewer,'接着讲',session_id=first['session']['id'])
    # A summary depending on the shared answer must be cleared for the reader too.
    with SessionLocal() as db:
        session=db.get(ChatSession,first['session']['id'])
        session.summary='旧的共享约定';session.summary_sources=[first['start']['run_id']];db.commit()
    result=client.post('/api/memory/forget',headers=headers,json={'item_ids':[source['id']]})
    assert result.status_code==200 and result.json()['forgotten']==2
    with SessionLocal() as db:
        assert db.get(ChatRun,first['start']['run_id']).memory_excluded
        assert db.get(ChatRun,second['start']['run_id']).memory_excluded
        session=db.get(ChatSession,first['session']['id'])
        assert not session.summary and not session.upstream_id
        with pytest.raises(HTTPException):context.validate(db,second['start']['run_id'])


def test_shared_publish_forbids_private_chat_projection(client,person,model):
    from commerce.memory.models import MemorySpace
    name,headers=person
    events=ask(client,headers,'请记住我的个人报告偏好：先列异常')
    with SessionLocal() as db:
        actor=scoped_actor(db,db.get(User,name),'a')
        from commerce.memory.schemas import MemoryCreate
        item=service.create(db,actor,MemoryCreate(title='私人偏好',content='先列异常'),
            source={'type':'chat','id':events['start']['run_id'],'role':'user','quote':'先列异常'})
        db.add(MemorySpace(key=uuid4().hex,owner_id=name,kind='shop_shared',shop_id='a',required_shops={'a':'demo'}))
        db.commit();item_id=item.id
    response=client.post('/api/memory/items/'+item_id+'/publish',headers=headers,json={'expected_revision':1})
    assert response.status_code==403


def test_failed_jobs_retry_is_scoped_to_owner(client,person,auth):
    name,headers=person
    save(client,headers)
    with SessionLocal() as db:
        job=db.scalar(select(MemoryJob).where(MemoryJob.user_id==name))
        job.state='dead';job.attempts=4;db.commit()
    assert client.get('/api/memory/settings',headers=headers).json()['background_failed']==1
    client.post('/api/memory/jobs/retry',headers=auth(),json={})
    assert client.get('/api/memory/settings',headers=headers).json()['background_failed']==1
    assert client.post('/api/memory/jobs/retry',headers=headers,json={}).json()['queued']==1
    assert client.get('/api/memory/settings',headers=headers).json()['background_failed']==0


def test_global_disable_blocks_runtime_memory_and_receipt_jobs(client,person,model,monkeypatch):
    from commerce.business import audit
    name,headers=person
    row=save(client,headers).json()
    job=worker.claim(name)
    monkeypatch.setattr(settings,'memory_enabled',False)
    assert worker.execute(job)=='obsolete'
    events=ask(client,headers,'请列出我的偏好')
    assert events['memory']['status']=='disabled' and not events['memory']['references']
    with SessionLocal() as db:
        actor=scoped_actor(db,db.get(User,name),'a')
        audit(db,actor,'ops.task.update',{'id':'disabled-test'})
        db.commit()
        assert not db.scalar(select(MemoryJob.id).where(MemoryJob.user_id==name,MemoryJob.kind!='embed'))
    tool={'Authorization':'Bearer '+settings.tool_service_key,'X-Context-Ref':model[-1]['inputs']['context_ref']}
    response=client.post('/tools/read',headers=tool,json={'operation':'memory_search','arguments':{'query':'偏好'}})
    assert response.status_code==200 and response.json()['data']['status']=='disabled'
    assert client.get('/api/memory/items/'+row['id'],headers=headers).status_code==200
