from datetime import timedelta
from uuid import uuid4
import json
from sqlalchemy import func, select
from commerce.auth import issue_token
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import AccessToken, Order, User, now


def test_auth_and_price_filters(client, auth):
    assert client.post('/api/products/search', json={}).status_code == 401
    headers = auth()
    response = client.post('/api/products/search', headers=headers, json={
        'category': '衬衫', 'max_price_fen': 30000, 'size': 'L', 'material': '棉'})
    assert response.status_code == 200
    assert response.json()['items']
    for p in response.json()['items']:
        assert p['category'] == '衬衫' and p['price_fen'] <= 30000 and p['specs']['尺码'] == 'L' and '棉' in p['specs']['面料']
        assert p['stock'] > 0 and 'cost_fen' in p
    assert client.post('/api/products/search', headers=headers,
                       json={'min_price_fen': 20000, 'max_price_fen': 1}).status_code == 422


def test_metrics_and_selection_are_reproducible(client, auth):
    headers = auth('operator-a')
    response = client.get('/api/analytics?days=7', headers=headers)
    assert response.status_code == 200, response.text
    metrics = response.json()
    assert metrics['paid_fen'] == sum(x['paid_fen'] for x in metrics['trend'])
    assert metrics['order_count'] == sum(x['orders'] for x in metrics['channels'])
    assert metrics['net_receipts_fen'] == metrics['paid_fen']-metrics['refund_fen']
    rows = client.get('/api/selection?category=衬衫&min_margin=0.2', headers=headers).json()
    weights = dict(demand=.30, margin=.25, competition=.20, supply=.15, quality=.10)
    assert rows['items']
    for row in rows['items']:
        assert row['margin_percent'] >= 20
        assert abs(row['score']-sum(row['parts'][k]*v for k,v in weights.items())) < .02
    assert [x['score'] for x in rows['items']] == sorted([x['score'] for x in rows['items']], reverse=True)


def test_missing_dify_key_is_explicit(client, auth, monkeypatch):
    monkeypatch.setattr(settings, 'dify_operator_key', '')
    response = client.post('/api/chat', headers=auth(), json={'query': '推荐一款衬衫'})
    assert response.status_code == 503
    assert 'Dify' in response.json()['detail']


def test_readonly_tool_and_server_call_budget(client):
    from commerce.models import uid
    with SessionLocal() as db:
        token = issue_token(db, db.get(User, 'operator-a'), 'tool', uid())
    headers = {'Authorization': f'Bearer {settings.tool_service_key}', 'X-Context-Ref': token}
    assert client.post('/tools/read', headers=headers, json={'operation': 'after_sales_prepare',
        'arguments': {'order_id': 'FIT-A', 'quantity': 1, 'reason': '问题'}}).status_code == 422
    for _ in range(8):
        result = client.post('/tools/read', headers=headers,
                             json={'operation': 'products', 'arguments': '{"limit":1}'})
        assert result.status_code == 200, result.text
    assert client.post('/tools/read', headers=headers, json={'operation': 'products', 'arguments': {}}).status_code == 429


def test_expired_tool_context_is_rejected(client):
    with SessionLocal() as db:
        user = db.get(User, 'operator-a')
        token = issue_token(db, user, 'tool', 'expired-test')
        from commerce.auth import digest
        db.get(AccessToken, digest(token)).expires_at = now()-timedelta(seconds=1)
        db.commit()
    headers = {'Authorization': f'Bearer {settings.tool_service_key}', 'X-Context-Ref': token}
    assert client.post('/tools/read', headers=headers, json={'operation': 'products', 'arguments': {}}).status_code == 401


def test_conversation_ownership_checked_before_model_call(client, auth, monkeypatch):
    from commerce.models import Conversation
    monkeypatch.setattr(settings, 'dify_operator_key', 'test-key-not-a-real-secret')
    with SessionLocal() as db:
        db.add(Conversation(id='test-other-conversation', user_id='operator-b', context_ref='unused'))
        db.commit()
    result = client.post('/api/chat', headers=auth(),
                         json={'query': '查询订单', 'conversation_id': 'test-other-conversation'})
    assert result.status_code == 404


