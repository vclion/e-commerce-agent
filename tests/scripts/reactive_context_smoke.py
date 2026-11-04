"""Real compaction and Agent over synthetic history, with no platform operations."""
import asyncio
import json
import time
from uuid import uuid4
from sqlalchemy import delete, select
from commerce.auth import password_hash, scoped_actor, scope_fingerprint
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import User, Membership, ChatRun, AccessToken, Audit, Conversation
from commerce.memory.models import ChatSession, MemoryRunContext, MemoryItem, MemorySpace
from commerce.memory.policy import preferences
from commerce.memory import indexes, hierarchy
from commerce.schemas import Chat
from commerce.main import chat


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    name='reactive-live-'+uuid4().hex[:12]
    report={'scope':'synthetic_history_real_compactor_and_dify_agent_no_platform_requests','checks':[]}
    with SessionLocal() as db:
        db.add(User(id=name,username=name,role='operator',shop_id='a',display_name='兜底压缩验收',
            password_hash=password_hash(settings.demo_password)))
        db.flush();db.add(Membership(user_id=name,shop_id='a'));db.flush()
        actor=scoped_actor(db,db.get(User,name),'a')
        session=ChatSession(user_id=name,shop_id='a',workspace='today',scope_fingerprint=scope_fingerprint(actor),next_seq=4)
        db.add(session);db.flush();sid=session.id
        preferences(db,name).auto_write=False
        rows=[]
        for turn in range(1,4):
            row=ChatRun(user_id=name,shop_id='a',workspace='today',scope_fingerprint=session.scope_fingerprint,
                session_id=sid,turn_seq=turn,status='succeeded',
                query='确认活动预算12345.67元；不要执行退款；素材ID为ASSET-007。' if turn==1 else '继续讨论活动素材，尚未决定发布。',
                answer='历史素材讨论，不表示执行成功。'*350 if turn<3 else '最新一轮原文：活动草稿仍待确认，未发布。',effects=[])
            db.add(row);rows.append(row)
        db.flush();source_ids=[r.id for r in rows];originals={r.id:r.answer for r in rows}
        current=ChatRun(user_id=name,shop_id='a',workspace='today',session_id=sid,turn_seq=4,
            query='请简述活动预算、素材ID和未完成事项；不执行任何业务操作，不保存记忆。')
        settings.memory_context_tokens=48000
        _,_,baseline=indexes.assemble(db,session,current,[])
        report['uncompressed_tokens_upper_bound']=baseline['context_tokens_upper_bound']
        db.commit()
    try:
        # Only this process uses the small budget. Running API/other chats keep their normal settings.
        settings.memory_context_tokens=6000
        started=time.monotonic()
        with SessionLocal() as db:
            actor=scoped_actor(db,db.get(User,name),'a')
            response=chat(Chat(query=current.query,workspace='today',session_id=sid),user=actor,db=db)
            async def consume():
                output=''
                async for frame in response.body_iterator:output+=frame
                return output
            raw=asyncio.run(consume())
        events={};answer=''
        for frame in raw.split('\n\n'):
            lines=frame.splitlines()
            if len(lines)>1 and lines[0].startswith('event:'):
                event=lines[0][7:];data=json.loads(lines[1][6:]);events[event]=data
                if event=='delta':answer+=data['text']
        assert events.get('done',{}).get('status')=='succeeded',events.get('failure')
        assert '12345.67' in answer and 'ASSET-007' in answer
        with SessionLocal() as db:
            rid=events['start']['run_id'];snap=db.get(MemoryRunContext,rid)
            assert snap.timings['reactive']['action']=='summarize_recent'
            assert snap.timings['reactive']['summary_calls']==1 and snap.timings['levels']['raw']==1
            assert snap.timings['reactive']['summary_source_ids']==source_ids[:2]
            assert originals[source_ids[-1]] in snap.payload
            assert snap.timings['context_tokens_upper_bound']<=6000
            for source in source_ids:assert db.get(ChatRun,source).answer==originals[source]
            assert not db.scalar(select(MemoryItem.id).join(MemorySpace).where(MemorySpace.owner_id==name))
            ops=list(db.scalars(select(Audit.operation).where(Audit.request_id==rid,Audit.user_id==name)))
            assert all(op in {'tool.attempt','tool.workspace_context','tool.history_read','tool.memory_search','tool.memory_get'} for op in ops),ops
            report.update(trace=snap.timings,answer=answer,operations=ops,total_ms=round((time.monotonic()-started)*1000,1))
            report['checks']=['real_preflight_compaction','latest_original_and_exact_values_preserved',
                'original_chats_unchanged','no_long_memory_created','real_agent_answers_from_compacted_context','within_budget']
            report['result']='passed'
    finally:
        with SessionLocal() as db:
            for cls in [Audit,AccessToken,ChatRun,Conversation,Membership]:db.execute(delete(cls).where(cls.user_id==name))
            db.execute(delete(User).where(User.id==name));db.commit()
        report['test_account_removed']=True
        (ROOT/'tests/artifacts/reports/reactive-context-live-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'result':report.get('result','failed'),'checks':report['checks']},ensure_ascii=False))


if __name__=='__main__':main()
