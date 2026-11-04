"""Response compaction, immutable originals, and at-most-once pre-tool retry."""
import asyncio
import json
from uuid import uuid4
import pytest
from sqlalchemy import select, func
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import ChatRun, Draft, Conversation, Audit
from commerce.memory import providers, service, reactive
from commerce.memory.models import ChatSession, MemoryRunContext, MemoryItem, MemorySpace
from test_memory import person, model, ask
from test_tiered_context import history


@pytest.fixture
def compactor(monkeypatch):
    calls=[]
    def maintain(action,payload):
        assert action=='reactive_context'
        calls.append(payload)
        return providers.MaintenanceResult(summary='前两轮讨论活动素材，方案尚待核对；以保留的用户原文和实际操作状态为准。')
    monkeypatch.setattr(providers,'maintain',maintain)
    return calls


def seeded(name, *, large=False, workspace='today'):
    sid,ids=history(name,count=3,answer_size=40)
    with SessionLocal() as db:
        db.get(ChatSession,sid).workspace=workspace
        for i,rid in enumerate(ids):
            row=db.get(ChatRun,rid);row.workspace=workspace
            row.answer=('旧轮次讨论素材细节。'*400 if large and i<2 else '近期素材方案仍待核对。'*30)
        db.get(ChatRun,ids[0]).answer+=' 数字123.45元，资源DRAFT-001，尚未确认完成。'
        db.commit()
    return sid,ids


def frames(response):
    result={}
    for chunk in response.text.split('\n\n'):
        lines=chunk.splitlines()
        if len(lines)>1 and lines[0].startswith('event:'):
            result[lines[0][7:]]=json.loads(lines[1][6:])
    return result


def test_preflight_compacts_older_two_and_keeps_latest_and_protected_sources(client,person,model,compactor,monkeypatch):
    name,headers=person
    monkeypatch.setattr(settings,'memory_context_tokens',6000)
    sid,ids=seeded(name,large=True)
    with SessionLocal() as db:
        originals={rid:db.get(ChatRun,rid).answer for rid in ids}
    result=ask(client,headers,'当前问题逐字保留',session_id=sid)
    assert len(compactor)==1 and model[-1]['query']=='当前问题逐字保留'
    with SessionLocal() as db:
        snap=db.get(MemoryRunContext,result['start']['run_id'])
        assert snap.timings['levels']['raw']==1 and snap.timings['levels']['reactive_summary']==1
        assert snap.timings['reactive']['summary_source_ids']==ids[:2]
        assert snap.timings['reactive']['retained_latest_id']==ids[-1]
        assert originals[ids[-1]] in snap.payload
        for value in ['12345.67','不要执行退款','ASSET-007','123.45元','DRAFT-001','actual_effects']:
            assert value in snap.payload
        assert snap.timings['context_tokens_upper_bound']<=6000
        assert set(ids).issubset(snap.timings['history_sources'])
        assert not db.scalar(select(MemoryItem.id).join(MemorySpace).where(MemorySpace.owner_id==name))
        for rid in ids:assert db.get(ChatRun,rid).answer==originals[rid]


def test_normal_request_does_not_call_compactor(client,person,model,monkeypatch):
    name,headers=person
    sid,_=seeded(name)
    monkeypatch.setattr(providers,'maintain',lambda *a,**k:pytest.fail('Normal context must not call compaction model'))
    result=ask(client,headers,'继续',session_id=sid)
    with SessionLocal() as db:
        snap=db.get(MemoryRunContext,result['start']['run_id'])
        assert snap.timings['levels']['raw']==3 and snap.timings['reactive']['summary_calls']==0


def test_compaction_failure_is_bounded_and_does_not_leave_a_running_run(client,person,model,monkeypatch):
    name,headers=person
    sid,_=seeded(name,large=True)
    monkeypatch.setattr(settings,'memory_context_tokens',6000)
    calls=[]
    def fail(*a):calls.append(1);raise ValueError('PROVIDER-SECRET-MUST-NOT-LEAK')
    monkeypatch.setattr(providers,'maintain',fail)
    response=client.post('/api/chat',headers=headers,json={'query':'继续','workspace':'today','session_id':sid})
    assert response.status_code==503 and 'PROVIDER-SECRET' not in response.text and len(calls)==1
    with SessionLocal() as db:
        assert not db.scalar(select(ChatRun.id).where(ChatRun.user_id==name,ChatRun.status=='running'))


