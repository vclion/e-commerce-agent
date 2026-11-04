"""Revoked aggregate content and duplicate external IDs across authorized shops."""
import json
from uuid import uuid4

from sqlalchemy import delete, select

from commerce.auth import issue_token, password_hash, scoped_actor
from commerce.config import ROOT, settings
from commerce.db import SessionLocal, initialize_schema
from commerce.models import Draft, Membership, OpsRecord, User


def test_aggregate_draft_and_chat_dedup_recheck_source_memberships(client, auth):
    name = 'draft-scope-' + uuid4().hex[:12]
    with SessionLocal() as db:
        db.add(User(id=name, username=name, password_hash=password_hash(settings.demo_password),
                    role='operator', shop_id='a', display_name='范围回归'))
        db.flush()
        db.add_all([Membership(user_id=name, shop_id=sid) for sid in ['a', 'b']])
        db.commit()
    headers = auth(name)
    body = {'kind': 'report', 'title': '来源店铺范围回归', 'content': '原来汇总包含A和B的资料。'}
    old = client.post('/api/content', headers=headers, json=body).json()
    with SessionLocal() as db:
        assert db.get(Draft, old['id']).source_scope == {'a': 'demo', 'b': 'demo'}
        # Expanding access must not hide still-authorized earlier sources.
        db.add(Membership(user_id=name, shop_id='additional-shop'))
        db.commit()
    assert old['id'] in {r['id'] for r in client.get('/api/drafts', headers=headers).json()['items']}
    with SessionLocal() as db:
        db.execute(delete(Membership).where(Membership.user_id == name, Membership.shop_id == 'b'))
        db.commit()
    assert old['id'] not in {r['id'] for r in client.get('/api/drafts', headers=headers).json()['items']}
    with SessionLocal() as db:
        context = issue_token(db, scoped_actor(db, db.get(User, name)), 'tool', uuid4().hex, workspace='drafts')
    tools = {'Authorization': 'Bearer ' + settings.tool_service_key, 'X-Context-Ref': context}
    saved = client.post('/tools/execute', headers=tools,
                        json={'operation': 'content_save', 'arguments': body})
    assert saved.status_code == 200, saved.text
    assert saved.json()['data']['id'] != old['id']
    assert saved.json()['data']['id'] in {r['id'] for r in client.get('/api/drafts', headers=headers).json()['items']}


def test_unknown_legacy_aggregates_stay_hidden_and_single_shop_backfill_is_idempotent(client, auth):
    aggregate_id, single_id = uuid4().hex, uuid4().hex
    with SessionLocal() as db:
        for row_id, shop in [(aggregate_id, None), (single_id, 'a')]:
            db.add(Draft(id=row_id, user_id='operator-a', shop_id=shop, kind='report',
                         payload={'title': '历史来源回归', 'content': '历史内容'}, source_scope=None))
        db.commit()
    initialize_schema(); initialize_schema()
    with SessionLocal() as db:
        assert db.get(Draft, aggregate_id).source_scope is None
        assert db.get(Draft, single_id).source_scope == {'a': 'demo'}
    ids = {r['id'] for r in client.get('/api/drafts', headers=auth()).json()['items']}
    assert aggregate_id not in ids and single_id in ids


def test_report_cannot_cross_data_environment_change(client, auth):
    row_id = uuid4().hex
    with SessionLocal() as db:
        db.add(Draft(id=row_id, user_id='operator-a', shop_id=None, kind='report',
                     payload={'title': '环境回归', 'content': '来源模式不一致'}, source_scope={'a': 'live'}))
        db.commit()
    assert row_id not in {r['id'] for r in client.get('/api/drafts', headers=auth()).json()['items']}


def fixture_row(kind, shop):
    return next(json.loads(line) for line in (ROOT / f'data/douyin-ops-v1/{kind}.jsonl').read_text().splitlines()
                if json.loads(line)['shop_id'] == shop)


def test_same_external_ids_import_and_join_within_each_shop(client, auth):
    headers = auth()
    marker = uuid4().hex[:12]
    sku, order, video = 'shared-sku-' + marker, 'shared-order-' + marker, 'shared-video-' + marker
    product_rows, order_rows, refund_rows, video_rows, metric_rows = [], [], [], [], []
    for index, sid in enumerate(['a', 'b']):
        product_rows.append({**fixture_row('products', sid), 'sku_id': sku, 'size': ['M', 'L'][index]})
        original = fixture_row('orders', sid)
        order_rows.append({**original, 'sub_order_id': order, 'sku_id': sku,
                           'paid_at': '2026-10-15T23:59:00+08:00', 'shipped_at': None,
                           'delivered_at': None, 'paid_fen': [1000, 2000][index], 'refund_fen': 0, 'status': 'paid'})
        refund_rows.append({**fixture_row('refunds', sid), 'sku_id': sku, 'sub_order_id': order,
                            'refund_id': 'shared-refund-' + marker, 'reason': 'reason-' + sid,
                            'requested_fen': [100, 1500][index], 'refund_fen': [100, 1500][index]})
        video_rows.append({**fixture_row('videos', sid), 'video_id': video})
        metric_rows.append({**fixture_row('video_daily', sid), 'video_id': video, 'views': [1000, 2000][index],
                            'product_clicks': 1, 'completed_views': 10})
    def commit(kind, rows):
        preview = client.post('/api/ops/imports/preview', headers=headers,
                              json={'kind': kind, 'filename': 'scope.json', 'text': json.dumps(rows)}).json()
        assert preview['error_count'] == 0, preview
        result = client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers)
        assert result.status_code == 200, result.text
    for kind, rows in [('products', product_rows), ('orders', order_rows), ('refunds', refund_rows),
                       ('videos', video_rows), ('video_daily', metric_rows)]:
        commit(kind, rows)
    orders = [r for r in client.get('/api/orders', headers=headers).json()['items'] if r['id'] == order]
    assert len(orders) == 2
    for row in orders:
        assert row['refund_reason'] == 'reason-' + row['shop_id']
        assert row['specs_snapshot']['尺码'] == ('M' if row['shop_id'] == 'a' else 'L')
    # Shop A cannot borrow shop B's larger order amount for refund validation.
    too_large = {**refund_rows[0], 'refund_id': 'too-large-' + marker, 'requested_fen': 1500, 'refund_fen': 1500}
    preview = client.post('/api/ops/imports/preview', headers=headers,
                          json={'kind': 'refunds', 'filename': 'scope.json', 'text': json.dumps([too_large])}).json()
    assert client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers).status_code == 422
    from commerce import ops_data
    from fastapi import HTTPException
    import pytest
    with SessionLocal() as db:
        actor = scoped_actor(db, db.get(User, 'operator-a'))
        with pytest.raises(HTTPException, match='相同资料编号'):
            ops_data.record(db, actor, 'videos', video)
        assert ops_data.record(db, scoped_actor(db, db.get(User, 'operator-a'), 'a'), 'videos', video).shop_id == 'a'
