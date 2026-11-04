"""Official-connection contracts and offline lifecycle, using an isolated DB schema."""
import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, delete

from commerce.auth import password_hash
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import (User, Membership, PlatformConnection, PlatformShop, PlatformSync,
    OpsRecord, AccessToken, Audit, OpsTask, ChatRun, now)
from commerce.platform_client import signed_params, canonical_json, Client, PlatformError, check_response
from commerce import platform_service as service, platform_mapping as mapping
from commerce.platform_vault import decrypt


@pytest.fixture
def operator(client, auth):
    username = 'platform-' + uuid4().hex[:12]
    user_id = uuid4().hex
    with SessionLocal() as db:
        db.add(User(id=user_id, username=username, password_hash=password_hash(settings.demo_password),
                    role='operator', shop_id='a', display_name='平台测试'))
        db.flush()
        db.add_all([Membership(user_id=user_id, shop_id=sid) for sid in ['a', 'b']])
        db.commit()
    headers = auth(username)
    yield headers, user_id
    with SessionLocal() as db:
        rows = list(db.scalars(select(PlatformConnection).where(PlatformConnection.user_id == user_id)))
        ids, shops = [r.id for r in rows], [r.shop_id for r in rows]
        db.execute(delete(PlatformSync).where(PlatformSync.connection_id.in_(ids)))
        db.execute(delete(PlatformConnection).where(PlatformConnection.id.in_(ids)))
        db.execute(delete(OpsTask).where(OpsTask.shop_id.in_(shops)))
        db.execute(delete(OpsRecord).where(OpsRecord.shop_id.in_(shops)))
        db.execute(delete(Membership).where(Membership.user_id == user_id))
        db.execute(delete(PlatformShop).where(PlatformShop.id.in_(shops)))
        db.execute(delete(AccessToken).where(AccessToken.user_id == user_id))
        db.execute(delete(ChatRun).where(ChatRun.user_id == user_id))
        db.execute(delete(Audit).where(Audit.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()


def create(client, headers, **overrides):
    value = dict(name='独立接入测试', provider='doudian', mode='simulation', external_id='SIM-TEST')
    value.update(overrides)
    response = client.post('/api/platform/connections', headers=headers, json=value)
    assert response.status_code == 200, response.text
    return response.json()


def connect_and_queue(client, headers, row, resources=None):
    response = client.post(f'/api/platform/connections/{row["id"]}/connect', headers=headers)
    assert response.status_code == 200, response.text
    response = client.post(f'/api/platform/connections/{row["id"]}/sync', headers=headers,
                           json={'days': 7, **({'resources': resources} if resources else {})})
    assert response.status_code == 200, response.text
    return response.json()


def test_signature_matches_documented_algorithm_and_unicode():
    args = {'z': '棉<&', 'a': {'b': 2, 'a': 1}}
    params = signed_params('key', 'secret', 'product.listV2', args, 'private-token', '2026-10-18 09:00:00')
    assert params['param_json'] == '{"a":{"a":1,"b":2},"z":"棉\\u003c\\u0026"}'
    message = 'secretapp_keykeymethodproduct.listV2param_json' + params['param_json'] + 'timestamp2026-10-18 09:00:00v2secret'
    assert params['sign'] == hmac.new(b'secret', message.encode(), hashlib.sha256).hexdigest()
    assert 'private-token' not in message


def test_save_empty_live_configuration_and_no_network(client, operator, monkeypatch):
    headers, _ = operator
    def network_forbidden(*args, **kwargs):
        raise AssertionError('No real API calls may occur without credentials')
    monkeypatch.setattr(Client, 'request', network_forbidden)
    row = create(client, headers, mode='live', external_id='')
    assert row['status'] == 'unconfigured' and len(row['missing']) == 3
    response = client.post(f'/api/platform/connections/{row["id"]}/connect', headers=headers)
    assert response.status_code == 422
    response = client.post(f'/api/platform/connections/{row["id"]}/sync', headers=headers, json={})
    assert response.status_code == 409
    me = client.get('/api/auth/me', headers=headers).json()
    assert me['shop']['name'] == '全部正式店铺'
    overview = client.get('/api/ops/overview', headers=headers).json()
    assert overview['total']['paid_fen'] is None
    pending_orders = client.get('/api/orders', headers=headers).json()
    assert pending_orders['items'] == [] and pending_orders['data_available'] is False and pending_orders['is_demo'] is False
    assert client.post('/api/ops/demo/import', headers=headers).status_code == 409


def test_credentials_encrypted_not_echoed_and_live_never_falls_back(client, operator, monkeypatch):
    headers, _ = operator
    row = create(client, headers, mode='live', external_id='123456', app_key='app-key', app_secret='very-private-secret')
    assert 'very-private-secret' not in json.dumps(row)
    with SessionLocal() as db:
        record = db.get(PlatformConnection, row['id'])
        assert 'very-private-secret' not in record.credentials
        assert decrypt(record.credentials)['app_secret'] == 'very-private-secret'
    def denied(*args, **kwargs):
        raise PlatformError('authorization', '平台权限不足')
    monkeypatch.setattr(Client, 'request', denied)
    response = client.post(f'/api/platform/connections/{row["id"]}/connect', headers=headers)
    assert response.status_code == 502
    current = client.get('/api/platform/connections', headers=headers).json()['items'][0]
    assert current['status'] == 'authorization_required' and not current['has_token']
    with SessionLocal() as db:
        assert not list(db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row['shop_id'])))