def test_overlong_summary_is_not_recursively_compressed(client,person,model,monkeypatch):
    name,headers=person
    sid,_=seeded(name,large=True)
    monkeypatch.setattr(settings,'memory_context_tokens',6000)
    calls=[]
    def too_long(*a):calls.append(1);return providers.MaintenanceResult(summary='过长'*1000)
    monkeypatch.setattr(providers,'maintain',too_long)
    response=client.post('/api/chat',headers=headers,json={'query':'继续','workspace':'today','session_id':sid})
    assert response.status_code==413 and len(calls)==1


def test_forget_during_compaction_cannot_write_back_stale_context(client,person,model,monkeypatch):
    name,headers=person
    sid,_=seeded(name,large=True)
    monkeypatch.setattr(settings,'memory_context_tokens',6000)
    def invalidate(*a):
        with SessionLocal() as db:service.invalidate(db,name);db.commit()
        return providers.MaintenanceResult(summary='过时内容不可写回')
    monkeypatch.setattr(providers,'maintain',invalidate)
    response=client.post('/api/chat',headers=headers,json={'query':'继续','workspace':'today','session_id':sid})
    assert response.status_code==409 and not model


def scripted(monkeypatch,events,*,before_error=None,status_code=200,repeat_error=False):
    from commerce import main
    requests=[]
    class Response:
        def __init__(self,body,index):self.body,self.index,self.status_code=body,index,status_code if index==0 else 200
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        async def aread(self):return b'{"code":"context_length_exceeded","message":"maximum context length"}'
        async def aiter_lines(self):
            if self.index==0 and before_error:await asyncio.to_thread(before_error,self.body)
            sequence=events if self.index==0 or repeat_error else [{'event':'message','answer':'重试后的完整回答'},{'event':'message_end'}]
            for event in sequence:
                yield 'data: '+json.dumps({**event,'conversation_id':'reactive-cid-'+str(self.index)+'-'+self.body['user']})
    class Client:
        def __init__(self,**k):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def stream(self,*a,**k):
            body=k['json'];requests.append(body);return Response(body,len(requests)-1)
    monkeypatch.setattr(main.httpx,'AsyncClient',Client)
    monkeypatch.setattr(settings,'dify_operator_key','reactive-test-placeholder')
    return requests


OVERFLOW=[{'event':'workflow_finished','data':{'status':'failed','error':'maximum context length exceeded SECRET'}}]


@pytest.mark.parametrize('http_status',[200,413])
def test_provider_overflow_retries_once_with_new_credential_and_same_query(client,person,monkeypatch,compactor,http_status):
    name,headers=person
    sid,ids=seeded(name)
    requests=scripted(monkeypatch,OVERFLOW,status_code=http_status)
    result=ask(client,headers,'原问题不得改写',session_id=sid)
    assert result['done']['status']=='succeeded' and len(requests)==2 and len(compactor)==1,result
    assert requests[0]['query']==requests[1]['query']=='原问题不得改写'
    first,second=[r['inputs']['context_ref'] for r in requests]
    assert first!=second and requests[1]['conversation_id']==''
    auth={'Authorization':'Bearer '+settings.tool_service_key}
    assert client.post('/internal/agent-context/bootstrap',headers={**auth,'X-Context-Ref':first}).status_code in (401,409)
    assert client.post('/internal/agent-context/bootstrap',headers={**auth,'X-Context-Ref':second}).status_code==200
    with SessionLocal() as db:
        snap=db.get(MemoryRunContext,result['start']['run_id'])
        assert snap.timings['reactive_retry_count']==1 and snap.timings['reactive']['trigger']=='provider_context_limit'
        assert db.get(ChatRun,ids[-1]).answer in snap.payload
        conv=db.get(Conversation,db.get(ChatRun,result['start']['run_id']).conversation_id)
        assert conv.context_ref==second


