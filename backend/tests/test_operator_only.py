"""The removed workbenches must be inaccessible through UI, sessions and tools."""
from uuid import uuid4
import json
import pytest
from sqlalchemy import select

from commerce.auth import issue_token, password_hash, scoped_actor
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import Draft, Membership, User


@pytest.mark.parametrize('role', ['customer', 'merchant', 'buyer'])
def test_only_operators_can_login_or_reuse_old_tokens(client, role):
    account = 'retired-' + uuid4().hex[:14]
    with SessionLocal() as db:
        actor = User(id=account, username=account, role=role, shop_id='a', display_name='历史身份',
                     password_hash=password_hash(settings.demo_password))
        db.add(actor)
        db.flush()
        db.add(Membership(user_id=account, shop_id='a'))
        session = issue_token(db, actor)
        tool = issue_token(db, actor, 'tool', uuid4().hex)
    assert client.post('/api/auth/login', json={'username': account, 'password': settings.demo_password}).status_code == 403
    headers = {'Authorization': 'Bearer ' + session}
    for endpoint in ['/api/auth/me', '/api/orders', '/api/analytics', '/api/drafts', '/api/chat/runs']:
        assert client.get(endpoint, headers=headers).status_code == 403
    assert client.post('/api/content', headers=headers,
                       json={'kind': 'report', 'title': '测试', 'content': '不应被保存的内容'}).status_code == 403
    assert client.post('/tools/read', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
                                               'X-Context-Ref': tool},
                       json={'operation': 'products', 'arguments': {}}).status_code == 403


def test_removed_commands_are_not_exposed(client, auth):
    headers = auth()
    paths = client.get('/openapi.json').json()['paths']
    for endpoint in ['/api/after-sales/prepare', '/api/actions', '/api/actions/test/confirm',
                     '/api/listings/drafts', '/api/listings/drafts/test/prepare', '/api/policy']:
        assert endpoint not in paths
        assert client.post(endpoint, headers=headers, json={}).status_code == 404
    with SessionLocal() as db:
        token = issue_token(db, scoped_actor(db, db.get(User, 'operator-a')), 'tool', uuid4().hex)
    for operation in ['after_sales_prepare', 'policy', 'listing']:
        assert client.post('/tools/read', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
                                                   'X-Context-Ref': token},
                           json={'operation': operation, 'arguments': {}}).status_code == 422


def test_marketing_and_reports_are_available_to_operator_only(client, auth):
    headers = auth()
    for kind in ['marketing', 'report']:
        response = client.post('/api/content', headers=headers,
                               json={'kind': kind, 'title': '运营草稿', 'content': '运营可保存的事实文案与分析内容。'})
        assert response.status_code == 200
        saved = response.json()
        assert saved['id'] in {d['id'] for d in client.get('/api/drafts', headers=headers).json()['items']}
        assert saved['id'] not in {d['id'] for d in client.get('/api/drafts', headers=auth('operator-b')).json()['items']}
    assert client.post('/api/content', headers=headers,
                       json={'kind': 'handoff', 'title': '旧类型', 'content': '旧消费者工单不再支持。'}).status_code == 422
    with SessionLocal() as db:
        legacy = Draft(user_id='operator-a', shop_id='a', kind='listing', payload={'title': '历史上架资料'})
        db.add(legacy)
        db.commit()
        old_id = legacy.id
    assert old_id not in {d['id'] for d in client.get('/api/drafts', headers=headers).json()['items']}


def test_operator_order_views_follow_shop_authorization(client, auth):
    headers = auth()
    for shop in ['a', 'b']:
        rows = client.get('/api/orders', headers={**headers, 'X-Shop-ID': shop}).json()['items']
        assert rows and all(r['shop_id'] == shop for r in rows)
        assert all('user_id' not in r for r in rows)
    assert set(client.get('/api/health').json()['dify']) == {'operator'}


def test_dify_definitions_expose_one_operator_chatflow():
    assert {p.stem for p in (ROOT / 'dify/chatflows').glob('*.yml')} == {'operator'}
    assert {p.stem for p in (ROOT / 'dify/workflows').glob('*.yml')} == {
        'product-match', 'marketing-draft', 'selection-research', 'business-analysis', 'video-script', 'peer-analysis', 'memory-maintenance'}
    contract = json.loads((ROOT / 'contracts/dify-read-tools.json').read_text())
    operations = contract['paths']['/tools/read']['post']['requestBody']['content']['application/json']['schema']['properties']['operation']['enum']
    assert 'after_sales_prepare' not in operations and 'policy' not in operations
