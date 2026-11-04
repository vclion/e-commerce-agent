"""Failure injection against real isolated PostgreSQL transactions; no platform/model calls."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from commerce import recovery
from commerce.auth import issue_token, password_hash, scoped_actor, scope_fingerprint
from commerce.config import settings
from commerce.db import SessionLocal, initialize_schema
from commerce.models import AccessToken, Audit, ChatRun, Draft, Membership, ToolExecution, User, now


@pytest.fixture
def person(client, auth):
    name = 'recovery-' + uuid4().hex[:16]
    with SessionLocal() as db:
        db.add(User(id=name, username=name, role='operator', shop_id='a', display_name='恢复测试',
                    password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add_all([Membership(user_id=name, shop_id='a'), Membership(user_id=name, shop_id='b')])
        db.commit()
    return name, {**auth(name), 'X-Shop-ID': 'a'}


def frames(response):
    assert response.status_code == 200, response.text
    return {part.splitlines()[0][7:]: json.loads(part.splitlines()[1][6:])
            for part in response.text.split('\n\n') if part.strip()}


def scripted(monkeypatch, sequences, before=None):
    from commerce import main
    requests = []
    class Response:
        status_code = 200
        def __init__(self, body, index): self.body, self.index = body, index
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            if before:
                await asyncio.to_thread(before, self.body, self.index)
            for item in sequences[min(self.index, len(sequences)-1)]:
                yield 'data: ' + json.dumps({**item, 'conversation_id': 'recover-cid-' + uuid4().hex
                    if not self.body['conversation_id'] else self.body['conversation_id']})
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            body = kwargs['json']; requests.append(body)
            return Response(body, len(requests)-1)
    monkeypatch.setattr(main.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(settings, 'dify_operator_key', 'offline-recovery-test')
    return requests


OK = [{'event': 'message', 'answer': '已返回完整答复。'}, {'event': 'message_end'}]
FAILED = [{'event': 'message', 'answer': '已有部分答复'}, {'event': 'error', 'message': 'connection timeout'}]


def tools_for(name):
    rid = uuid4().hex
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, name), 'a')
        db.add(ChatRun(id=rid, user_id=name, shop_id='a', workspace='drafts',
            scope_fingerprint=scope_fingerprint(actor), query='保存报告', status='running', updated_at=now()))
        token = issue_token(db, actor, 'tool', rid, workspace='drafts')
    return rid, {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token}


def save_body():
    return {'operation': 'content_save', 'arguments': {'kind': 'report',
        'title': '恢复回归-' + uuid4().hex, 'content': '仅用于离线恢复测试的合成报告。'}}


def test_same_request_id_returns_existing_run_without_model_call(client, person, monkeypatch):
    name, headers = person
    requests = scripted(monkeypatch, [OK])
    body = {'query': '分析经营', 'workspace': 'today', 'client_request_id': uuid4().hex}
    first = frames(client.post('/api/chat', headers=headers, json=body))
    monkeypatch.setattr(settings, 'dify_operator_key', '')  # recovery also works during model outage
    replay = frames(client.post('/api/chat', headers=headers, json=body))
    assert first['start']['run_id'] == replay['start']['run_id']
    assert replay['start']['replayed'] and len(requests) == 1
    assert replay['delta']['text'] == '已返回完整答复。'
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(ChatRun).where(ChatRun.user_id == name)) == 1
    assert client.post('/api/chat', headers=headers, json={**body, 'query': '不同问题'}).status_code == 409
    assert client.post('/api/chat', headers=headers, json={**body, 'workspace': 'drafts'}).status_code == 409
    assert client.post('/api/chat', headers={**headers, 'X-Shop-ID': 'b'}, json=body).status_code == 409


def test_safe_retry_creates_one_child_and_earlier_retry_follows_it(client, person, monkeypatch):
    _, headers = person
    requests = scripted(monkeypatch, [FAILED, OK])
    body = {'query': '分析经营', 'workspace': 'today'}
    failed = frames(client.post('/api/chat', headers=headers, json=body))
    rid = failed['start']['run_id']
    assert failed['recovery']['can_retry']
    retried = frames(client.post('/api/chat', headers=headers, json={**body, 'retry_of': rid}))
    again = frames(client.post('/api/chat', headers=headers, json={**body, 'retry_of': rid}))
    assert retried['done']['status'] == 'succeeded' and len(requests) == 2
    assert retried['start']['run_id'] == again['start']['run_id'] != rid
    assert again['start']['replayed']


def test_committed_write_and_failed_reply_are_reconciled_without_reexecution(client, person, monkeypatch):
    name, headers = person
    body = save_body()
    saved = []
    def write(request, index):
        result = client.post('/tools/execute', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
            'X-Context-Ref': request['inputs']['context_ref']}, json=body)
        assert result.status_code == 200, result.text
        saved.append(result.json()['data']['id'])
    requests = scripted(monkeypatch, [FAILED], before=write)
    query = {'query': '保存经营报告', 'workspace': 'drafts'}
    first = frames(client.post('/api/chat', headers=headers, json=query))
    again = frames(client.post('/api/chat', headers=headers, json={**query, 'retry_of': first['start']['run_id']}))
    assert len(requests) == len(saved) == 1
    assert not again['recovery']['can_retry'] and again['recovery']['business_status'] == 'completed'
    assert again['effects'][0]['resource_id'] == saved[0]
    assert again['recovery']['operations'][0]['resource_id'] == saved[0]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Draft).where(Draft.user_id == name)) == 1


def test_repeated_write_uses_durable_receipt_and_does_not_consume_budget(client, person, monkeypatch):
    from commerce import workspace_tools
    name, _ = person
    rid, tools = tools_for(name)
    body = save_body()
    original, calls = workspace_tools.execute, []
    def count(*args, **kwargs): calls.append(1); return original(*args, **kwargs)
    monkeypatch.setattr(workspace_tools, 'execute', count)
    first = client.post('/tools/execute', headers=tools, json=body)
    assert first.status_code == 200, first.text
    # Changing JSON key order must not change the idempotency identity.
    body['arguments'] = dict(reversed(list(body['arguments'].items())))
    for _ in range(9):
        result = client.post('/tools/execute', headers=tools, json=body)
        assert result.status_code == 200 and result.json() == first.json()
    assert len(calls) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Audit).where(
            Audit.request_id == rid, Audit.operation == 'tool.attempt')) == 1
        row = db.scalar(select(ToolExecution).where(ToolExecution.run_id == rid))
        assert row.state == 'succeeded'
        db.get(ChatRun, rid).status = 'interrupted'; db.commit()
    assert client.post('/tools/execute', headers=tools, json=body).status_code == 200
    assert client.post('/tools/execute', headers=tools, json=save_body()).status_code == 409


def test_crash_gap_after_business_commit_blocks_duplicate_write(client, person, monkeypatch):
    from commerce import workspace_tools
    name, headers = person
    rid, tools = tools_for(name)
    body = save_body()
    original = workspace_tools.execute
    def committed_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('injected process failure after business commit')
    monkeypatch.setattr(workspace_tools, 'execute', committed_then_crash)
    with pytest.raises(RuntimeError):
        client.post('/tools/execute', headers=tools, json=body)
    with SessionLocal() as db:
        assert db.scalar(select(ToolExecution).where(ToolExecution.run_id == rid)).state == 'uncertain'
        run = db.get(ChatRun, rid); run.status = 'interrupted'; db.commit()
    assert client.post('/tools/execute', headers=tools, json=body).status_code == 409
    row = client.get('/api/chat/runs', headers=headers, params={'workspace': 'drafts', 'run_id': rid}).json()['items'][0]
    assert row['recovery']['business_status'] == 'unknown' and not row['recovery']['can_retry']
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Draft).where(Draft.user_id == name)) == 1


def test_uncommitted_failure_rolls_back_business_change_and_is_safe_to_retry(client, person, monkeypatch):
    from commerce import workspace_tools
    name, _ = person
    rid, tools = tools_for(name)
    def uncommitted(db, user, *args):
        db.add(Draft(user_id=name, shop_id='a', kind='report', payload={'title': '不得提交'}, version=1, status='draft'))
        raise ValueError('do not expose raw handler details')
    monkeypatch.setattr(workspace_tools, 'execute', uncommitted)
    result = client.post('/tools/execute', headers=tools, json=save_body())
    assert result.status_code == 422 and 'raw handler' not in result.text
    with SessionLocal() as db:
        run = db.get(ChatRun, rid); run.status = 'failed'; db.commit()
        assert recovery.describe(db, run)['can_retry']
        assert db.scalar(select(ToolExecution).where(ToolExecution.run_id == rid)).state == 'failed'
        assert not db.scalar(select(Draft.id).where(Draft.user_id == name))


def test_concurrent_duplicate_cannot_execute_handler_twice(client, person, monkeypatch):
    from commerce import workspace_tools
    name, _ = person
    rid, tools = tools_for(name)
    entered, release = Event(), Event()
    original, calls = workspace_tools.execute, []
    def slow(*args, **kwargs):
        calls.append(1); entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(workspace_tools, 'execute', slow)
    body = save_body()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.post, '/tools/execute', headers=tools, json=body)
        assert entered.wait(10)
        try:
            second = client.post('/tools/execute', headers=tools, json=body)
            assert second.status_code == 409
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
    assert len(calls) == 1


def test_stale_run_recovery_preserves_partial_answer_expires_tools_and_is_idempotent(client, person):
    name, _ = person
    rid, tools = tools_for(name)
    old = now() - timedelta(seconds=settings.chat_recovery_seconds + 10)
    with SessionLocal() as db:
        run = db.get(ChatRun, rid)
        run.answer, run.created_at, run.updated_at = '服务中断前已保存的正文', old, old
        db.commit()
        assert recovery.recover_stale(db, name) == 1
        db.commit()
        assert recovery.recover_stale(db, name) == 0
        assert db.get(ChatRun, rid).answer == '服务中断前已保存的正文'
        assert db.get(ChatRun, rid).status == 'interrupted'
    assert client.post('/tools/execute', headers=tools, json=save_body()).status_code == 401


def test_recent_run_is_not_reclaimed_and_running_request_is_replayed(client, person):
    name, headers = person
    rid, _ = tools_for(name)
    request_id = uuid4().hex
    with SessionLocal() as db:
        run = db.get(ChatRun, rid); run.client_request_id = request_id; db.commit()
        assert recovery.recover_stale(db, name) == 0
    result = frames(client.post('/api/chat', headers=headers, json={
        'query': '保存报告', 'workspace': 'drafts', 'client_request_id': request_id}))
    assert result['done']['status'] == 'running' and result['start']['replayed']


def test_retry_is_scoped_and_content_bound(client, person, auth, monkeypatch):
    _, headers = person
    scripted(monkeypatch, [FAILED])
    body = {'query': '分析经营', 'workspace': 'today'}
    rid = frames(client.post('/api/chat', headers=headers, json=body))['start']['run_id']
    for foreign in [auth(), {**headers, 'X-Shop-ID': 'b'}]:
        assert client.post('/api/chat', headers=foreign, json={**body, 'retry_of': rid}).status_code == 404
    assert client.post('/api/chat', headers=headers, json={**body, 'retry_of': rid, 'workspace': 'drafts'}).status_code == 404
    assert client.post('/api/chat', headers=headers, json={**body, 'retry_of': rid, 'query': '不同指令'}).status_code == 409


def test_checkpoints_survive_before_the_stream_finishes(client, person, monkeypatch):
    from commerce import main
    from commerce.schemas import Chat
    name, _ = person
    scripted(monkeypatch, [OK])
    with SessionLocal() as db:
        response = main.chat(Chat(query='保存流式检查点', workspace='today'),
            user=scoped_actor(db, db.get(User, name), 'a'), db=db)
        async def interrupt():
            iterator = response.body_iterator
            run_id = None
            async for value in iterator:
                if value.startswith('event: start'):
                    run_id = json.loads(value.split('data: ', 1)[1])['run_id']
                if value.startswith('event: delta'):
                    # Resuming once persists the checkpoint before yielding the next frame.
                    await anext(iterator)
                    with SessionLocal() as store:
                        assert store.get(ChatRun, run_id).answer == '已返回完整答复。'
                    await iterator.aclose()
                    return
        asyncio.run(interrupt())


def test_additive_migration_can_run_twice(client):
    initialize_schema(); initialize_schema()


def test_existing_run_cleanup_can_delete_new_journal_without_deleting_business_data(client, person):
    name, _ = person
    rid, tools = tools_for(name)
    result = client.post('/tools/execute', headers=tools, json=save_body())
    assert result.status_code == 200
    draft_id = result.json()['data']['id']
    with SessionLocal() as db:
        db.delete(db.get(ChatRun, rid)); db.commit()
        assert not db.scalar(select(ToolExecution.id).where(ToolExecution.run_id == rid))
        assert db.get(Draft, draft_id) is not None


def test_original_retry_cannot_repeat_a_write_committed_in_its_child(client, person, monkeypatch):
    _, headers = person
    saved = save_body()
    def write_on_retry(request, index):
        if index == 1:
            response = client.post('/tools/execute', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
                'X-Context-Ref': request['inputs']['context_ref']}, json=saved)
            assert response.status_code == 200, response.text
    requests = scripted(monkeypatch, [FAILED, FAILED], before=write_on_retry)
    body = {'query': '保存报告', 'workspace': 'drafts'}
    original = frames(client.post('/api/chat', headers=headers, json=body))['start']['run_id']
    child = frames(client.post('/api/chat', headers=headers, json={**body, 'retry_of': original}))
    again = frames(client.post('/api/chat', headers=headers, json={**body, 'retry_of': original}))
    assert len(requests) == 2 and again['start']['replayed']
    assert again['start']['run_id'] == child['start']['run_id']
    assert again['recovery']['business_status'] == 'completed' and not again['recovery']['can_retry']


@pytest.mark.parametrize('message,kind', [('模型服务认证失败', 'authorization'), ('工具参数不符合接口契约', 'validation'),
    ('模型服务繁忙', 'rate_limit'), ('模型上下文超限', 'context_limit'), ('连接超时', 'timeout'),
    ('连接中断', 'interrupted'), ('服务暂时不可达', 'unavailable')])
def test_recovery_error_categories(message, kind):
    assert recovery.error_kind(ChatRun(error=message)) == kind


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_browser_reconciles_committed_write_after_failed_reply(client, person, monkeypatch, width, height):
    """Built Vue UI, real business handlers, stubbed model, isolated test database."""
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from playwright.sync_api import expect, sync_playwright
    from commerce.config import ROOT
    name, headers = person
    body = save_body()
    def write(request, index):
        response = client.post('/tools/execute', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
            'X-Context-Ref': request['inputs']['context_ref']}, json=body)
        assert response.status_code == 200, response.text
    requests = scripted(monkeypatch, [FAILED], before=write)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'frontend/dist')))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    errors, posted = [], []
    def forward(route):
        request = route.request
        if request.method == 'POST' and request.url.endswith('/api/chat'):
            posted.append(request.post_data_json)
        response = client.request(request.method, '/api/' + request.url.split('/api/', 1)[1],
            headers={k: v for k, v in request.headers.items() if k.lower() not in {'host', 'content-length'}},
            content=request.post_data_buffer)
        route.fulfill(status=response.status_code, body=response.content,
            content_type=response.headers.get('content-type', 'application/json'))
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': width, 'height': height})
            context.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(body=''))
            context.route('**/api/**', forward)
            context.add_init_script('''sessionStorage.setItem('commerce-token', %s);
                sessionStorage.setItem('commerce-shop','a');sessionStorage.setItem('commerce-workspace','drafts');'''
                % json.dumps(headers['Authorization'].removeprefix('Bearer ')))
            page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
            if width < 980:
                page.get_by_label('切换智能助手', exact=True).click()
            composer = page.get_by_label('给助手发送消息', exact=True)
            expect(composer).to_be_enabled()
            composer.fill('保存经营报告')
            page.get_by_label('发送消息', exact=True).click()
            failed = page.locator('.chat-message.assistant.error')
            expect(failed).to_have_count(1)
            expect(failed.locator('.message-recovery')).to_contain_text('已有操作完成')
            expect(failed.locator('.message-effects')).to_contain_text('已保存内容草稿')
            failed.get_by_role('button', name='核对执行结果', exact=True).click()
            expect(page.locator('.chat-message.assistant.error')).to_have_count(2)
            expect(page.locator('.chat-message.assistant.error').last.locator('.message-effects')).to_contain_text('已保存内容草稿')
            assert len(requests) == 1 and len(posted) == 2
            assert posted[0]['client_request_id'] and posted[1]['retry_of']
            page.reload(wait_until='networkidle')
            if width < 980:
                page.get_by_label('切换智能助手', exact=True).click()
            expect(page.locator('.chat-message.assistant.error')).to_have_count(1)
            expect(page.locator('.message-recovery')).to_contain_text('已有操作完成')
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            screenshots = ROOT / 'tests/artifacts/screenshots'
            screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshots / f'recovery-{width}.png'), full_page=True)
            assert not errors, errors
            context.close(); browser.close()
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(Draft).where(Draft.user_id == name)) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
