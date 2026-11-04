"""Default payload size, hot/cold lifecycle, indexed recovery and isolation."""
from datetime import timedelta
import pytest
from sqlalchemy import select
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import ChatRun, now
from commerce.memory import hierarchy, indexes
from commerce.memory.models import ChatSession, ContextSegment, MemoryRunContext
from test_memory import person, model, ask
from test_tiered_context import history, tool_headers


def index_for(sid, ids, *, start=1, topic='旧话题', hint='原文中有详细记录', points=None, age_days=60):
    with SessionLocal() as db:
        session = db.get(ChatSession, sid)
        row = ContextSegment(session_id=sid, epoch=session.epoch, start_seq=start, end_seq=start+len(ids)-1,
            source_ids=ids, title=topic, summary='DO-NOT-AUTOLOAD-FULL-SUMMARY'*30,
            overview=hint, key_points=points or [], entities=['SKU-001'],
            created_at=now()-timedelta(days=age_days))
        db.add(row); db.commit(); return row.id


def snapshot_for(events):
    with SessionLocal() as db:
        return db.get(MemoryRunContext, events['start']['run_id'])


def read(client, model, **arguments):
    return client.post('/tools/read', headers=tool_headers(model), json={'operation':'history_read','arguments':arguments})


def test_default_loads_exactly_three_original_turns_even_with_spare_capacity(client, person, model, monkeypatch):
    name, headers = person
    sid, ids = history(name, count=20, answer_size=1)
    monkeypatch.setattr(hierarchy, 'records_for', lambda *a,**k:pytest.fail('Default path read all old assistant bodies'))
    events = ask(client, headers, '继续', session_id=sid)
    snap = snapshot_for(events)
    assert snap.timings['recent_run_ids'] == ids[-3:]
    assert snap.timings['levels']['raw'] == 3 and snap.timings['context_strategy'] == 'indexed-v2'
    with SessionLocal() as db:
        for rid in ids[-3:]:
            row = db.get(ChatRun, rid)
            assert row.answer in snap.payload and row.query in snap.payload
        assert db.get(ChatRun, ids[0]).answer not in snap.payload


def test_cold_index_is_not_in_payload_even_if_relevant_but_is_searchable(client, person, model):
    name, headers = person
    sid, ids = history(name, count=100, answer_size=1)
    cold = index_for(sid, ids[:8], topic='绝版蓝莓计划', hint='旧批次编号可回查')
    events = ask(client, headers, '查一下绝版蓝莓计划', session_id=sid)
    snap = snapshot_for(events)
    assert cold not in snap.timings['selected_indexes'] and '绝版蓝莓计划' not in snap.payload
    result = read(client, model, query='绝版蓝莓计划').json()['data']
    assert any(item['index_id'] == cold for item in result['indexes'])
    with SessionLocal() as db:
        row = db.get(ContextSegment, cold)
        assert row.use_count == 0 and row.last_used_at is None


def test_reading_cold_index_reheats_it_and_pages_only_its_own_originals(client, person, model):
    name, headers = person
    sid, ids = history(name, count=100, answer_size=1)
    cold = index_for(sid, ids[:8], topic='早期素材')
    ask(client, headers, '查早期素材', session_id=sid)
    first = read(client, model, index_id=cold, limit=3).json()['data']
    assert [item['run_id'] for item in first['items']] == ids[:3]
    assert first['has_more'] and first['next_after_turn'] == 3
    second = read(client, model, index_id=cold, after_turn=first['next_after_turn'], limit=3).json()['data']
    assert [item['run_id'] for item in second['items']] == ids[3:6]
    result = read(client, model, index_id=cold, run_id=ids[0], field='assistant')
    assert result.status_code == 200 and result.json()['data']['text'].startswith('原文细节1：')
    assert read(client, model, index_id=cold, run_id=ids[9]).status_code == 404
    with SessionLocal() as db:
        row = db.get(ContextSegment, cold)
        assert row.use_count == 1 and row.last_used_seq == 101 and row.last_used_at is not None
    events = ask(client, headers, '继续早期素材', session_id=sid)
    assert cold in snapshot_for(events).timings['selected_indexes']


def test_proactive_index_display_does_not_refresh_usage(client, person, model):
    name, headers = person
    sid, ids = history(name, count=20, answer_size=1)
    hot = index_for(sid, ids[8:16], start=9, age_days=0)
    events = ask(client, headers, '继续', session_id=sid)
    assert hot in snapshot_for(events).timings['selected_indexes']
    with SessionLocal() as db:
        row = db.get(ContextSegment, hot)
        assert row.last_used_at is None and row.use_count == 0


def test_unfinished_task_index_stays_available_and_resolution_removes_its_pin(client, person, model):
    name, headers = person
    sid, ids = history(name, count=100, answer_size=1)
    with SessionLocal() as db:
        db.get(ChatRun, ids[0]).query = '活动报告还没完成'; db.commit()
    pending = index_for(sid, ids[:8], points=[{'run_id':ids[0], 'turn':1, 'kind':'decision','quote':'活动报告还没完成'}])
    events = ask(client, headers, '继续', session_id=sid)
    snap = snapshot_for(events)
    assert pending in snap.timings['selected_indexes'] and '活动报告还没完成' in snap.payload
    with SessionLocal() as db:
        db.get(ChatRun, ids[-1]).query = '活动报告已完成'; db.commit()
    events = ask(client, headers, '继续', session_id=sid)
    assert pending not in snapshot_for(events).timings['selected_indexes']


def test_number_of_loaded_indexes_and_hints_is_bounded_and_no_full_summary_is_sent(client, person, model):
    name, headers = person
    sid, ids = history(name, count=100, answer_size=1)
    for start in range(0, 80, 8):
        index_for(sid, ids[start:start+8], start=start+1, age_days=0)
    events = ask(client, headers, '看看旧话题', session_id=sid)
    snap = snapshot_for(events)
    assert 0 < len(snap.timings['selected_indexes']) <= settings.memory_index_active_limit
    assert 'DO-NOT-AUTOLOAD-FULL-SUMMARY' not in snap.payload
    assert snap.timings['levels']['light'] == snap.timings['levels']['segment'] == snap.timings['levels']['overview'] == 0


def test_unindexed_old_detail_can_be_found_without_its_body_in_prompt(client, person, model):
    name, headers = person
    sid, ids = history(name, count=60, answer_size=1)
    with SessionLocal() as db:
        db.get(ChatRun, ids[5]).answer = 'UNINDEXED-BATCH-7788'; db.commit()
    events = ask(client, headers, '查旧批次', session_id=sid)
    assert 'UNINDEXED-BATCH-7788' not in snapshot_for(events).payload
    result = read(client, model, query='UNINDEXED-BATCH-7788').json()['data']
    assert result['items'][0]['run_id'] == ids[5]


def test_index_cannot_be_read_across_sessions_or_after_source_exclusion(client, person, model):
    name, headers = person
    sid, ids = history(name, count=20, answer_size=1)
    index_id = index_for(sid, ids[:8])
    ask(client, headers, '新会话')
    assert read(client, model, index_id=index_id).status_code == 404
    ask(client, headers, '同一会话', session_id=sid)
    with SessionLocal() as db:
        db.get(ChatRun, ids[0]).memory_excluded = True; db.commit()
    assert read(client, model, index_id=index_id).status_code == 404
