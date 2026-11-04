"""Preserve actual terminal outcomes and safe errors from Dify SSE responses."""
import json
from uuid import uuid4

import pytest

from commerce.config import settings


@pytest.mark.parametrize('events,expected,answer', [
    ([{'event': 'workflow_finished', 'data': {'status': 'failed', 'error': 'PluginInvokeError: ConnectTimeoutError: api.deepseek.com timed out'}}], '模型服务连接超时', ''),
    ([{'event': 'error', 'message': '401 invalid API key SECRET-MUST-NOT-LEAK'}], '模型服务认证失败', ''),
    ([{'event': 'message', 'answer': '已经收到的部分内容'},
      {'event': 'workflow_finished', 'data': {'status': 'failed', 'error': 'rate limit 429'}}], '模型服务繁忙', '已经收到的部分内容'),
    ([{'event': 'message', 'answer': '这是片段'}, {'event': 'message_end'},
      {'event': 'workflow_finished', 'data': {'status': 'failed', 'error': 'Plugin stack trace'}}], '运营助手执行失败', '这是片段'),
    ([{'event': 'message', 'answer': '不完整的正文'},
      {'event': 'workflow_finished', 'data': {'status': 'succeeded'}}], '回复连接中断', '不完整的正文'),
    ([{'event': 'message_end'}], '模型未返回可显示的回复', ''),
])
def test_failures_are_actionable_persisted_and_never_marked_success(client, auth, monkeypatch, events, expected, answer):
    from commerce import main
    cid = 'stream-failure-' + uuid4().hex

    class Response:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            for event in events:
                yield 'data: ' + json.dumps({**event, 'conversation_id': cid})

    class ModelClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): return Response()

    monkeypatch.setattr(main.httpx, 'AsyncClient', ModelClient)
    monkeypatch.setattr(settings, 'dify_operator_key', 'test-placeholder')
    headers = auth()
    response = client.post('/api/chat', headers=headers, json={'query': '帮我生成文案', 'conversation_id': ''})
    assert response.status_code == 200
    assert expected in response.text and 'event: failure' in response.text
    assert '"status": "failed"' in response.text
    assert 'SECRET-MUST-NOT-LEAK' not in response.text and 'Plugin stack trace' not in response.text
    row = client.get('/api/chat/runs', headers=headers, params={'conversation_id': cid}).json()['items'][0]
    assert row['status'] == 'failed' and expected in row['error']
    assert row['answer'] == answer


def test_normal_workflow_finish_keeps_complete_answer(client, auth, monkeypatch):
    from commerce import main

    class Response:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            for event in [{'event': 'message', 'answer': '请告诉我商品和发布场景。'},
                          {'event': 'message_end'},
                          {'event': 'workflow_finished', 'data': {'status': 'succeeded'}}]:
                yield 'data: ' + json.dumps(event)

    class ModelClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): return Response()

    monkeypatch.setattr(main.httpx, 'AsyncClient', ModelClient)
    monkeypatch.setattr(settings, 'dify_operator_key', 'test-placeholder')
    response = client.post('/api/chat', headers=auth(), json={'query': '帮我生成文案', 'conversation_id': ''})
    assert '"status": "succeeded"' in response.text
    assert 'event: failure' not in response.text
