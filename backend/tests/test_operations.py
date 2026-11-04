"""Operations contracts, financial reconciliation and real media execution."""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.auth import password_hash
from commerce.models import OpsRecord, OpsTask, OpsIssue, OpsAsset, OpsJob, OpsSchedule, User, Membership


@pytest.fixture(scope='module')
def ops(database, tmp_path_factory):
    from commerce.main import app
    from commerce import ops_media
    patch = pytest.MonkeyPatch()
    patch.setattr(ops_media, 'MEDIA_ROOT', tmp_path_factory.mktemp('media'))
    with TestClient(app) as client:
        login = client.post('/api/auth/login', json={'username': 'operator', 'password': settings.demo_password}).json()
        headers = {'Authorization': 'Bearer ' + login['access_token']}
        response = client.post('/api/ops/demo/import', headers=headers)
        assert response.status_code == 200, response.text
        yield client, headers
    patch.undo()


def test_imported_financials_match_independent_fixture_truth(ops):
    client, headers = ops
    expected = json.loads((ROOT / 'data/douyin-ops-v1/expected_results.json').read_text())
    result = client.get('/api/ops/overview', headers=headers).json()
    assert result['complete']
    for field in ['paid_fen', 'refund_fen', 'net_receipts_fen', 'paid_parent_orders', 'paid_sub_orders']:
        assert result['total'][field] == expected['total'][field]
    assert result['reconciliation'] == {'payments': True, 'refunds': True}
    assert result['by_shop'][0]['growth_percent'] == -52.57
    videos = client.get('/api/ops/videos', headers=headers).json()['items']
    viral = next(r for r in videos if r['video_id'] == 'TEST-VIDEO-a-29')
    assert viral['click_rate'] == .001 and viral['warning']
    missing = next(r for r in videos if r['video_id'] == 'TEST-VIDEO-b-29')
    assert missing['completion_rate'] is None


def test_import_preview_commit_and_duplicate_are_atomic(ops):
    client, headers = ops
    original = client.get('/api/ops/overview', headers=headers).json()['total']
    path = ROOT / 'data/douyin-ops-v1/daily_reports.csv'
    body = {'kind': 'daily_reports', 'filename': path.name, 'text': path.read_text(encoding='utf-8-sig')}
    preview = client.post('/api/ops/imports/preview', headers=headers, json=body).json()
    assert preview['error_count'] == 0 and preview['valid_rows'] == 120
    first = client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers)
    assert first.status_code == 200, first.text
    repeated = client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers).json()
    assert repeated['imported'] == 0 and repeated['duplicate_batch']
    assert client.get('/api/ops/overview', headers=headers).json()['total'] == original
    row = json.loads((ROOT / 'data/douyin-ops-v1/orders.jsonl').read_text().splitlines()[0])
    row['paid_fen'] = -100
    bad = client.post('/api/ops/imports/preview', headers=headers,
        json={'kind': 'orders', 'filename': 'bad.json', 'text': json.dumps([row])}).json()
    assert bad['error_count'] == 1
    assert client.post(f'/api/ops/imports/{bad["id"]}/commit', headers=headers).status_code == 422
    assert client.get('/api/ops/overview', headers=headers).json()['total'] == original


def test_sop_generation_preserves_progress_and_versions(ops):
    client, headers = ops
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    first = client.post('/api/ops/tasks/generate', headers=headers, json={'date': tomorrow}).json()
    assert first['created'] == 16
    task = first['items'][0]
    response = client.patch('/api/ops/tasks/' + task['id'], headers=headers,
        json={'expected_version': task['version'], 'status': 'in_progress', 'owner': '测试运营'})
    assert response.status_code == 200
    again = client.post('/api/ops/tasks/generate', headers=headers, json={'date': tomorrow}).json()
    assert again['created'] == 0
    assert next(r for r in again['items'] if r['id'] == task['id'])['owner'] == '测试运营'
    assert client.patch('/api/ops/tasks/' + task['id'], headers=headers,
        json={'expected_version': task['version'], 'status': 'completed', 'evidence': '已核对报表'}).status_code == 409


