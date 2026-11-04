"""Evidence editing and source-report adaptation, using isolated database fixtures."""
import base64
import io
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select

from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import OpsAsset, OpsRecord, OpsScript

SKU = 'ENHANCE-SKU'
GROUP = 'ENHANCE-GROUP'


@pytest.fixture(scope='module')
def enhancement(database):
    from commerce.main import app
    with SessionLocal() as db:
        clock = db.scalar(select(OpsRecord).where(OpsRecord.kind == 'clock', OpsRecord.shop_id == 'a'))
        original_clock = clock.payload if clock else None
        product = dict(sku_id=SKU, group_id=GROUP, shop_id='a', title='棉混纺衬衫',
                       material='60%棉、40%聚酯纤维', size='M', price_fen=19900)
        db.add(OpsRecord(shop_id='a', kind='products', external_id=SKU, payload=product))
        db.add(OpsAsset(id='ENHANCE-ASSET', shop_id='a', group_id=GROUP, title='试穿素材',
                       relative_path='assets/enhancement.mp4', metadata_json={'duration_seconds': 12}))
        db.commit()
    with TestClient(app) as client:
        def login(username):
            token = client.post('/api/auth/login', json={'username': username, 'password': settings.demo_password}).json()['access_token']
            return {'Authorization': 'Bearer ' + token}
        yield client, login('operator'), {**login('operator-b'), 'X-Shop-ID': 'b'}
    with SessionLocal() as db:
        db.execute(delete(OpsScript).where(OpsScript.payload['product_id'].as_string() == SKU))
        db.execute(delete(OpsAsset).where(OpsAsset.id == 'ENHANCE-ASSET'))
        db.execute(delete(OpsRecord).where(OpsRecord.external_id == SKU))
        db.execute(delete(OpsRecord).where(OpsRecord.kind == 'orders', OpsRecord.external_id.like('20261018000000000%')))
        clock = db.scalar(select(OpsRecord).where(OpsRecord.kind == 'clock', OpsRecord.shop_id == 'a'))
        if clock:
            if original_clock is None:
                db.delete(clock)
            else:
                clock.payload = original_clock
        db.commit()


def new_script(client, headers):
    body = dict(product_id=SKU, title='棉混纺衬衫', segments=[
        dict(asset_id='ENHANCE-ASSET', in_seconds=0, out_seconds=3, caption='纯棉面料，限时五折，只要99元'),
        dict(asset_id='ENHANCE-ASSET', in_seconds=3, out_seconds=6, caption='当前SKU为M码'),
        dict(asset_id='ENHANCE-ASSET', in_seconds=6, out_seconds=9, caption='防水面料', locked=True)])
    result = client.post('/api/ops/scripts', headers=headers, json=body)
    assert result.status_code == 200, result.text
    return result.json()


def test_review_grounds_claims_and_rewrite_preserves_locks_and_good_segments(enhancement, monkeypatch):
    from commerce import ops_media
    client, headers, restricted = enhancement
    script = new_script(client, headers)
    found = {(i['segment_index'], i['category']) for i in script['payload']['review']['issues']}
    assert {(0, 'price'), (0, 'material'), (0, 'promotion'), (2, 'performance')} <= found
    assert not any(index == 1 for index, _ in found)
    body = {'expected_version': 1}
    url = f'/api/ops/scripts/{script["id"]}'
    assert client.post(url + '/review', headers=restricted, json=body).status_code == 404
    assert client.post(url + '/review', headers=headers, json={'expected_version': 99}).status_code == 409
    calls = []
    def generate(*args):
        calls.append(args)
        return {'title': '模型试图改掉好标题', 'segments': [
            {'caption': '面料为60%棉、40%聚酯纤维，售价199元'},
            {'caption': '模型试图改掉好字幕', 'asset_id': 'other-shop'},
            {'caption': '模型试图改掉锁定字幕', 'in_seconds': 99}]}
    monkeypatch.setattr(ops_media, 'call_workflow', generate)
    response = client.post(url + '/rewrite', headers=headers, json=body)
    assert response.status_code == 200, response.text
    candidate = response.json()
    assert len(calls) == 1 and candidate['rounds'] == 1
    assert candidate['title'] == script['title']
    assert candidate['segments'][1:] == script['payload']['segments'][1:]
    assert candidate['segments'][0]['asset_id'] == 'ENHANCE-ASSET'
    assert candidate['segments'][0]['in_seconds'] == 0
    assert {i['segment_index'] for i in candidate['review']['issues']} == {2}
    with SessionLocal() as db:
        assert db.get(OpsScript, script['id']).version == 1
    saved = client.patch(url, headers=headers, json=dict(product_id=SKU, title=candidate['title'],
        segments=candidate['segments'], expected_version=candidate['expected_version']))
    assert saved.status_code == 200 and saved.json()['version'] == 2
    assert client.patch(url, headers=headers, json=dict(product_id=SKU, title=candidate['title'],
        segments=candidate['segments'], expected_version=1)).status_code == 409