def test_simulation_sync_business_views_and_idempotency(client, operator):
    headers, _ = operator
    row = create(client, headers)
    job = connect_and_queue(client, headers, row)
    same = client.post(f'/api/platform/connections/{row["id"]}/sync', headers=headers, json={}).json()
    assert same['id'] == job['id']
    with SessionLocal() as db:
        saved = db.get(PlatformSync, job['id'])
        saved.status, saved.attempts = 'running', 1
        db.commit()
        service.execute_job(db, saved)
    jobs = client.get(f'/api/platform/connections/{row["id"]}/syncs', headers=headers).json()['items']
    assert jobs[0]['status'] == 'succeeded', jobs
    scoped = {**headers, 'X-Shop-ID': row['shop_id']}
    products = client.post('/api/products/search', headers=scoped, json={}).json()
    assert len(products['items']) == 1 and products['items'][0]['cost_fen'] is None
    assert products['items'][0]['is_demo'] is True
    pid = products['items'][0]['id']
    compared = client.post('/api/products/compare', headers=scoped, json={'product_ids': [pid]})
    assert compared.status_code == 200
    overview = client.get('/api/ops/overview', headers=scoped).json()
    assert overview['total']['paid_fen'] == 7 * 19900
    assert overview['total']['refund_fen'] == 19900
    assert overview['total']['visitors'] is None and overview['total']['buyer_day_conversion'] is None
    assert client.get('/api/analytics', headers=scoped).json()['paid_fen'] == 7 * 19900
    assert len(client.get('/api/orders', headers=scoped).json()['items']) == 7
    generated = client.post('/api/ops/tasks/generate', headers=scoped, json={})
    assert generated.status_code == 200 and generated.json()['created'] == 8
    assert client.post('/api/ops/tasks/generate', headers=scoped, json={}).json()['created'] == 0
    selected = client.get('/api/selection', headers=scoped).json()
    assert not selected['items'] and selected['incomplete'][0]['score'] is None
    connect_and_queue(client, headers, row)
    service.tick()
    assert len(client.get('/api/orders', headers=scoped).json()['items']) == 7


def test_resource_failure_is_partial_and_retry_only_failed(client, operator, monkeypatch):
    headers, _ = operator
    row = create(client, headers)
    original = Client.refund_pages
    def denied(*args):
        raise PlatformError('authorization', '退款接口未获批')
    monkeypatch.setattr(Client, 'refund_pages', denied)
    job = connect_and_queue(client, headers, row)
    service.tick()
    jobs = client.get(f'/api/platform/connections/{row["id"]}/syncs', headers=headers).json()['items']
    assert jobs[0]['status'] == 'partial' and jobs[0]['result']['orders']['status'] == 'succeeded'
    overview = client.get('/api/ops/overview', headers={**headers, 'X-Shop-ID': row['shop_id']}).json()
    assert overview['total']['paid_fen'] == 7 * 19900
    assert overview['total']['refund_fen'] is None and not overview['complete']
    retry = client.post(f'/api/platform/syncs/{job["id"]}/retry', headers=headers).json()
    assert retry['resources'] == ['refunds']
    monkeypatch.setattr(Client, 'refund_pages', original)
    service.tick()
    overview = client.get('/api/ops/overview', headers={**headers, 'X-Shop-ID': row['shop_id']}).json()
    assert overview['total']['refund_fen'] == 19900 and overview['complete']


def test_transient_retry_and_resume_does_not_rewrite_success(client, operator, monkeypatch):
    headers, _ = operator
    row = create(client, headers)
    original = Client.refund_pages
    monkeypatch.setattr(Client, 'refund_pages', lambda *args: (_ for _ in ()).throw(PlatformError('rate_limit', '限流', retryable=True)))
    job = connect_and_queue(client, headers, row)
    service.tick()
    with SessionLocal() as db:
        saved = db.get(PlatformSync, job['id'])
        assert saved.status == 'retry_wait'
        saved.retry_at = now() - timedelta(seconds=1)
        db.commit()
    monkeypatch.setattr(Client, 'refund_pages', original)
    monkeypatch.setattr(Client, 'product_pages', lambda *args: (_ for _ in ()).throw(AssertionError('Must not repeat committed resource')))
    service.tick()
    with SessionLocal() as db:
        assert db.get(PlatformSync, job['id']).status == 'succeeded'


