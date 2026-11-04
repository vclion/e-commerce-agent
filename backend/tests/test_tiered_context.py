"""Long-history retention, original recovery, and stale/forgotten source fences."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import select, update
from commerce.auth import scoped_actor, scope_fingerprint
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import ChatRun, User
from commerce.memory import hierarchy, providers, worker, service
from commerce.memory.models import ChatSession, ContextSegment, MemoryJob, MemoryRunContext
from commerce.memory.policy import preferences
from commerce.memory.schemas import MemoryCreate
from test_memory import person, model, ask


def history(name, count=100, answer_size=1100):
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, name), 'a')
        session = ChatSession(user_id=name, shop_id='a', workspace='today',
            scope_fingerprint=scope_fingerprint(actor), next_seq=count+1,
            summary='旧滚动摘要不应再注入LEGACY', through_seq=count)
        db.add(session); db.flush()
        records = []
        for seq in range(1, count+1):
            query = '继续讨论本次工作' if seq != 1 else '确认活动预算12345.67元；不要执行退款；素材ID为ASSET-007。'
            record = ChatRun(user_id=name, shop_id='a', workspace='today',
                scope_fingerprint=session.scope_fingerprint, session_id=session.id, turn_seq=seq,
                query=query, answer=('原文细节' + str(seq) + '：' + '服装运营说明' * answer_size),
                status='succeeded', effects=[{'operation':'content_save','label':'草稿已保存','resource_id':'ASSET-007'}] if seq == 1 else [])
            db.add(record); records.append(record)
        db.flush(); ids = [r.id for r in records]; db.commit()
        return session.id, ids


def segments(name, sid, ids):
    with SessionLocal() as db:
        session = db.get(ChatSession, sid)
        for start in range(0, len(ids)-settings.memory_recent_turns, 8):
            batch = ids[start:start+8]
            if len(batch) != 8 or start+8 > len(ids)-settings.memory_recent_turns:
                continue
            db.add(ContextSegment(session_id=sid, epoch=session.epoch, start_seq=start+1,
                end_seq=start+len(batch), source_ids=batch, title='活动素材安排',
                summary='本段讨论服装活动素材安排，模型提出建议但未确认执行。'*12,
                overview='活动素材仍待确认，未发布。', key_points=[]))
        db.commit()


def tool_headers(model):
    return {'Authorization':'Bearer '+settings.tool_service_key,
        'X-Context-Ref':model[-1]['inputs']['context_ref']}


def test_hundred_turns_preserve_exact_constraints_and_recover_original(client, person, model, monkeypatch):
    name, headers = person
    monkeypatch.setattr(settings, 'memory_context_tokens', 12000)
    sid, ids = history(name, answer_size=80)
    segments(name, sid, ids)
    events = ask(client, headers, '活动预算和素材ID是什么，继续当前任务', session_id=sid)
    assert events['done']['status'] == 'succeeded'
    with SessionLocal() as db:
        snapshot = db.get(MemoryRunContext, events['start']['run_id'])
        assert '原文细节1：' not in snapshot.payload and 'LEGACY' not in snapshot.payload
        assert '12345.67' not in snapshot.payload
        assert snapshot.timings['context_tokens_upper_bound'] <= 12000
        assert snapshot.timings['history_turns'] == 100
        assert snapshot.timings['levels']['raw'] == 3 and snapshot.timings['levels']['index'] <= 3
        assert snapshot.timings['levels']['segment'] == snapshot.timings['levels']['overview'] == 0
    response = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read','arguments':{'run_id':ids[0], 'field':'assistant', 'length':4000}})
    assert response.status_code == 200, response.text
    page = response.json()['data']
    assert page['text'].startswith('原文细节1：') and page['has_more']
    following = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read','arguments':{'run_id':ids[0], 'offset':page['next_offset'], 'length':4000}}).json()['data']
    with SessionLocal() as db:
        raw = db.get(ChatRun, ids[0]).answer
        assert page['text'] + following['text'] == raw[:following['next_offset']]
        assert ids[0] in db.get(MemoryRunContext, events['start']['run_id']).timings['history_sources']


def test_recent_messages_stay_whole_when_budget_is_sufficient(client, person, model):
    name, headers = person
    sid, ids = history(name, count=2, answer_size=40)
    events = ask(client, headers, '继续', session_id=sid)
    with SessionLocal() as db:
        snapshot = db.get(MemoryRunContext, events['start']['run_id'])
        assert snapshot.timings['levels']['raw'] == 2
        assert not snapshot.timings['history_incomplete']
        for rid in ids:
            assert db.get(ChatRun, rid).answer in snapshot.payload


def test_light_compression_preserves_distinct_numbers_and_effects():
    record = ChatRun(id=uuid4().hex, turn_seq=1, query='预算100元', status='succeeded',
        answer='库存10件\n\n库存10件\n\n库存11件', effects=[{'resource_id':'draft-123','status':'queued'}])
    value = hierarchy.light(record)
    assert value['assistant'] == '库存10件\n\n库存11件'
    assert value['actual_effects'] == record.effects and value['user'] == record.query


def test_independent_segments_never_summarize_a_summary(client, person, monkeypatch):
    name, _ = person
    sid, ids = history(name, count=40, answer_size=2)
    calls = []
    def maintain(action, data):
        calls.append((action, data))
        assert action == 'compress_segment' and data['prior_summary'] == ''
        assert all('LEGACY' not in t['assistant'] for t in data['turns'])
        return providers.MaintenanceResult(summary='独立分段：保留预算和待办。', title='素材活动', overview='活动待确认。')
    monkeypatch.setattr(providers, 'maintain', maintain)
    with SessionLocal() as db:
        prefs = preferences(db, name)
        service.enqueue(db, name, 'compress', ids[-1], prefs.epoch); db.commit()
    for _ in range(2):
        job = worker.claim(name)
        assert job and worker.execute(job) == 'succeeded'
        with SessionLocal() as db:
            db.get(MemoryJob, job['id']).state = 'succeeded'; db.commit()
    assert set(calls[0][1]['source_ids']).isdisjoint(calls[1][1]['source_ids'])
    with SessionLocal() as db:
        rows = list(db.scalars(select(ContextSegment).where(ContextSegment.session_id == sid)))
        assert len(rows) == 2 and rows[0].key_points
        assert db.get(ChatSession, sid).summary == '旧滚动摘要不应再注入LEGACY'


def test_forget_removes_segments_and_blocks_raw_recovery(client, person, model):
    name, headers = person
    sid, ids = history(name, count=30, answer_size=2)
    segments(name, sid, ids)
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, name), 'a')
        item = service.create(db, actor, MemoryCreate(scope='workspace', workspace='today',
            title='活动预算', content='活动预算12345.67元'), source={
                'type':'chat', 'id':ids[0], 'role':'user', 'quote':'确认活动预算12345.67元'})
        db.commit(); item_id = item.id
    ask(client, headers, '继续', session_id=sid)
    assert client.post('/api/memory/forget', headers=headers, json={'item_ids':[item_id]}).status_code == 200
    ask(client, headers, '继续', session_id=sid)
    response = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read', 'arguments':{'run_id':ids[0], 'field':'user'}})
    assert response.status_code == 404
    with SessionLocal() as db:
        assert not db.scalar(select(ContextSegment.id).where(ContextSegment.session_id == sid))


def test_history_recovery_is_session_scoped_and_temporary_isolated(client, person, model):
    name, headers = person
    sid, ids = history(name, count=2, answer_size=1)
    ask(client, headers, '新会话')
    response = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read','arguments':{'run_id':ids[0]}})
    assert response.status_code == 404
    ask(client, headers, '临时提问', session_id=sid, temporary=True)
    assert client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read','arguments':{'run_id':ids[0]}}).status_code == 403
    with SessionLocal() as db:
        latest = db.scalar(select(MemoryRunContext).where(MemoryRunContext.session_id == sid))
        assert not latest.timings['history_sources']


def test_stale_compression_cannot_resurrect_forgotten_history(client, person, monkeypatch):
    name, _ = person
    sid, ids = history(name, count=40, answer_size=1)
    with SessionLocal() as db:
        service.enqueue(db, name, 'compress', ids[-1], preferences(db, name).epoch); db.commit()
    def delayed(*args):
        with SessionLocal() as db:
            service.invalidate(db, name)
            db.execute(update(ChatRun).where(ChatRun.id == ids[0]).values(memory_excluded=True)); db.commit()
        return providers.MaintenanceResult(summary='旧结果不应写回', overview='旧结果')
    monkeypatch.setattr(providers, 'maintain', delayed)
    assert worker.execute(worker.claim(name)) == 'obsolete'
    with SessionLocal() as db:
        assert not db.scalar(select(ContextSegment.id).where(ContextSegment.session_id == sid))


def test_history_search_recovers_detail_absent_from_summary(client, person, model):
    name, headers = person
    sid, ids = history(name, count=70, answer_size=1)
    with SessionLocal() as db:
        db.get(ChatRun, ids[9]).answer = '只在原文出现的批次编号XYZ-999'; db.commit()
    ask(client, headers, '查询旧批次', session_id=sid)
    result = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read', 'arguments':{'query':'XYZ-999'}}).json()['data']
    assert result['items'][0]['run_id'] == ids[9]


def test_budget_includes_current_query_and_output_reserve(monkeypatch):
    monkeypatch.setattr(settings, 'memory_context_tokens', 16000)
    monkeypatch.setattr(settings, 'memory_model_context_tokens', 20000)
    monkeypatch.setattr(settings, 'memory_reserved_tokens', 8000)
    assert hierarchy.context_budget('中' * 1000) == 9000


def test_oversized_recent_originals_are_rejected_instead_of_silently_truncated(client, person, model, monkeypatch):
    name, headers = person
    monkeypatch.setattr(settings, 'memory_reactive_enabled', False)
    monkeypatch.setattr(settings, 'memory_context_tokens', 6000)
    sid, ids = history(name, count=3, answer_size=1500)
    with SessionLocal() as db:
        db.get(ChatRun, ids[-1]).query = '更正：预算改为9876.54元，原先12345.67元作废。'; db.commit()
    response = client.post('/api/chat', headers=headers, json={'query':'继续当前任务','workspace':'today','session_id':sid})
    assert response.status_code == 413 and '未截断' in response.json()['detail']
    with SessionLocal() as db:
        assert db.get(ChatRun, ids[-1]).query == '更正：预算改为9876.54元，原先12345.67元作废。'


def test_oversized_old_message_does_not_block_later_segments(client, person):
    name, _ = person
    sid, ids = history(name, count=40, answer_size=1)
    with SessionLocal() as db:
        db.get(ChatRun, ids[0]).answer = '超长' * 50000; db.flush()
        data = hierarchy.compression_source(db, db.get(ChatSession, sid), 40)
        assert data and ids[0] not in data['source_ids'] and data['source_ids'][0] == ids[1]


def test_invalid_segment_is_never_injected(client, person, model):
    name, headers = person
    sid, ids = history(name, count=30, answer_size=30)
    segments(name, sid, ids)
    with SessionLocal() as db:
        segment = db.scalar(select(ContextSegment).where(ContextSegment.session_id == sid))
        segment.summary = 'FORGOTTEN-SUMMARY-MARKER'
        db.get(ChatRun, segment.source_ids[0]).memory_excluded = True; db.commit()
    events = ask(client, headers, '继续', session_id=sid)
    with SessionLocal() as db:
        assert 'FORGOTTEN-SUMMARY-MARKER' not in db.get(MemoryRunContext, events['start']['run_id']).payload


def test_old_unindexed_budget_is_retrievable_without_proactive_loading(client, person, model, monkeypatch):
    name, headers = person
    monkeypatch.setattr(settings, 'memory_context_tokens', 6000)
    sid, ids = history(name, count=100, answer_size=40)
    with SessionLocal() as db:
        db.execute(update(ChatRun).where(ChatRun.id.in_(ids[1:])).values(query='讨论活动素材安排，尚未确认发布。'))
        db.commit()
    events = ask(client, headers, '回查以前的助手原文', session_id=sid)
    with SessionLocal() as db:
        snapshot = db.get(MemoryRunContext, events['start']['run_id'])
        assert '12345.67' not in snapshot.payload and snapshot.timings['levels']['raw'] == 3
    result = client.post('/tools/read', headers=tool_headers(model), json={
        'operation':'history_read','arguments':{'query':'12345.67'}}).json()['data']
    assert result['items'][0]['run_id'] == ids[0]


def test_recent_originals_and_small_index_directory_are_loaded_without_summaries(client, person, model, monkeypatch):
    name, headers = person
    monkeypatch.setattr(settings, 'memory_context_tokens', 32000)
    sid, ids = history(name, count=40, answer_size=400)
    segments(name, sid, ids)
    events = ask(client, headers, '请回查助手原文', session_id=sid)
    with SessionLocal() as db:
        trace = db.get(MemoryRunContext, events['start']['run_id']).timings
        assert trace['levels']['raw'] == 3 and 0 < trace['levels']['index'] <= 3
        assert trace['levels']['segment'] == trace['levels']['overview'] == trace['levels']['light'] == 0
        assert trace['context_tokens_upper_bound'] <= 32000