def test_task_completion_requires_evidence_but_not_retired_issues(ops):
    client, headers = ops
    task = next(r for r in client.get('/api/ops/tasks', headers=headers).json()['items'] if r['status'] == 'pending')
    # An archived issue must no longer block the remaining task workflow.
    with SessionLocal() as db:
        db.add(OpsIssue(shop_id=task['shop_id'], task_id=task['id'], title='旧版遗留记录', detail='', owner='归档', status='open'))
        db.commit()
    assert client.patch('/api/ops/tasks/' + task['id'], headers=headers,
        json={'expected_version': task['version'], 'status': 'completed'}).status_code == 422
    assert client.patch('/api/ops/tasks/' + task['id'], headers=headers,
        json={'expected_version': task['version'], 'status': 'completed', 'evidence': '核查记录 test-001'}).status_code == 200
    daily = client.get('/api/ops/daily-report', headers=headers).json()
    assert 'open_issues' not in daily and 'resolved_issues' not in daily


def test_real_render_is_persistent_idempotent_and_has_outputs(ops):
    from commerce.ops_worker import tick
    from commerce import ops_media
    client, headers = ops
    response = client.post('/api/ops/scripts/TEST-SCRIPT-a/render', headers=headers, json={'expected_version': 1})
    assert response.status_code == 200, response.text
    job_id = response.json()['id']
    assert client.post('/api/ops/scripts/TEST-SCRIPT-a/render', headers=headers, json={'expected_version': 1}).json()['id'] == job_id
    assert tick()
    job = next(r for r in client.get('/api/ops/jobs', headers=headers).json()['items'] if r['id'] == job_id)
    assert job['status'] == 'succeeded', job
    assert abs(job['result']['duration_seconds'] - 12) < .2
    for kind in ['video', 'cover', 'subtitles']:
        assert client.get(f'/api/ops/jobs/{job_id}/{kind}', headers=headers).status_code == 200
        assert client.get(f'/api/ops/jobs/{job_id}/{kind}').status_code == 401
    assert '通勤穿搭' in client.get(f'/api/ops/jobs/{job_id}/subtitles', headers=headers).text


def test_dify_render_arguments_keep_authorization_versions_and_idempotency(ops):
    from uuid import uuid4
    from commerce.auth import issue_token, scoped_actor
    from commerce.models import Audit

    client, headers = ops
    run_id = uuid4().hex
    with SessionLocal() as db:
        context = issue_token(db, scoped_actor(db, db.get(User, 'operator-a'), 'a'),
                              'tool', run_id, workspace='ops-videos')
    tools = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': context}
    arguments = {'expected_version': 1, 'script_id': 'TEST-SCRIPT-a'}
    jobs = []
    for body in [arguments, json.dumps(arguments), repr(arguments)]:
        response = client.post('/tools/execute', headers=tools,
            json={'operation': 'ops_render', 'arguments': body})
        assert response.status_code == 200, response.text
        jobs.append(response.json()['data']['id'])
    assert len(set(jobs)) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(
            Audit.request_id == run_id, Audit.operation == 'tool.ops_render')) == 3

    for args, status in [
        ({**arguments, 'expected_version': 999}, 409),
        ({**arguments, 'script_id': 'TEST-SCRIPT-b'}, 404),
        ({**arguments, 'unexpected': True}, 422),
    ]:
        response = client.post('/tools/execute', headers=tools,
            json={'operation': 'ops_render', 'arguments': repr(args)})
        assert response.status_code == status, response.text
    assert client.post('/tools/read', headers=tools,
        json={'operation': 'ops_render', 'arguments': repr(arguments)}).status_code == 403