def test_ownership_revocation_disconnect_and_config_version(client, operator, auth):
    headers, user_id = operator
    row = create(client, headers)
    other = auth('operator-b')
    assert client.post(f'/api/platform/connections/{row["id"]}/connect', headers=other).status_code == 404
    body = dict(name='更新', provider='doudian', mode='simulation', external_id='SIM-TEST', expected_version=999)
    assert client.patch(f'/api/platform/connections/{row["id"]}', headers=headers, json=body).status_code == 409
    job = connect_and_queue(client, headers, row)
    assert client.post(f'/api/platform/connections/{row["id"]}/disconnect', headers=headers).status_code == 200
    jobs = client.get(f'/api/platform/connections/{row["id"]}/syncs', headers=headers).json()['items']
    assert jobs[0]['status'] == 'cancelled'
    with SessionLocal() as db:
        db.execute(delete(Membership).where(Membership.user_id == user_id, Membership.shop_id == row['shop_id']))
        db.commit()
    assert client.get(f'/api/platform/connections/{row["id"]}/syncs', headers=headers).status_code == 404


def test_douyin_simulated_video_snapshot_no_fake_conversion(client, operator):
    headers, _ = operator
    row = create(client, headers, provider='douyin', external_id='simulation-author')
    connect_and_queue(client, headers, row)
    service.tick()
    scoped = {**headers, 'X-Shop-ID': row['shop_id']}
    video = client.get('/api/ops/videos', headers=scoped).json()['items'][0]
    assert video['views'] == 1250
    assert video['completion_rate'] is None and video['click_rate'] is None
    connect_and_queue(client, headers, row)
    service.tick()
    video = client.get('/api/ops/videos', headers=scoped).json()['items'][0]
    assert video['views'] == 1250


def test_oauth_state_one_use_and_callback_cannot_bind_other_account(client, operator, monkeypatch):
    headers, _ = operator
    row = create(client, headers, provider='douyin', mode='live', external_id='', app_key='key', app_secret='secret',
                 redirect_uri='https://example.com/api/platform/oauth/callback')
    started = client.post(f'/api/platform/connections/{row["id"]}/authorize', headers=headers)
    assert started.status_code == 200
    from urllib.parse import urlparse, parse_qs
    state = parse_qs(urlparse(started.json()['url']).query)['state'][0]
    assert client.get('/api/platform/oauth/callback', params={'state': row['id'] + '.bad', 'code': 'x'}).status_code == 400
    monkeypatch.setattr(Client, 'token', lambda *args: {'access_token': 'private-token', 'expires_in': 3600, 'open_id': 'authorized-open-id'})
    result = client.get('/api/platform/oauth/callback', params={'state': state, 'code': 'authcode'})
    assert result.status_code == 200 and 'private-token' not in result.text
    assert client.get('/api/platform/oauth/callback', params={'state': state, 'code': 'authcode'}).status_code == 400
    saved = client.get('/api/platform/connections', headers=headers).json()['items'][0]
    assert saved['external_id'] == 'authorized-open-id'


@pytest.mark.parametrize('response', [httpx.Response(429), httpx.Response(503),
    httpx.Response(200, json={'code': 0, 'data': None}), httpx.Response(200, text='not json')])
def test_bad_responses_never_become_empty_success(response):
    with pytest.raises(PlatformError):
        check_response(response, 'doudian')


def test_mapping_rejects_incomplete_and_unsafe_values():
    for value in [1.5, True, None, -1, '199.00']:
        with pytest.raises(PlatformError):
            mapping.integer(value, '金额')
    with pytest.raises(PlatformError):
        list(mapping.orders(type('Connection', (), {'shop_id':'s','mode':'live','provider':'doudian','id':'c'})(),
                            {'shop_order_id': 'parent'}))


def test_official_success_envelope_and_statuses():
    assert check_response(httpx.Response(200, json={'code': 10000, 'data': {'items': []}}), 'doudian') == {'items': []}
    for code in [None, 0, 10001]:
        with pytest.raises(PlatformError):
            check_response(httpx.Response(200, json={'code': code, 'data': {}}), 'doudian')
    assert mapping.ORDER_STATES['4'] == 'cancelled' and mapping.ORDER_STATES['5'] == 'delivered'