def test_model_metrics_use_exact_formatted_yuan(client, auth):
    from commerce.models import uid
    from decimal import Decimal
    with SessionLocal() as db:
        token = issue_token(db, db.get(User, 'operator-a'), 'tool', uid())
    headers = {'Authorization': f'Bearer {settings.tool_service_key}', 'X-Context-Ref': token}
    data = client.post('/tools/read', headers=headers,
                       json={'operation': 'analytics', 'arguments': {'days': 7}}).json()['data']
    raw = client.get('/api/analytics?days=7', headers={**auth('operator-a'), 'X-Shop-ID': 'a'}).json()
    assert data['期间退款额'] == f'{Decimal(raw["refund_fen"]) / Decimal(100):,.2f} 元'
    assert data['支付成交额'] == f'{Decimal(raw["paid_fen"]) / Decimal(100):,.2f} 元'
    assert not any(key.endswith('_fen') for key in data)


def test_apparel_style_grouping_and_hard_filters(client, auth):
    headers = auth()
    body = {'group_styles': True, 'limit': 100}
    data = client.post('/api/products/search', headers=headers, json=body).json()
    # Other tests may publish an extra style; the seeded collection itself has forty.
    assert len({p['group_id'] for p in data['items'] if p['group_id'].startswith('MEN-')}) == 40
    body.update(size='L', material='棉', fit='合体', max_price_fen=30000)
    filtered = client.post('/api/products/search', headers=headers, json=body).json()
    assert filtered['items']
    for p in filtered['items']:
        assert p['price_fen'] <= 30000
        for v in p['variants']:
            assert v['specs']['尺码'] == 'L' and '棉' in v['specs']['面料']
            assert v['specs']['版型'] == '合体' and v['stock'] > 0
            assert 'cost_fen' in v
        assert p['style_stock'] == sum(v['stock'] for v in p['variants'])
    sold_out = client.post('/api/products/search', headers=headers,
                           json={'query': '远山牛津纺', 'size': 'L', 'color': '云白'}).json()
    assert sold_out['total'] == 0


def test_gender_stores_and_names_are_distinct(client, auth):
    account = auth()
    assert client.get('/api/auth/me', headers=account).json()['shop']['name'] == '全部授权店铺'
    men, women = {**account, 'X-Shop-ID': 'a'}, {**account, 'X-Shop-ID': 'b'}
    assert client.get('/api/auth/me', headers=men).json()['shop']['name'] == '山序男装'
    assert client.get('/api/auth/me', headers=women).json()['shop']['name'] == '知织女装'
    assert not client.post('/api/products/search', headers=men, json={'category': '连衣裙'}).json()['items']
    dresses = client.post('/api/products/search', headers=women,
                          json={'category': '连衣裙', 'size': 'M', 'max_price_fen': 40000, 'occasion': '通勤'}).json()['items']
    assert dresses and all(p['specs']['性别'] == '女装' and p['specs']['尺码'] == 'M' for p in dresses)


def test_refund_and_size_breakdowns_reconcile_and_exclude_legacy(client, auth):
    headers = auth('operator-b')
    before = client.get('/api/analytics?days=7', headers=headers).json()
    assert before['refund_reasons']
    assert sum(x['refund_fen'] for x in before['refund_reasons']) == before['refund_fen']
    with SessionLocal() as db:
        oid = 'LEGACY-' + uuid4().hex[:20]
        db.add(Order(id=oid, shop_id='b', user_id='operator-b', product_id='WOM-001-01-M',
                     title='旧演示订单', dataset_version='legacy-v1', quantity=1, paid_fen=99999999,
                     cost_fen=100, status='refunded', paid_at=now()-timedelta(days=2),
                     refund_at=now()-timedelta(days=1), refund_fen=99999999, channel='旧数据', version=1))
        db.commit()
    after = client.get('/api/analytics?days=7', headers=headers).json()
    assert after['paid_fen'] == before['paid_fen'] and after['refund_fen'] == before['refund_fen']
    own_orders = client.get('/api/orders', headers=auth('operator-b')).json()['items']
    assert oid not in {o['id'] for o in own_orders}
    assert all(o['specs_snapshot'] for o in own_orders)


def test_apparel_seed_is_idempotent_and_preserves_credentials(client):
    from commerce.seed import seed
    from commerce.models import Product, Manifest
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(Product))
        encoded = db.get(User, 'operator-a').password_hash
        anchor = db.get(Manifest, 'apparel-v1').data['anchor']
    seed()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Product)) == count
        assert db.get(User, 'operator-a').password_hash == encoded
        assert db.get(Manifest, 'apparel-v1').data['anchor'] == anchor