def test_rewrite_rejects_model_shape_and_concurrent_edits(enhancement, monkeypatch):
    from commerce import ops_media
    client, headers, _ = enhancement
    script = new_script(client, headers)
    url = f'/api/ops/scripts/{script["id"]}/rewrite'
    monkeypatch.setattr(ops_media, 'call_workflow', lambda *args: {'segments': []})
    assert client.post(url, headers=headers, json={'expected_version': 1}).status_code == 502
    def concurrent_edit(*args):
        with SessionLocal() as db:
            row = db.get(OpsScript, script['id'])
            row.version += 1
            row.title = '另一个运营已保存'
            db.commit()
        return {'segments': [{'caption': '棉混纺面料'}] * 3}
    monkeypatch.setattr(ops_media, 'call_workflow', concurrent_edit)
    assert client.post(url, headers=headers, json={'expected_version': 1}).status_code == 409
    with SessionLocal() as db:
        assert db.get(OpsScript, script['id']).title == '另一个运营已保存'


def workbook_body(bad_status=False, formula=False):
    workbook = Workbook()
    workbook.active.title = '说明'
    workbook.active.append(['提示'])
    workbook.active.append(['这是用于测试适配器的合成表头，不代表真实平台模板'])
    sheet = workbook.create_sheet('订单')
    source = dict(子订单号='202610180000000001', 父订单号='202610180000000000',
        店铺ID='a', 买家编号='TEST-BUYER', SKU编号=SKU, 款式ID=GROUP, 商品名称='棉混纺衬衫',
        数量=1, 支付金额='99.50', 成本金额='10.00', 下单时间=datetime(2026, 10, 17, 9),
        支付时间=datetime(2026, 10, 17, 10), 承诺发货时间=datetime(2026, 10, 19, 10),
        发货时间=None, 签收时间=None, 订单状态='待发货', 渠道='短视频', 视频ID=None,
        退款金额='0.00', 更新时间=datetime(2026, 10, 17, 10))
    sheet.append(list(source))
    sheet.append(list(source.values()))
    sheet.append([None] * len(source))
    second = {**source, '子订单号': '202610180000000002'}
    if bad_status:
        second['订单状态'] = '无法识别的状态'
    if formula:
        second['支付金额'] = '=10+20'
    sheet.append(list(second.values()))
    binary = io.BytesIO()
    workbook.save(binary)
    return dict(kind='orders', filename='订单.xlsx', content_base64=base64.b64encode(binary.getvalue()).decode(), sheet='订单')


def inspect_order(client, headers, **kwargs):
    body = workbook_body(**kwargs)
    response = client.post('/api/ops/imports/inspect', headers=headers, json=body)
    assert response.status_code == 200, response.text
    table = response.json()
    assert table['row_count'] == 2 and table['sheets'] == ['说明', '订单']
    mapping = table['mapping']
    mapping['shop_id'] = 'a'
    assert mapping['columns']['sub_order_id'] == '子订单号'
    assert mapping['money_units']['paid_fen'] == 'yuan'
    return {**body, 'mapping': mapping}