def test_live_projection_mocked_transport_is_non_demo_and_never_merges_fixture_shops(client, operator, monkeypatch):
    headers, _ = operator
    from commerce.platform_fixtures import transport_for
    original = Client.__init__
    def test_transport(self, connection, credentials):
        original(self, connection, credentials)
        self.http.close()
        self.http = httpx.Client(transport=transport_for(connection))
    monkeypatch.setattr(Client, '__init__', test_transport)
    row = create(client, headers, mode='live', external_id='123456', app_key='test-key', app_secret='test-private-secret')
    connect_and_queue(client, headers, row)
    service.tick()
    result = client.get('/api/ops/overview', headers=headers).json()
    assert result['total']['paid_fen'] == 139300 and len(result['by_shop']) == 1
    assert result['is_demo'] is False
    product = client.post('/api/products/search', headers=headers, json={}).json()['items'][0]
    assert product['is_demo'] is False and product['source'] == 'doudian_openapi'
    assert product['cost_fen'] is None
    with SessionLocal() as db:
        records = list(db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row['shop_id'])))
        assert all(r.payload.get('is_demo') is False for r in records)


def test_fact_supplement_preserved_and_credentials_can_be_completed_in_stages(client, operator):
    headers, _ = operator
    draft = create(client, headers, mode='live', external_id='', app_key='key', app_secret='private-test-secret')
    changed = client.patch(f'/api/platform/connections/{draft["id"]}', headers=headers, json={
        'name': '补齐配置', 'provider': 'doudian', 'mode': 'live', 'external_id': '123456',
        'app_key': 'key', 'expected_version': draft['version']})
    assert changed.status_code == 200 and changed.json()['has_secret'] and not changed.json()['missing']
    row = create(client, headers)
    connect_and_queue(client, headers, row)
    service.tick()
    result = client.get(f'/api/platform/connections/{row["id"]}/products', headers=headers).json()['items'][0]
    response = client.patch(f'/api/platform/connections/{row["id"]}/products/{result["sku_id"]}',
        headers=headers, json={'cost_fen': 8950, 'size': 'M', 'material': '100%棉', 'evidence': '合成供货单'})
    assert response.status_code == 200
    connect_and_queue(client, headers, row)
    service.tick()
    saved = client.get(f'/api/platform/connections/{row["id"]}/products', headers=headers).json()['items'][0]
    assert saved['cost_fen'] == 8950 and saved['operator_facts_note'] == '合成供货单'
    scoped = {**headers, 'X-Shop-ID': row['shop_id']}
    search = client.post('/api/products/search', headers=scoped, json={'size': 'M', 'material': '棉'}).json()
    assert search['total'] == 1
    selected = client.get('/api/selection', headers=scoped).json()['incomplete'][0]
    assert selected['margin_percent'] is not None and '采购成本与毛利' not in selected['missing']


def test_scope_change_does_not_restore_old_demo_history_or_tool_context(client, operator):
    headers, user_id = operator
    from commerce.auth import scoped_actor, issue_token, scope_fingerprint
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, user_id))
        token = issue_token(db, actor, 'tool')
        db.add(ChatRun(user_id=user_id, shop_id=None, query='旧演示数据', status='succeeded',
                       scope_fingerprint=scope_fingerprint(actor)))
        db.commit()
    assert client.get('/api/chat/runs', headers=headers).json()['items']
    create(client, headers, mode='live', external_id='')
    assert not client.get('/api/chat/runs', headers=headers).json()['items']
    response = client.post('/tools/read', headers={'Authorization': 'Bearer ' + settings.tool_service_key,
        'X-Context-Ref': token}, json={'operation': 'ops_overview', 'arguments': {}})
    assert response.status_code == 409


def test_schedule_restart_recovery_and_longer_backfill(client, operator, monkeypatch):
    headers, _ = operator
    row = create(client, headers)
    queued = connect_and_queue(client, headers, row)
    with SessionLocal() as db:
        job = db.get(PlatformSync, queued['id'])
        job.status = 'running'
        db.commit()
    service.recover()
    assert service.tick()
    schedule = client.put(f'/api/platform/connections/{row["id"]}/schedule', headers=headers,
                          json={'enabled': True, 'interval_minutes': 60})
    assert schedule.status_code == 200
    assert service.tick()
    captured = []
    original = service.fetch_resource
    def spy(db, connection, client, resource, start, end):
        if resource == 'orders':
            captured.append((end-start).days)
        return original(db, connection, client, resource, start, end)
    monkeypatch.setattr(service, 'fetch_resource', spy)
    response = client.post(f'/api/platform/connections/{row["id"]}/sync', headers=headers,
                           json={'days': 30, 'resources': ['orders']})
    assert response.status_code == 200
    service.tick()
    assert captured and captured[0] >= 29