def test_chat_history_groups_sessions_and_scopes_messages(client, auth):
    from commerce.models import ChatRun, Conversation
    cid, other = 'history-' + uuid4().hex, 'history-' + uuid4().hex
    ids = []
    with SessionLocal() as db:
        db.add_all([Conversation(id=cid, user_id='operator-a', context_ref='history-test'),
                    Conversation(id=other, user_id='operator-b', context_ref='other-history-test')])
        for i in range(25):
            r = ChatRun(user_id='operator-a', conversation_id=cid, query=f'历史问题{i}', answer=f'历史回复{i}',
                        sources=[{'document_id': 'demo-doc', 'title': '尺码资料'}], status='succeeded',
                        created_at=now()-timedelta(minutes=30-i))
            db.add(r)
        db.add(ChatRun(user_id='operator-b', conversation_id=other, query='另一店的私有问题', answer='私有回复', status='succeeded'))
        db.commit()
    headers = auth()
    sessions = client.get('/api/chat/conversations', headers=headers).json()['items']
    assert sum(s['conversation_id'] == cid for s in sessions) == 1
    assert all(s['conversation_id'] != other for s in sessions)
    first = client.get('/api/chat/runs', headers=headers, params={'conversation_id': cid}).json()
    second = client.get('/api/chat/runs', headers=headers, params={'conversation_id': cid, 'offset': 20}).json()
    assert len(first['items']) == 20 and first['has_more']
    assert len(second['items']) == 5 and not second['has_more']
    assert len({r['id'] for r in first['items'] + second['items']}) == 25
    assert first['items'][0]['query'] == '历史问题24'
    assert first['items'][0]['sources'][0]['title'] == '尺码资料'
    assert client.get('/api/chat/runs', headers=auth('operator-b'), params={'conversation_id': cid}).status_code == 404


def test_legacy_chat_records_remain_visible_and_private(client, auth):
    from commerce.models import ChatRun
    with SessionLocal() as db:
        row = ChatRun(user_id='operator-a', query='没有会话关联的旧问题', answer='保留旧正文', status='succeeded')
        db.add(row); db.commit(); rid = row.id
    data = client.get('/api/chat/runs', headers=auth(), params={'run_id': rid}).json()
    assert data['items'][0]['answer'] == '保留旧正文' and data['items'][0]['conversation_id'] is None
    assert not client.get('/api/chat/runs', headers=auth('operator-b'), params={'run_id': rid}).json()['items']


def test_stream_persists_session_sources_and_followup_context(client, auth, monkeypatch):
    from commerce import main
    cid = 'stream-history-' + uuid4().hex
    requests = []
    class FakeResponse:
        status_code = 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def aiter_lines(self):
            for event in [
                {'event': 'message', 'conversation_id': cid, 'answer': '这是已保存的服装回复。'},
                {'event': 'message_end', 'conversation_id': cid, 'metadata': {'retriever_resources': [
                    {'document_id': 'size-doc', 'document_name': '成衣尺寸说明', 'segment_id': 's1'}]}}]:
                yield 'data: ' + json.dumps(event)
    class FakeModelClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            requests.append(kwargs['json'])
            return FakeResponse()
    monkeypatch.setattr(main.httpx, 'AsyncClient', FakeModelClient)
    monkeypatch.setattr(settings, 'dify_operator_key', 'test-local-provider')
    headers = auth()
    for query, conversation in [('先推荐衬衫', ''), ('继续介绍它的面料', cid)]:
        response = client.post('/api/chat', headers=headers, json={'query': query, 'conversation_id': conversation})
        assert response.status_code == 200 and '"status": "succeeded"' in response.text
    assert requests[1]['conversation_id'] == cid
    assert requests[0]['inputs']['context_ref'] != requests[1]['inputs']['context_ref']
    history = client.get('/api/chat/runs', headers=headers, params={'conversation_id': cid}).json()['items']
    assert len(history) == 2
    assert all(r['answer'] == '这是已保存的服装回复。' and r['sources'][0]['document_id'] == 'size-doc' for r in history)
