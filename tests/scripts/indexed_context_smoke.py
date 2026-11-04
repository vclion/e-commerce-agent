"""Synthetic hot/cold indexes and a real Dify search -> original-read round trip."""
import json
import time
from datetime import timedelta
from uuid import uuid4
import httpx
from sqlalchemy import delete, select
from commerce.auth import password_hash, scope_fingerprint, scoped_actor
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import User, Membership, ChatRun, AccessToken, Audit, Conversation, now
from commerce.memory.models import ChatSession, ContextSegment, MemoryJob, MemoryRunContext
from commerce.memory.policy import preferences
from commerce.memory import service, indexes


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    name = 'indexed-live-' + uuid4().hex[:12]
    marker = 'COLD-' + uuid4().hex[:12]
    report = {'scope':'synthetic_history_real_dify_no_store_platform_requests','checks':[]}
    with SessionLocal() as db:
        db.add(User(id=name, username=name, role='operator', shop_id='a', display_name='索引上下文验收',
            password_hash=password_hash(settings.demo_password)))
        db.flush(); db.add(Membership(user_id=name, shop_id='a')); db.flush()
        actor = scoped_actor(db, db.get(User, name), 'a')
        session = ChatSession(user_id=name, shop_id='a', workspace='today',
            scope_fingerprint=scope_fingerprint(actor), next_seq=41)
        db.add(session); db.flush(); sid = session.id
        records = []
        for seq in range(1, 41):
            record = ChatRun(user_id=name,shop_id='a',workspace='today',session_id=sid,turn_seq=seq,
                scope_fingerprint=session.scope_fingerprint,status='succeeded', effects=[],
                query='冷档案蓝莓计划资料；最早一轮助手原文有验收编码。' if seq <= 8 else '讨论新的服装素材安排。',
                answer=('原文验收编码：'+marker+'。' if seq==1 else '')+'这是历史讨论，不代表实际业务执行。'*20)
            db.add(record); records.append(record)
        db.flush()
        cold = ContextSegment(session_id=sid,epoch=session.epoch,start_seq=1,end_seq=8,
            source_ids=[r.id for r in records[:8]],title='冷档案蓝莓计划',
            summary='最早一轮助手原文中保存了合成验收编码，需回查。',
            overview='最早一轮助手原文中有验收编码。',key_points=[],entities=[],created_at=now()-timedelta(days=60))
        db.add(cold); db.flush(); cold_id = cold.id
        service.enqueue(db,name,'compress',records[-1].id,preferences(db,name).epoch); db.commit()
    try:
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            with SessionLocal() as db:
                blocks=list(db.scalars(select(ContextSegment).where(ContextSegment.session_id==sid,ContextSegment.id!=cold_id)))
                if blocks:
                    assert blocks[0].entities is not None
                    report['checks'].append('real_background_index_metadata_saved');break
                if db.scalar(select(MemoryJob.id).where(MemoryJob.user_id==name,MemoryJob.state=='dead')):
                    raise AssertionError('Background index workflow failed')
            time.sleep(1)
        else:raise AssertionError('Background index not ready')
        with httpx.Client(base_url='http://127.0.0.1:8000',timeout=150) as client:
            login=client.post('/api/auth/login',json={'username':name,'password':settings.demo_password});login.raise_for_status()
            headers={'Authorization':'Bearer '+login.json()['access_token'],'X-Shop-ID':'a'}
            query='请调用history_read搜索冷档案蓝莓计划，按索引找到该计划最早一轮助手原文，原样告诉我验收编码；不要查询经营数据。'
            started=time.monotonic();answer='';events={};event=''
            with client.stream('POST','/api/chat',headers=headers,json={'query':query,'workspace':'today','session_id':sid}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('event:'):event=line[6:].strip()
                    elif line.startswith('data:'):
                        data=json.loads(line[5:]);events[event]=data
                        if event=='delta':answer+=data['text']
            assert events.get('done',{}).get('status')=='succeeded',events.get('failure')
            assert marker in answer,answer
            report.update(answer=answer,total_ms=round((time.monotonic()-started)*1000,1))
            with SessionLocal() as db:
                run_id=events['start']['run_id'];snap=db.get(MemoryRunContext,run_id)
                assert snap.timings['levels']['raw']==3
                assert snap.timings['context_strategy']=='indexed-v2'
                assert cold_id not in snap.timings['selected_indexes'] and marker not in snap.payload
                assert cold_id in snap.timings['used_index_ids']
                cold=db.get(ContextSegment,cold_id)
                assert cold.use_count==1 and indexes.is_hot(cold,42,indexes.metadata(db,db.get(ChatSession,sid),42))
                ops=list(db.scalars(select(Audit.operation).where(Audit.user_id==name,Audit.request_id==run_id)))
                assert ops.count('tool.history_read')>=2
                assert all(op in {'tool.attempt','tool.history_read','tool.workspace_context','tool.memory_search','tool.memory_get'} for op in ops)
                report['trace']=snap.timings;report['operations']=ops
                report['checks']+=['exactly_three_recent_originals','cold_index_absent_from_default_context',
                    'real_agent_searches_and_recovers_cold_original','explicit_read_reactivates_index',
                    'context_within_configured_budget']
                assert snap.timings['context_tokens_upper_bound']<=snap.timings['context_budget_tokens']
            report['result']='passed'
    finally:
        with SessionLocal() as db:
            for cls in [Audit,AccessToken,ChatRun,Conversation,Membership]:db.execute(delete(cls).where(cls.user_id==name))
            db.execute(delete(User).where(User.id==name));db.commit()
        report['test_account_removed']=True
        (ROOT/'tests/artifacts/reports/indexed-context-live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'result':report.get('result','failed'),'checks':report['checks']},ensure_ascii=False))


if __name__=='__main__':main()