def test_existing_tool_execution_is_not_replayed_after_context_error(client,person,monkeypatch,compactor):
    name,headers=person
    sid,_=seeded(name,workspace='drafts')
    title='reactive-write-'+uuid4().hex
    def write_once(body):
        response=client.post('/tools/execute',headers={'Authorization':'Bearer '+settings.tool_service_key,
            'X-Context-Ref':body['inputs']['context_ref']},json={'operation':'content_save','arguments':{
                'kind':'report','title':title,'content':'这是合成回归数据，不代表实际业务。'}})
        assert response.status_code==200,response.text
    requests=scripted(monkeypatch,OVERFLOW,before_error=write_once)
    result=frames(client.post('/api/chat',headers=headers,json={'query':'保存草稿','workspace':'drafts','session_id':sid}))
    assert result['done']['status']=='failed' and len(requests)==1 and not compactor
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Draft).where(Draft.payload['title'].as_string()==title))==1
        assert len(db.get(ChatRun,result['start']['run_id']).effects)==1


def test_partial_answer_is_kept_and_never_retried(client,person,monkeypatch,compactor):
    name,headers=person
    sid,_=seeded(name)
    requests=scripted(monkeypatch,[{'event':'message','answer':'已经显示的片段'},*OVERFLOW])
    result=ask(client,headers,'继续',session_id=sid)
    assert len(requests)==1 and not compactor and result['done']['status']=='failed'
    with SessionLocal() as db:assert db.get(ChatRun,result['start']['run_id']).answer=='已经显示的片段'


@pytest.mark.parametrize('message',['401 invalid api key','rate limit 429','ConnectTimeoutError','unrelated plugin error'])
def test_non_context_errors_never_trigger_compaction(client,person,monkeypatch,compactor,message):
    name,headers=person
    sid,_=seeded(name)
    requests=scripted(monkeypatch,[{'event':'error','message':message}])
    result=ask(client,headers,'继续',session_id=sid)
    assert len(requests)==1 and not compactor and result['done']['status']=='failed'


def test_second_context_overflow_stops_without_another_retry(client,person,monkeypatch,compactor):
    name,headers=person
    sid,_=seeded(name)
    requests=scripted(monkeypatch,OVERFLOW,repeat_error=True)
    result=ask(client,headers,'继续',session_id=sid)
    assert result['done']['status']=='failed' and len(requests)==2 and len(compactor)==1


def test_late_old_tool_call_is_rejected_during_retry_compaction(client,person,monkeypatch):
    name,headers=person
    sid,_=seeded(name)
    requests=scripted(monkeypatch,OVERFLOW)
    checked=[]
    def maintain(*a):
        response=client.post('/tools/read',headers={'Authorization':'Bearer '+settings.tool_service_key,
            'X-Context-Ref':requests[0]['inputs']['context_ref']},json={'operation':'workspace_context','arguments':{}})
        checked.append(response.status_code)
        return providers.MaintenanceResult(summary='前两轮讨论活动素材，尚未完成。')
    monkeypatch.setattr(providers,'maintain',maintain)
    result=ask(client,headers,'继续',session_id=sid)
    assert result['done']['status']=='succeeded' and checked==[401]


def test_tool_event_before_backend_call_prevents_retry(client,person,monkeypatch,compactor):
    name,headers=person
    sid,_=seeded(name)
    requests=scripted(monkeypatch,[{'event':'agent_thought','tool':'workspace_context'},*OVERFLOW])
    result=ask(client,headers,'继续',session_id=sid)
    assert result['done']['status']=='failed' and len(requests)==1 and not compactor


def test_resource_identifier_adjacent_to_chinese_and_starting_with_digit_is_preserved():
    rid='1ab23cd4-0000-4000-8000-abcd1234ffff'
    record=ChatRun(id=uuid4().hex,turn_seq=1,status='succeeded',query='继续',effects=[],
        answer='草稿ID为'+rid+'，金额-123.45元，SKU为ABC。')
    literals=reactive.protected(record)['assistant_literals_unverified']
    assert rid in literals and '-123.45元' in literals and 'ABC' in literals