def test_missing_media_fails_and_can_retry(ops):
    from commerce.ops_worker import tick
    client, headers = ops
    scripts = client.get('/api/ops/scripts', headers=headers).json()['items']
    source = next(s for s in scripts if s['id'] == 'TEST-SCRIPT-b')
    queued = client.post('/api/ops/scripts/TEST-SCRIPT-b/render', headers=headers, json={'expected_version': 1}).json()
    with SessionLocal() as db:
        asset = db.get(OpsAsset, 'TEST-ASSET-b-1')
        original = asset.relative_path
        asset.relative_path = 'assets/missing-test.mp4'
        db.commit()
    assert tick()
    row = next(r for r in client.get('/api/ops/jobs', headers=headers).json()['items'] if r['id'] == queued['id'])
    assert row['status'] == 'failed' and '缺少' in row['error']
    with SessionLocal() as db:
        db.get(OpsAsset, 'TEST-ASSET-b-1').relative_path = original
        db.commit()
    assert client.post('/api/ops/jobs/' + row['id'] + '/retry', headers=headers).status_code == 200
    assert tick()
    retried = next(r for r in client.get('/api/ops/jobs', headers=headers).json()['items'] if r['id'] == row['id'])
    assert retried['status'] == 'succeeded' and retried['attempts'] == 2


def test_scope_blocks_reports_assets_and_mutations(ops):
    client, headers = ops
    with SessionLocal() as db:
        actor = User(id='ops-restricted', username='ops-restricted', role='operator', shop_id='a',
                     display_name='单店运营', password_hash=password_hash(settings.demo_password))
        db.add(actor)
        db.flush()
        db.add(Membership(user_id=actor.id, shop_id='a'))
        db.commit()
    login = client.post('/api/auth/login', json={'username': 'ops-restricted', 'password': settings.demo_password}).json()
    restricted = {'Authorization': 'Bearer ' + login['access_token']}
    result = client.get('/api/ops/overview', headers=restricted).json()
    assert {r['shop_id'] for r in result['by_shop']} == {'a'}
    assert client.get('/api/ops/assets/TEST-ASSET-b-0/video', headers=restricted).status_code == 404
    assert client.post('/api/ops/scripts/TEST-SCRIPT-b/render', headers=restricted, json={'expected_version': 1}).status_code == 404
    row = json.loads((ROOT / 'data/douyin-ops-v1/daily_reports.jsonl').read_text().splitlines()[1])
    assert client.post('/api/ops/imports/preview', headers=restricted,
        json={'kind': 'daily_reports', 'filename': 'b.json', 'text': json.dumps([row])}).status_code == 403


def test_retired_modules_have_no_endpoints_tools_or_imports(ops):
    from commerce.schemas import ToolCall
    from commerce.ops_tools import READ_OPERATIONS, WRITE_OPERATIONS
    from commerce.models import OpsImport
    from pydantic import ValidationError
    client, headers = ops
    for method, path in [('GET', '/api/ops/issues'), ('POST', '/api/ops/issues'),
                         ('PATCH', '/api/ops/issues/old'), ('GET', '/api/ops/effects'),
                         ('POST', '/api/ops/scripts/TEST-SCRIPT-a/publication')]:
        assert client.request(method, path, headers=headers, json={}).status_code == 404
    for name in ['ops_issues', 'ops_effects', 'ops_issue_create', 'ops_issue_update']:
        assert name not in READ_OPERATIONS | WRITE_OPERATIONS
        with pytest.raises(ValidationError):
            ToolCall(operation=name, arguments={})
    assert 'issues' not in client.get('/api/ops/imports', headers=headers).json()['kinds']
    assert client.get('/api/ops/templates/issues', headers=headers).status_code == 404
    assert client.post('/api/ops/imports/preview', headers=headers,
        json={'kind': 'issues', 'filename': 'old.json', 'text': '[{}]'}).status_code == 422
    with SessionLocal() as db:
        batch = OpsImport(user_id='operator-a', shop_scope=['a','b'], kind='issues', filename='old.json',
                          fingerprint='retired-issues-batch', rows=[], errors=[])
        db.add(batch)
        db.flush()
        batch_id = batch.id
        db.commit()
    assert client.post(f'/api/ops/imports/{batch_id}/commit', headers=headers).status_code == 410
    assert batch_id not in [r['id'] for r in client.get('/api/ops/imports', headers=headers).json()['items']]