def test_excel_mapping_preserves_child_orders_ids_units_and_duplicates(enhancement):
    client, headers, restricted = enhancement
    body = inspect_order(client, headers)
    response = client.post('/api/ops/imports/preview', headers=headers, json=body)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['error_count'] == 0, preview
    assert preview['valid_rows'] == 2
    assert [r['sub_order_id'] for r in preview['preview']] == ['202610180000000001', '202610180000000002']
    assert {r['parent_order_id'] for r in preview['preview']} == {'202610180000000000'}
    assert all(r['paid_fen'] == 9950 and r['is_demo'] is False for r in preview['preview'])
    assert preview['preview'][0]['paid_at'] == '2026-10-17T10:00:00+08:00'
    assert preview['preview'][0]['status'] == 'paid'
    assert client.post('/api/ops/imports/preview', headers=restricted, json=body).status_code == 403
    commit = '/api/ops/imports/' + preview['id'] + '/commit'
    assert client.post(commit, headers=headers).json()['imported'] == 2
    assert client.post(commit, headers=headers).json()['duplicate_batch']
    again = client.post('/api/ops/imports/preview', headers=headers, json=body).json()
    assert again['id'] == preview['id']


@pytest.mark.parametrize('bad_status,formula,expected', [(True, False, '未知值'), (False, True, '公式单元格')])
def test_excel_row_errors_prevent_partial_commit(enhancement, bad_status, formula, expected):
    client, headers, _ = enhancement
    body = inspect_order(client, headers, bad_status=bad_status, formula=formula)
    preview = client.post('/api/ops/imports/preview', headers=headers, json=body).json()
    assert preview['valid_rows'] == 1 and preview['error_count'] == 1
    assert preview['errors'][0]['row'] == 4 and expected in preview['errors'][0]['message']
    assert client.post(f'/api/ops/imports/{preview["id"]}/commit', headers=headers).status_code == 422


def test_mapping_template_scope_and_validation(enhancement):
    client, headers, restricted = enhancement
    body = inspect_order(client, headers)
    template = dict(kind='orders', name='合成中文表头', mapping=body['mapping'])
    assert client.post('/api/ops/import-mappings', headers=headers, json=template).status_code == 200
    assert any(t['name'] == '合成中文表头' for t in client.get('/api/ops/import-mappings', headers=headers).json()['items'])
    assert not any(t['name'] == '合成中文表头' for t in client.get('/api/ops/import-mappings', headers=restricted).json()['items'])
    body['mapping']['columns']['parent_order_id'] = '子订单号'
    assert client.post('/api/ops/imports/preview', headers=headers, json=body).status_code == 422


def test_mapped_money_never_silently_rounds_or_double_converts(enhancement):
    client, headers, _ = enhancement
    from commerce.ops_schemas import ImportMapping
    from commerce.ops_imports import convert_value
    spec = {'type': 'int', 'nullable': False}
    with pytest.raises(ValueError):
        convert_value('orders', 'paid_fen', '0.001', spec, ImportMapping(money_units={'paid_fen': 'yuan'}))
    with pytest.raises(ValueError):
        convert_value('orders', 'paid_fen', '1,,000', spec, ImportMapping(money_units={'paid_fen': 'yuan'}))
    assert convert_value('orders', 'paid_fen', '9950', spec, ImportMapping(money_units={'paid_fen': 'fen'})) == '9950'
    assert convert_value('orders', 'paid_fen', '1,000.50', spec, ImportMapping(money_units={'paid_fen': 'yuan'})) == '100050'
    assert convert_value('orders', 'paid_fen', '1234567890123456789012345678.91', spec,
                         ImportMapping(money_units={'paid_fen': 'yuan'})) == '123456789012345678901234567891'
    with pytest.raises(ValueError):
        convert_value('orders', 'paid_fen', '99元', spec, ImportMapping(money_units={'paid_fen': 'fen'}))
    assert client.post('/api/ops/imports/inspect', headers=headers, json={**workbook_body(), 'content_base64': 'bad!'}).status_code == 422


def test_material_percentages_and_synonyms_are_checked_against_evidence():
    from commerce.ops_content import review
    product = dict(material='60%棉、40%聚酯纤维', size='M', price_fen=19900)
    accurate = review(product, '棉混纺衬衫', [{'caption': '60%棉、40%涤纶，当前SKU为M码，售价199元'}])
    assert accurate['issue_count'] == 0
    inaccurate = review(product, '80%棉衬衫', [{'caption': '60%羊绒，L码'}])
    assert any(i['segment_index'] == -1 and i['category'] == 'material' for i in inaccurate['issues'])
    assert any(i['category'] == 'size' for i in inaccurate['issues'])
