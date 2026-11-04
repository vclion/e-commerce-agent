"""Identity is independent of the selected shop; private resources stay scoped."""
from uuid import uuid4
from datetime import timedelta

from sqlalchemy import select

from commerce.auth import issue_token, password_hash, scoped_actor
from commerce.config import settings
from commerce.db import SessionLocal, initialize_schema
from commerce.models import AfterSale, ChatRun, Conversation, Draft, Membership, Order, User, now


def scoped(headers, shop):
    return {**headers, 'X-Shop-ID': shop}


def test_role_login_defaults_to_all_shops(client):
    for role in ['operator']:
        response = client.post('/api/auth/login', json={'username': role, 'password': settings.demo_password})
        assert response.status_code == 200
        user = response.json()['user']
        assert user['shop_id'] is None
        assert {shop['id'] for shop in user['shops']} == {'a', 'b'}
        headers = {'Authorization': 'Bearer ' + response.json()['access_token']}
        products = client.post('/api/products/search', headers=headers,
                               json={'group_styles': True, 'limit': 100}).json()['items']
        assert {p['shop_id'] for p in products} == {'a', 'b'}
        assert all(p['shop_name'] and p['available_sizes'] for p in products)
        for shop in ['a', 'b']:
            rows = client.post('/api/products/search', headers=scoped(headers, shop), json={}).json()['items']
            assert rows and all(p['shop_id'] == shop for p in rows)
        assert client.get('/api/auth/me', headers=headers).json()['shop_id'] is None


def test_operator_totals_and_shop_breakdown_reconcile(client, auth):
    headers = auth('operator-a')
    total = client.get('/api/analytics', headers=headers).json()
    shops = [client.get('/api/analytics', headers=scoped(headers, s)).json() for s in ['a', 'b']]
    for field in ['paid_fen', 'refund_fen', 'order_count', 'net_receipts_fen', 'previous_paid_fen']:
        assert total[field] == sum(shop[field] for shop in shops)
    for field in ['paid_fen', 'refund_fen', 'order_count']:
        assert total[field] == sum(row[field] for row in total['by_shop'])
    for shop in ['a', 'b']:
        rows = client.get('/api/competitors', headers=scoped(headers, shop)).json()['items']
        assert rows and all(row['shop_id'] == shop for row in rows)
        rankings = client.get('/api/selection', headers=scoped(headers, shop)).json()
        assert all(row['product']['shop_id'] == shop for row in rankings['items'] + rankings['incomplete'])


def test_memberships_are_enforced_and_not_recreated_at_startup(client, auth):
    account = 'restricted-' + uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(User(id=account, username=account, password_hash=password_hash(settings.demo_password),
                    role='operator', shop_id='a', display_name='受限运营'))
        db.flush()
        db.add(Membership(user_id=account, shop_id='b'))
        db.commit()
    initialize_schema()
    headers = auth(account)
    assert {s['id'] for s in client.get('/api/auth/me', headers=headers).json()['shops']} == {'b'}
    rows = client.post('/api/products/search', headers=headers, json={}).json()['items']
    assert rows and all(p['shop_id'] == 'b' for p in rows)
    for path in ['/api/auth/me', '/api/analytics', '/api/selection', '/api/competitors', '/api/drafts']:
        assert client.get(path, headers=scoped(headers, 'a')).status_code == 403
    assert client.get('/api/analytics', headers=scoped(headers, 'unknown')).status_code == 403
    assert client.post('/api/products/compare', headers=headers,
                       json={'product_ids': ['MEN-001-01-M']}).status_code == 404
    with SessionLocal() as db:
        token = issue_token(db, scoped_actor(db, db.get(User, account), 'b'), 'tool', uuid4().hex)
        db.delete(db.get(Membership, (account, 'b')))
        db.commit()
    tool_headers = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token}
    assert client.post('/tools/read', headers=tool_headers,
                       json={'operation': 'products', 'arguments': {}}).status_code == 403
    initialize_schema()
    assert client.get('/api/auth/me', headers=headers).json()['shops'] == []


def test_tool_context_is_frozen_and_authorization_rechecked(client, auth):
    with SessionLocal() as db:
        user = db.get(User, 'operator-a')
        token = issue_token(db, scoped_actor(db, user, 'b'), 'tool', uuid4().hex)
    headers = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': token, 'X-Shop-ID': 'a'}
    response = client.post('/tools/read', headers=headers, json={'operation': 'products', 'arguments': {}})
    assert response.status_code == 200
    assert all(p['shop_id'] == 'b' for p in response.json()['data']['items'])
    assert client.get('/api/auth/me', headers=auth('operator-a')).json()['shop_id'] is None


def test_shop_history_cannot_resume_in_different_scope(client, auth, monkeypatch):
    from commerce import main
    cid = 'scope-' + uuid4().hex
    with SessionLocal() as db:
        token = issue_token(db, scoped_actor(db, db.get(User, 'operator-a'), 'a'), 'tool', uuid4().hex)
        db.add(Conversation(id=cid, user_id='operator-a', context_ref=token))
        db.add(ChatRun(user_id='operator-a', shop_id='a', conversation_id=cid,
                       query='店铺A的历史', answer='店铺A的数据', status='succeeded'))
        db.commit()
    headers = auth('operator-a')
    assert client.get('/api/chat/runs', headers=scoped(headers, 'a'), params={'conversation_id': cid}).json()['items']
    assert not client.get('/api/chat/runs', headers=scoped(headers, 'b'), params={'conversation_id': cid}).json()['items']
    monkeypatch.setattr(main.settings, 'dify_operator_key', 'scope-test')
    response = client.post('/api/chat', headers=scoped(headers, 'b'), json={'query': '继续', 'conversation_id': cid})
    assert response.status_code == 409


def test_aggregate_reports_are_private_and_single_shop_reports_stay_scoped(client, auth):
    headers = auth('operator-a')
    body = {'kind': 'report', 'title': '多店汇总报告', 'content': '这是多店经营汇总报告内容。'}
    report = client.post('/api/content', headers=headers, json=body).json()
    assert report['shop_id'] is None
    assert report['id'] in {d['id'] for d in client.get('/api/drafts', headers=headers).json()['items']}
    assert report['id'] not in {d['id'] for d in client.get('/api/drafts', headers=auth('operator-b')).json()['items']}
    assert report['id'] not in {d['id'] for d in client.get('/api/drafts', headers=scoped(headers, 'a')).json()['items']}
    report = client.post('/api/content', headers=scoped(headers, 'b'), json=body).json()
    assert report['shop_id'] == 'b'
    assert report['id'] not in {d['id'] for d in client.get('/api/drafts', headers=scoped(headers, 'a')).json()['items']}


def test_aggregate_competitor_import_uses_product_shop(client, auth):
    headers = auth('operator')
    title = '竞品范围-' + uuid4().hex[:12]
    response = client.post('/api/competitors/import', headers=headers, json=[{
        'product_id': 'WOM-001-01-M', 'title': title, 'price_fen': 24900,
        'specs': {'尺码': 'M'}, 'source': 'https://example.com/snapshot',
        'captured_at': (now()-timedelta(minutes=1)).isoformat(), 'currency': 'CNY'}])
    assert response.status_code == 200, response.text
    women = client.get('/api/competitors?product_id=WOM-001-01-M', headers=scoped(headers, 'b')).json()['items']
    assert any(c['title'] == title and c['shop_id'] == 'b' for c in women)
    assert client.get('/api/competitors?product_id=WOM-001-01-M', headers=scoped(headers, 'a')).status_code == 404