def test_cumulative_snapshots_are_not_added(ops):
    client, headers = ops
    source = json.loads((ROOT / 'data/douyin-ops-v1/videos.jsonl').read_text().splitlines()[0])
    source['video_id'] = 'TEST-CUMULATIVE-VIDEO'
    def ingest(kind, rows):
        p = client.post('/api/ops/imports/preview', headers=headers,
            json={'kind': kind, 'filename': 'snapshots.json', 'text': json.dumps(rows)}).json()
        assert p['error_count'] == 0, p
        result = client.post(f'/api/ops/imports/{p["id"]}/commit', headers=headers)
        assert result.status_code == 200, result.text
    ingest('videos', [source])
    metric = json.loads((ROOT / 'data/douyin-ops-v1/video_daily.jsonl').read_text().splitlines()[0])
    rows = [{**metric, 'video_id': source['video_id'], 'metric_grain': 'cumulative', 'views': views,
             'completed_views': 500, 'product_clicks': 20, 'extracted_at': f'2026-10-16T0{i+8}:00:00+08:00'}
            for i, views in enumerate([1000, 1500])]
    ingest('video_daily', rows)
    actual = next(v for v in client.get('/api/ops/videos', headers=headers).json()['items'] if v['video_id'] == source['video_id'])
    assert actual['views'] == 1500
    assert actual['click_rate'] == 20 / 1500


def test_missing_day_is_not_reported_as_zero_or_complete(ops):
    client, headers = ops
    with SessionLocal() as db:
        row = db.scalar(select(OpsRecord).where(OpsRecord.shop_id == 'b', OpsRecord.kind == 'daily_reports', OpsRecord.external_id == '2026-10-15'))
        saved = dict(shop_id=row.shop_id, kind=row.kind, external_id=row.external_id, payload=row.payload)
        db.delete(row)
        db.commit()
    try:
        result = client.get('/api/ops/overview', headers=headers).json()
        shop = next(s for s in result['by_shop'] if s['shop_id'] == 'b')
        assert not result['complete'] and not shop['complete']
        assert shop['missing_dates'] == ['2026-10-15'] and shop['paid_fen'] > 0
        assert shop['growth_percent'] is None and result['total']['growth_percent'] is None
        assert result['trend'][-1]['paid_fen'] is None
    finally:
        with SessionLocal() as db:
            db.add(OpsRecord(**saved))
            db.commit()


def test_growth_requires_complete_previous_window(ops):
    client, headers = ops
    with SessionLocal() as db:
        row = db.scalar(select(OpsRecord).where(OpsRecord.shop_id == 'a', OpsRecord.kind == 'daily_reports', OpsRecord.external_id == '2026-10-08'))
        saved = dict(shop_id=row.shop_id, kind=row.kind, external_id=row.external_id, payload=row.payload)
        db.delete(row)
        db.commit()
    try:
        result = client.get('/api/ops/overview', headers=headers).json()
        shop = next(s for s in result['by_shop'] if s['shop_id'] == 'a')
        assert shop['complete'] and shop['previous_missing_dates'] == ['2026-10-08']
        assert shop['growth_percent'] is None and result['total']['growth_percent'] is None
    finally:
        with SessionLocal() as db:
            db.add(OpsRecord(**saved))
            db.commit()


