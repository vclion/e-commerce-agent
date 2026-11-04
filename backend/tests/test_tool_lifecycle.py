"""Stream cancellation, a handler's later transaction, and late audit receipts."""
import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from commerce.auth import issue_token, scoped_actor
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import ChatRun, Draft, User


def running_tool(workspace='drafts'):
    run_id = uuid4().hex
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, 'operator-a'), 'a')
        db.add(ChatRun(id=run_id, user_id=actor.id, shop_id='a', workspace=workspace,
                       query='保存报告草稿', status='running'))
        token = issue_token(db, actor, 'tool', run_id, workspace=workspace)
    return run_id, {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token}


def save_body():
    return {'operation': 'content_save', 'arguments': {
        'kind': 'report', 'title': 'lifecycle-' + uuid4().hex, 'content': '合成生命周期回归内容。'}}


def test_disconnect_after_first_frame_finalizes_run_and_denies_late_write(client, monkeypatch):
    from commerce import main
    from commerce.schemas import Chat
    captured = []
    original = main.issue_token
    def capture(*args, **kwargs):
        token = original(*args, **kwargs)
        captured.append(token)
        return token
    monkeypatch.setattr(main, 'issue_token', capture)
    monkeypatch.setattr(settings, 'dify_operator_key', 'lifecycle-placeholder')
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, 'operator-a'), 'a')
        response = main.chat(Chat(query='保存报告草稿', workspace='drafts'), user=actor, db=db)
        async def disconnect():
            iterator = response.body_iterator
            first = await anext(iterator)
            with pytest.raises(asyncio.CancelledError):
                await iterator.athrow(asyncio.CancelledError())
            return json.loads(first.split('data: ', 1)[1])['run_id']
        run_id = asyncio.run(disconnect())
    with SessionLocal() as db:
        assert db.get(ChatRun, run_id).status == 'interrupted'
    tools = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': captured[-1]}
    assert client.post('/tools/execute', headers=tools, json=save_body()).status_code == 409


@pytest.mark.parametrize('state', ['failed', 'succeeded', 'interrupted'])
def test_terminal_chat_run_cannot_start_another_write(client, state):
    run_id, tools = running_tool()
    with SessionLocal() as db:
        db.get(ChatRun, run_id).status = state
        db.commit()
    assert client.post('/tools/execute', headers=tools, json=save_body()).status_code == 409


def test_handler_cannot_commit_after_run_closes_between_transactions(client, monkeypatch):
    from commerce import main, workspace_tools
    run_id, tools = running_tool()
    body = save_body()
    original = workspace_tools.execute
    def close_then_write(db, user, operation, arguments, workspace):
        db.commit()  # the same gap as a handler waiting for a nested workflow
        with SessionLocal() as closer:
            closer.get(ChatRun, run_id).status = 'interrupted'
            closer.commit()
        return original(db, user, operation, arguments, workspace)
    monkeypatch.setattr(workspace_tools, 'execute', close_then_write)
    assert client.post('/tools/execute', headers=tools, json=body).status_code == 409
    with SessionLocal() as db:
        assert not db.scalar(select(Draft.id).where(Draft.payload['title'].as_string() == body['arguments']['title']))


def test_committed_action_keeps_receipt_if_stream_closes_before_tool_audit(client, monkeypatch):
    from commerce import workspace_tools
    run_id, tools = running_tool()
    original = workspace_tools.execute
    def write_then_close(db, user, operation, arguments, workspace):
        result = original(db, user, operation, arguments, workspace)
        with SessionLocal() as closer:
            row = closer.get(ChatRun, run_id)
            row.status, row.effects = 'interrupted', []
            closer.commit()
        return result
    monkeypatch.setattr(workspace_tools, 'execute', write_then_close)
    response = client.post('/tools/execute', headers=tools, json=save_body())
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        run = db.get(ChatRun, run_id)
        assert run.status == 'interrupted'
        assert run.effects == [{'operation': 'content_save', 'label': '已保存内容草稿',
                                'resource_id': response.json()['data']['id']}]
