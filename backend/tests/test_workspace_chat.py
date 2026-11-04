"""Exercise server boundaries and real page business handlers in an isolated schema."""
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from commerce.auth import issue_token, scoped_actor, password_hash
from commerce.chat_workspaces import OPERATIONS, WORKSPACES
from commerce.config import ROOT, settings
from commerce.db import SessionLocal, initialize_schema
from commerce.models import Conversation, ChatRun, User, OpsRecord, Membership, PlatformSync


def tool_headers(workspace, shop='a', actor='operator-a'):
    with SessionLocal() as db:
        token = issue_token(db, scoped_actor(db, db.get(User, actor), shop), 'tool', uuid4().hex, workspace=workspace)
    return {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token}


def invoke(client, headers, operation, arguments=None, path='/tools/execute'):
    return client.post(path, headers=headers, json={'operation': operation, 'arguments': arguments or {}})


@pytest.fixture
def model(monkeypatch):
    from commerce import main
    requests = []

    class Response:
        status_code = 200
        def __init__(self, body): self.body = body
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            cid = self.body['conversation_id'] or 'column-' + uuid4().hex
            for event in [{'event': 'message', 'answer': '答复：' + self.body['query']}, {'event': 'message_end'}]:
                yield 'data: ' + json.dumps({**event, 'conversation_id': cid})

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            requests.append(kwargs['json'])
            return Response(kwargs['json'])

    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(settings, 'dify_operator_key', 'workspace-test')
    return requests


def test_sidebar_modules_keep_separate_persistent_model_conversations(client, auth, model):
    headers = {**auth(), 'X-Shop-ID': 'a'}
    ids = {}
    marker = uuid4().hex
    for workspace in WORKSPACES:
        query = marker + workspace
        result = client.post('/api/chat', headers=headers, json={'query': query, 'workspace': workspace})
        assert '"status": "succeeded"' in result.text
        row = client.get('/api/chat/runs', headers=headers, params={'workspace': workspace}).json()['items'][0]
        ids[workspace] = row['conversation_id']
        assert row['query'] == query and row['workspace'] == workspace
        assert model[-1]['inputs']['workspace'] == workspace
        with SessionLocal() as db:
            conv = db.get(Conversation, ids[workspace])
            assert conv.workspace == workspace
    assert len(set(ids.values())) == len(WORKSPACES)
    # Reloading and continuing an earlier column uses its own upstream conversation.
    result = client.post('/api/chat', headers=headers, json={
        'query': '继续今日工作', 'workspace': 'today', 'conversation_id': ids['today']})
    assert '"status": "succeeded"' in result.text
    assert model[-1]['conversation_id'] == ids['today']
    assert client.get('/api/chat/conversations', headers=headers, params={'workspace': 'today'}).json()['items'][0]['id'] == ids['today']
    # A client-supplied ID cannot override the column or store binding.
    for workspace, shop in [('ops-videos', 'a'), ('today', 'b')]:
        r = client.post('/api/chat', headers={**headers, 'X-Shop-ID': shop}, json={
            'query': '不能串上下文', 'workspace': workspace, 'conversation_id': ids['today']})
        assert r.status_code == 409
    foreign = {**auth('operator-b'), 'X-Shop-ID': 'a'}
    assert client.post('/api/chat', headers=foreign, json={
        'query': '不能串账号', 'workspace': 'today', 'conversation_id': ids['today']}).status_code == 404
    assert client.post('/api/chat', headers=headers, json={'query': '无效栏目', 'workspace': 'made-up'}).status_code == 422


def test_legacy_rows_remain_general_after_idempotent_migration(client, auth):
    marker = 'legacy-' + uuid4().hex
    with SessionLocal() as db:
        db.add(ChatRun(user_id='operator-a', shop_id='a', query=marker, answer='旧版内容', status='succeeded'))
        db.commit()
    initialize_schema(); initialize_schema()
    headers = {**auth(), 'X-Shop-ID': 'a'}
    assert any(r['query'] == marker for r in client.get('/api/chat/runs', headers=headers).json()['items'])
    assert not any(r['query'] == marker for r in client.get('/api/chat/runs', headers=headers, params={'workspace': 'today'}).json()['items'])