def test_scheduled_generation_and_write_tool_boundary(ops):
    from commerce.ops_worker import tick
    from commerce.auth import issue_token, scoped_actor
    from commerce.models import now, uid
    from commerce.ops_data import ZONE
    client, headers = ops
    with SessionLocal() as db:
        schedule = db.get(OpsSchedule, 'a')
        schedule.hour, schedule.enabled, schedule.last_date = now().astimezone(ZONE).hour, True, None
        db.commit()
    tick()
    with SessionLocal() as db:
        assert db.get(OpsSchedule, 'a').last_date == now().astimezone(ZONE).date()
        count = db.scalar(select(func.count()).select_from(OpsTask))
        token = issue_token(db, scoped_actor(db, db.get(User, 'operator-a')), 'tool', uid())
    tick()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OpsTask)) == count
    tool_headers = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token}
    assert client.post('/tools/read', headers=tool_headers, json={'operation': 'ops_sop_generate', 'arguments': {}}).status_code == 403
    assert client.post('/tools/execute', headers=tool_headers, json={'operation': 'ops_sop_generate', 'arguments': {}}).status_code == 200


@pytest.mark.parametrize('kind,text', [('daily_reports', 'shop_id,date\na,2026-10-16,extra'), ('sop_templates', '[null]')])
def test_malformed_import_rows_are_reported_and_cannot_commit(ops, kind, text):
    client, headers = ops
    response = client.post('/api/ops/imports/preview', headers=headers,
        json={'kind': kind, 'filename': 'bad.csv' if kind == 'daily_reports' else 'bad.json', 'text': text})
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['valid_rows'] == 0 and preview['error_count'] > 0
    assert client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers).status_code == 422


def test_whitespace_identifiers_are_not_imported(ops):
    client, headers = ops
    row = json.loads((ROOT / 'data/douyin-ops-v1/videos.jsonl').read_text().splitlines()[0])
    row['video_id'] = '   '
    response = client.post('/api/ops/imports/preview', headers=headers,
        json={'kind': 'videos', 'filename': 'blank.json', 'text': json.dumps([row])})
    assert response.status_code == 200
    preview = response.json()
    assert preview['error_count'] == 1 and 'video_id' in preview['errors'][0]['message']


def test_specific_workday_remains_available_after_many_newer_tasks(ops):
    from commerce.models import now
    client, headers = ops
    later = date.today() + timedelta(days=2)
    with SessionLocal() as db:
        db.add_all([OpsTask(shop_id='a', work_date=later, template_id=f'QA-BULK-{i}', title='临时隔离测试任务',
                           owner='测试', due_at=now() + timedelta(days=2), status='pending') for i in range(301)])
        db.commit()
    response = client.get('/api/ops/tasks?day=2026-10-14', headers=headers)
    assert response.status_code == 200
    rows = response.json()['items']
    assert len(rows) == 16 and all(r['date'] == '2026-10-14' for r in rows)


def test_agent_lists_newest_scripts_and_render_jobs(ops):
    from commerce import ops_tools
    from commerce.auth import scoped_actor
    from commerce.models import OpsScript, now, uid
    with SessionLocal() as db:
        timestamp = now()
        ids = []
        job_ids = []
        for i in range(31):
            script = OpsScript(shop_id='a', user_id='operator-a', title=f'测试脚本{i}', payload={},
                               created_at=timestamp + timedelta(seconds=i))
            db.add(script)
            db.flush()
            job = OpsJob(shop_id='a', user_id='operator-a', script_id=script.id, fingerprint=uid(), payload={},
                         status='succeeded', created_at=timestamp + timedelta(seconds=i))
            db.add(job)
            db.flush()
            ids.append(script.id)
            job_ids.append(job.id)
        db.commit()
        actor = scoped_actor(db, db.get(User, 'operator-a'))
        scripts = ops_tools.execute(db, actor, 'ops_scripts', {})['items']
        jobs = ops_tools.execute(db, actor, 'ops_jobs', {})['items']
        assert [r['id'] for r in scripts] == list(reversed(ids[-30:]))
        assert [r['id'] for r in jobs] == list(reversed(job_ids[-30:]))
