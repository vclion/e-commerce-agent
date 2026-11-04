"""Synthetic-user acceptance against the real published Dify Agent and worker."""
import json
import time
from uuid import uuid4
import httpx
from sqlalchemy import delete,select
from commerce.auth import password_hash
from commerce.config import ROOT,settings
from commerce.db import SessionLocal
from commerce.models import User,Membership,Audit,ChatRun,Conversation,AccessToken
from commerce.memory.models import ChatSession,MemoryJob,ContextSegment


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    actor='memorylive-'+uuid4().hex[:12]
    marker='MQA'+uuid4().hex[:8]
    report={'scope':'synthetic_user_real_dify_no_platform_requests','runs':[],'checks':[]}
    with SessionLocal() as db:
        db.add(User(id=actor,username=actor,role='operator',shop_id='a',display_name='记忆联调',password_hash=password_hash(settings.demo_password)))
        db.flush();db.add(Membership(user_id=actor,shop_id='a'));db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',timeout=150) as client:
            token=client.post('/api/auth/login',json={'username':actor,'password':settings.demo_password}).json()['access_token']
            headers={'Authorization':'Bearer '+token,'X-Shop-ID':'a'}
            def ask(label,query,session_id='',attempt=0):
                start=time.perf_counter();first=None;events={};answer=''
                with client.stream('POST','/api/chat',headers=headers,json={'query':query,'workspace':'today','session_id':session_id}) as response:
                    response.raise_for_status();event=''
                    for line in response.iter_lines():
                        if line.startswith('event:'):event=line[6:].strip()
                        if line.startswith('data:'):
                            data=json.loads(line[5:]);events[event]=data
                            if event=='delta':
                                first=first if first is not None else (time.perf_counter()-start)*1000
                                answer+=data['text']
                entry={'label':label,'attempt':attempt+1,'first_token_ms':round(first,1) if first else None,'total_ms':round((time.perf_counter()-start)*1000,1),
                    'status':events.get('done',{}).get('status'),'answer':answer,'memory':events.get('memory'), 'failure':events.get('failure')}
                report['runs'].append(entry);print(label,entry['status'],entry['total_ms'],flush=True)
                if entry['status']!='succeeded' and attempt==0:
                    return ask(label,query,events.get('session',{}).get('id',session_id),attempt+1)
                assert entry['status']=='succeeded',entry
                return events,answer
            row=client.post('/api/memory/items',headers=headers,json={'title':'报告格式验收','scope':'personal_shop',
                'subject':'report','predicate':'format','content':'报告按异常、原因、建议组织；每次回复末尾单独写固定标记 '+marker+'。'}).json()
            first,answer=ask('fresh_session_uses_memory','不用查询经营数据，请说明你将采用的报告结构。')
            assert marker in answer and any(r['id']==row['id'] for r in first['memory']['references'])
            second,answer=ask('another_session_uses_memory','请简述我喜欢的报告结构，不查询经营数据。')
            assert marker in answer
            report['checks'].append('cross_session_persisted_memory')
            updated=marker+'NEW'
            changed=client.patch('/api/memory/items/'+row['id'],headers=headers,json={'expected_revision':row['revision'],
                'content':'报告先给建议，再列异常；每次回复末尾单独写固定标记 '+updated+'。'}).json()
            third,answer=ask('correction_uses_new_version','请简述我喜欢的报告结构，不查询经营数据。',first['session']['id'])
            assert updated in answer and any(r['revision']==changed['revision'] for r in third['memory']['references'])
            report['checks'].append('explicit_correction_updates_active_context')
            client.post('/api/memory/forget',headers=headers,json={'item_ids':[row['id']]}).raise_for_status()
            fourth,answer=ask('forget_blocks_old_context','你记得我的固定验收标记吗？如无记录请回答不知道，不要猜。',first['session']['id'])
            assert marker not in answer and not fourth['memory']['references']
            report['checks'].append('forget_excludes_old_answer_and_context')
            fifth,answer=ask('natural_language_remember','请记住我的个人报告偏好：异常在前，建议在后。请保存为当前店铺的记忆。')
            memories=client.get('/api/memory/items',headers=headers).json()['items']
            assert any(r['status']=='active' and '异常' in r['content'] for r in memories),memories
            report['checks'].append('real_agent_memory_tool_commits')
            # Recent turns remain originals; independently seed an aged block to exercise compaction.
            from commerce.auth import scope_fingerprint,scoped_actor
            from commerce.memory import service
            from commerce.memory.policy import preferences
            with SessionLocal() as db:
                local=db.get(ChatSession,fifth['session']['id'])
                fingerprint=scope_fingerprint(scoped_actor(db,db.get(User,actor),'a'))
                for seq in range(local.next_seq,local.next_seq+26):
                    last=ChatRun(user_id=actor,shop_id='a',workspace='today',session_id=local.id,
                        turn_seq=seq,scope_fingerprint=fingerprint,query='确认本次活动只保存草稿，不发布。',
                        answer='草稿方案待确认，未发布。',status='succeeded')
                    db.add(last)
                local.next_seq+=26;db.flush()
                service.enqueue(db,actor,'compress',last.id,preferences(db,actor).epoch)
                db.commit()
            deadline=time.monotonic()+80
            while time.monotonic()<deadline:
                with SessionLocal() as db:
                    segment=db.scalar(select(ContextSegment).where(ContextSegment.session_id==fifth['session']['id']))
                    if segment:
                        report['checks'].append('real_independent_segment_saved');break
                time.sleep(1)
            else:raise AssertionError('Background segment not ready')
            report['cache']='No application memory-content cache; model provider cache not controlled'
    finally:
        with SessionLocal() as db:
            for model in [Audit,AccessToken,ChatRun,Conversation,Membership]:db.execute(delete(model).where(model.user_id==actor))
            db.execute(delete(User).where(User.id==actor));db.commit()
        report['test_account_removed']=True
        (ROOT/'tests/artifacts/reports/memory-live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({'checks':report['checks'],'runs':len(report['runs'])},ensure_ascii=False))


if __name__=='__main__':main()