@pytest.mark.parametrize('workspace,operation,args,status', [
    ('orders', 'ops_sop_generate', {}, 403),
    ('ops-analysis', 'ops_import_commit', {'batch_id': 'anything'}, 403),
    ('catalog', 'platform_sync', {'connection_id': 'anything'}, 403),
    ('ops-videos', 'ops_script_review', {'script_id': 'absent', 'expected_version': 1}, 404),
    ('today', 'ops_schedule_set', {'shop_id': 'b', 'enabled': True}, 403),
    ('platform', 'platform_sync', {'connection_id': 'absent'}, 404),
])
def test_tools_enforce_column_store_and_resource_scope(client, workspace, operation, args, status):
    r = invoke(client, tool_headers(workspace), operation, args)
    assert r.status_code == status, r.text


def test_new_write_cannot_use_read_only_endpoint_or_refund_alias(client):
    headers = tool_headers('drafts')
    assert invoke(client, headers, 'content_save', {'kind': 'report', 'title': '报告', 'content': '合法报告内容'}, '/tools/read').status_code == 403
    assert invoke(client, headers, 'refund', {'order_id': 'x'}).status_code == 422
    assert invoke(client, headers, 'publish', {}).status_code == 422
    assert invoke(client, headers, 'workspace_context', {}).json()['data']['workspace'] == 'drafts'


def test_content_save_and_schedule_tools_have_real_page_effects(client, auth):
    headers = {**auth(), 'X-Shop-ID': 'a'}
    content = {'kind': 'report', 'title': uuid4().hex, 'content': '聊天保存的经营报告，验证实际落库。'}
    tools = tool_headers('drafts')
    first = invoke(client, tools, 'content_save', content)
    assert first.status_code == 200, first.text
    draft_id = first.json()['data']['id']
    assert invoke(client, tools, 'content_save', content).json()['data']['id'] == draft_id
    assert draft_id in {r['id'] for r in client.get('/api/drafts', headers=headers).json()['items']}
    schedule = invoke(client, tool_headers('today'), 'ops_schedule_set', {'shop_id': 'a', 'enabled': False, 'hour': 11})
    assert schedule.status_code == 200, schedule.text
    item = next(r for r in client.get('/api/ops/schedules', headers=headers).json()['items'] if r['shop_id'] == 'a')
    assert item['hour'] == 11 and not item['enabled']


def test_chat_import_previews_and_commits_same_batch_as_page(client, auth):
    row = json.loads((ROOT / 'data/douyin-ops-v1/products.jsonl').read_text().splitlines()[0])
    row['sku_id'] = 'CHAT-IMPORT-' + uuid4().hex[:12]
    headers = tool_headers('ops-imports')
    body = {'kind': 'products', 'filename': 'chat-import.json', 'text': json.dumps([row])}
    inspection = invoke(client, headers, 'ops_import_inspect', body)
    assert inspection.status_code == 200, inspection.text
    response = invoke(client, headers, 'ops_import_preview', body)
    assert response.status_code == 200, response.text
    batch = response.json()['data']
    assert batch['error_count'] == 0
    assert invoke(client, tool_headers('ops-imports', 'b'), 'ops_import_batch', {'batch_id': batch['id']}).status_code == 404
    assert invoke(client, headers, 'ops_import_batch', {'batch_id': batch['id']}).json()['data']['preview'][0]['sku_id'] == row['sku_id']
    result = invoke(client, headers, 'ops_import_commit', {'batch_id': batch['id']})
    assert result.status_code == 200 and result.json()['data']['imported'] == 1, result.text
    repeated = invoke(client, headers, 'ops_import_commit', {'batch_id': batch['id']})
    assert repeated.json()['data']['duplicate_batch']
    with SessionLocal() as db:
        assert db.scalar(select(OpsRecord).where(OpsRecord.external_id == row['sku_id']))


def test_contracts_and_tool_schema_include_every_adapter():
    from typing import get_args
    from commerce.schemas import ToolCall
    assert set(OPERATIONS) <= set(get_args(ToolCall.model_fields['operation'].annotation))
    for actions in [False, True]:
        schema = json.loads((ROOT / ('contracts/dify-action-tools.json' if actions else 'contracts/dify-read-tools.json')).read_text())
        path = '/tools/execute' if actions else '/tools/read'
        ops = schema['paths'][path]['post']['requestBody']['content']['application/json']['schema']['properties']['operation']['enum']
        assert {op for op, (kind, _) in OPERATIONS.items() if (kind == 'write') == actions} <= set(ops)


def test_page_read_adapters_and_cross_column_run_summaries(client):
    for operation in ['ops_schedules', 'ops_assets', 'ops_imports', 'ops_import_mappings',
                      'content_list', 'audit_events', 'chat_run_records']:
        result = invoke(client, tool_headers('audit'), operation)
        assert result.status_code == 200 and isinstance(result.json()['data']['items'], list), result.text
    assert invoke(client, tool_headers('today'), 'chat_run_records', {'workspace':'drafts'}).status_code == 403
    result = invoke(client, tool_headers('ops-imports'), 'ops_import_fields', {'kind':'daily_reports'})
    assert result.status_code == 200 and result.json()['data']['fields']


def test_platform_chat_uses_real_business_handlers_without_exposing_credentials(client, auth):
    from commerce import platform_service
    actor = 'workspace-platform-' + uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='栏目平台测试',
                    password_hash=password_hash(settings.demo_password)))
        db.flush(); db.add(Membership(user_id=actor, shop_id='a')); db.commit()
    headers = auth(actor)
    response = client.post('/api/platform/connections', headers=headers, json={
        'name':'聊天平台演练','provider':'doudian','mode':'simulation','external_id':'SIM-CHAT',
        'app_key':'qa-key-must-not-reach-model','app_secret':'qa-secret-must-not-reach-model'})
    assert response.status_code == 200, response.text
    connection = response.json()
    tools = tool_headers('platform', connection['shop_id'], actor)
    result = invoke(client, tools, 'platform_connections')
    assert result.status_code == 200 and result.json()['data']['items']
    assert 'qa-key' not in result.text and 'qa-secret' not in result.text
    assert 'app_key' not in result.text and 'credentials' not in result.text
    assert invoke(client, tools, 'platform_connect', {'connection_id':connection['id']}).status_code == 200
    queued = invoke(client, tools, 'platform_sync', {'connection_id':connection['id'],'days':7})
    assert queued.status_code == 200 and queued.json()['data']['status'] == 'queued', queued.text
    with SessionLocal() as db:
        job = db.get(PlatformSync, queued.json()['data']['id'])
        job.status, job.attempts = 'running', 1; db.commit()
        platform_service.execute_job(db, job)
    history = invoke(client, tools, 'platform_syncs', {'connection_id':connection['id']})
    assert history.json()['data']['items'][0]['status'] == 'succeeded'
    products = invoke(client, tools, 'platform_products', {'connection_id':connection['id']})
    product = products.json()['data']['items'][0]
    result = invoke(client, tools, 'platform_product_facts_set', {
        'connection_id':connection['id'],'product_id':product['sku_id'],'cost_fen':10000,'evidence':'测试供货单事实'})
    assert result.status_code == 200 and result.json()['data']['cost_fen'] == 10000, result.text
    result = invoke(client, tools, 'platform_schedule_set', {'connection_id':connection['id'],'enabled':False})
    assert result.status_code == 200 and not result.json()['data']['enabled']
    result = invoke(client, tools, 'platform_disconnect', {'connection_id':connection['id']})
    assert result.status_code == 200 and result.json()['data']['status'] == 'disconnected'
    # An authorized operator's other store still cannot use this connection through a scoped tool token.
    assert invoke(client, tool_headers('platform','a',actor), 'platform_sync', {'connection_id':connection['id']}).status_code == 404
    assert invoke(client, tool_headers('platform',connection['shop_id'],actor), 'platform_sync', {
        'connection_id':connection['id'],'access_token':'not-a-supported-chat-argument'}).status_code == 422
