"""Imported operations data, deterministic metrics, and source provenance."""
import csv
import hashlib
import io
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .auth import scope_shop_ids, require_role, shop_info
from .business import fail, audit
from .catalog import SHOPS
from .config import ROOT
from .models import OpsImport, OpsRecord, OpsTask, Product, now, uid

ZONE = ZoneInfo('Asia/Shanghai')
FIXTURES = ROOT / 'data/douyin-ops-v1'
KEYS = dict(shops=['shop_id'], staff=['staff_id'], products=['sku_id'], orders=['sub_order_id'],
            refunds=['refund_id'], daily_reports=['date'], inventory=['sku_id'], videos=['video_id'],
            video_daily=['video_id', 'date'], sop_templates=['template_id'], tasks=['task_id'],
            peer_references=['reference_id'])


def allowed(user, shop):
    require_role(user, 'operator')
    if shop not in scope_shop_ids(user):
        fail(403, '无权操作此店铺')


def records(db, user, kind):
    return [r.payload for r in db.scalars(select(OpsRecord).where(
        OpsRecord.shop_id.in_(scope_shop_ids(user)), OpsRecord.kind == kind))]


def record(db, user, kind, external_id):
    rows = list(db.scalars(select(OpsRecord).where(OpsRecord.shop_id.in_(scope_shop_ids(user)),
                    OpsRecord.kind == kind, OpsRecord.external_id == external_id).limit(2)))
    if not rows:
        fail(404, '资料不存在或不可访问')
    if len(rows) > 1:
        fail(422, '多个授权店铺存在相同资料编号，请先选择单店')
    return rows[0]


def clock(db, user):
    values = records(db, user, 'clock')
    return max((datetime.fromisoformat(r['as_of']) for r in values), default=now()).astimezone(ZONE)


def read_text(kind, filename, text):
    if kind not in KEYS:
        fail(422, '不支持的报表类型')
    if len(text.encode('utf-8')) > 24 * 1024 * 1024:
        fail(413, '报表最大24MB')
    text = text.lstrip('\ufeff').strip()
    try:
        if filename.lower().endswith('.csv'):
            reader = csv.DictReader(io.StringIO(text))
            if len(reader.fieldnames or []) != len(set(reader.fieldnames or [])):
                fail(422, 'CSV包含重复表头')
            rows = list(reader)
        elif text.startswith('['):
            rows = json.loads(text)
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (ValueError, csv.Error):
        fail(422, '文件不是有效的CSV、JSON数组或JSONL；请检查编码与格式')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 25000:
        fail(422, '单次导入1～25,000行')
    return rows


def normalize(kind, raw, user):
    if not isinstance(raw, dict):
        raise ValueError('每行必须是字段对象')
    if None in raw:
        raise ValueError('CSV列数超过表头，请检查多余的逗号或字段引号')
    schema = json.loads((FIXTURES / 'schema.json').read_text())
    fields = schema[kind]['fields']
    row = dict(raw)
    # SOP templates are shop-neutral in the fixture; preview expands them explicitly.
    for key, spec in fields.items():
        value = row.get(key)
        if value == '' or value is None:
            if spec['nullable']:
                row[key] = None
                continue
            raise ValueError(f'缺少字段 {key}')
        if spec['type'] == 'bool':
            if str(value).lower() not in ['true', 'false']:
                raise ValueError(f'{key} 必须为true或false')
            row[key] = str(value).lower() == 'true'
        elif spec['type'] == 'int':
            if isinstance(value, bool) or not re.fullmatch(r'[+-]?\d+', str(value)):
                raise ValueError(f'{key} 必须为整数')
            row[key] = int(value)
            if row[key] < 0 and key != 'net_receipts_fen':
                raise ValueError(f'{key} 不能为负数')
        elif spec['type'] == 'str':
            if not isinstance(value, str) or len(value) > 12000:
                raise ValueError(f'{key} 必须是有效文本')
            row[key] = value.strip()
            if not row[key] and not spec['nullable']:
                raise ValueError(f'缺少字段 {key}')
            if key.endswith('_at'):
                parsed = datetime.fromisoformat(value)
                if parsed.tzinfo is None:
                    raise ValueError(f'{key} 必须带时区')
            if key == 'date':
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('日期必须为YYYY-MM-DD')
                date.fromisoformat(value)
    if kind == 'sop_templates' and not row.get('shop_id'):
        raise ValueError('SOP模板需要指定店铺')
    allowed(user, row.get('shop_id'))
    if row['shop_id'] not in SHOPS and row['shop_id'] not in getattr(user, 'shop_infos', {}):
        raise ValueError('未知店铺')
    if getattr(user, 'shop_infos', {}).get(row['shop_id'], {}).get('mode') == 'live' and row.get('is_demo'):
        raise ValueError('不能将演示记录导入正式店铺，请使用演练店铺')
    if any(not isinstance(row.get(key), str) or len(row[key]) > 100 for key in KEYS[kind]):
        raise ValueError('主键缺失或超过长度限制')
    if kind == 'orders':
        if row['quantity'] <= 0 or row['refund_fen'] > row['paid_fen']:
            raise ValueError('订单数量或退款金额不合法')
        if bool(row['paid_at']) != (row['paid_fen'] > 0):
            raise ValueError('支付时间与支付金额不一致')
        timeline = [datetime.fromisoformat(row[k]) for k in ['created_at', 'paid_at', 'shipped_at', 'delivered_at'] if row.get(k)]
        if timeline != sorted(timeline):
            raise ValueError('订单事件时间顺序错误')
    if kind == 'refunds' and row['refund_fen'] > row['requested_fen']:
        raise ValueError('退款金额超过申请金额')
    if kind == 'daily_reports' and row['net_receipts_fen'] != row['paid_fen'] - row['refund_fen']:
        raise ValueError('净收款必须等于支付减期间退款')
    if kind == 'daily_reports' and row['paid_buyers'] > row['visitors']:
        raise ValueError('支付买家数超过访客数')
    if kind == 'video_daily':
        if row['metric_grain'] not in ['daily_increment', 'cumulative']:
            raise ValueError('视频指标口径必须为daily_increment或cumulative')
        if row['product_clicks'] > row['views'] or (row['completed_views'] is not None and row['completed_views'] > row['views']):
            raise ValueError('视频点击或完整播放超过播放量')
    if kind == 'sop_templates' and not 0 <= row['due_hour'] <= 23:
        raise ValueError('SOP执行小时必须为0～23')
    if kind == 'tasks' and row['status'] not in ['pending', 'in_progress', 'blocked', 'review', 'completed', 'cancelled']:
        raise ValueError('任务状态不合法')
    return row


def external_key(kind, row):
    key = '|'.join(str(row[k]) for k in KEYS[kind])
    return key + '|' + row['extracted_at'] if kind == 'video_daily' and row['metric_grain'] == 'cumulative' else key


def preview(db, user, kind, filename, text, *, prepared=None):
    raw = prepared if prepared is not None else [(i, r, None) for i, r in enumerate(read_text(kind, filename, text), 2)]
    if kind == 'sop_templates':
        raw = [(i, {**r, 'shop_id': sid} if isinstance(r, dict) else r, error)
               for i, r, error in raw for sid in ([r['shop_id']] if isinstance(r, dict) and r.get('shop_id') else scope_shop_ids(user))]
    rows, errors, seen, duplicates = [], [], {}, 0
    for i, value, conversion_error in raw:
        try:
            if conversion_error:
                raise ValueError(conversion_error)
            row = normalize(kind, value, user)
            key = (row['shop_id'], external_key(kind, row))
            if key in seen:
                if seen[key] != row:
                    raise ValueError('同一主键出现冲突内容')
                duplicates += 1
                continue
            seen[key] = row
            rows.append(row)
        except (ValueError, TypeError) as exc:
            errors.append({'row': i, 'message': str(exc)[:250]})
    fingerprint = hashlib.sha256(json.dumps([kind, list(scope_shop_ids(user)), rows, errors], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    batch = db.scalar(select(OpsImport).where(OpsImport.user_id == user.id, OpsImport.fingerprint == fingerprint))
    if not batch:
        batch = OpsImport(user_id=user.id, shop_scope=list(scope_shop_ids(user)), kind=kind,
                          filename=filename[:180], fingerprint=fingerprint, rows=rows, errors=errors)
        db.add(batch)
        db.commit()
    return {'id': batch.id, 'status': batch.status, 'kind': kind, 'valid_rows': len(rows),
            'duplicates': duplicates, 'error_count': len(errors), 'errors': errors[:100], 'preview': rows[:5]}


def check_references(db, user, kind, rows):
    if kind in ['orders', 'inventory', 'refunds']:
        known = {(r['shop_id'], r['sku_id']) for r in records(db, user, 'products')}
        known.update((p.shop_id, p.id) for p in db.scalars(select(Product).where(Product.shop_id.in_(scope_shop_ids(user)))))
        if any((r['shop_id'], r['sku_id']) not in known for r in rows):
            fail(422, 'SKU不存在或不属于报表店铺，请先导入商品')
    if kind in ['video_daily']:
        known = {(r['shop_id'], r['video_id']) for r in records(db, user, 'videos')}
        if any((r['shop_id'], r['video_id']) not in known for r in rows):
            fail(422, '视频不存在或跨店，请先导入视频清单')
    if kind == 'refunds':
        known = {(r['shop_id'], r['sub_order_id']): r for r in records(db, user, 'orders')}
        for row in rows:
            order = known.get((row['shop_id'], row['sub_order_id']))
            if not order or order['shop_id'] != row['shop_id'] or row['requested_fen'] > order['paid_fen']:
                fail(422, '售后缺少对应店铺订单或金额超过支付金额')


def apply_rows(db, user, kind, rows, batch_id):
    check_references(db, user, kind, rows)
    existing = {(r.shop_id, r.external_id): r for r in db.scalars(select(OpsRecord).where(
        OpsRecord.shop_id.in_(scope_shop_ids(user)), OpsRecord.kind == kind))}
    values = []
    for row in rows:
        allowed(user, row['shop_id'])
        key = external_key(kind, row)
        previous = existing.get((row['shop_id'], key))
        if kind == 'peer_references' and previous and previous.payload.get('transcript') == row.get('transcript'):
            row = {**row, **{k: previous.payload[k] for k in ['analysis', 'analyzed_at', 'analysis_basis', 'limitation', 'measurements', 'asset_id'] if k in previous.payload}}
        def captured(item):
            return next((datetime.fromisoformat(item[k]).timestamp() for k in ['updated_at', 'snapshot_at', 'extracted_at', 'data_as_of'] if item.get(k)), 0)
        if previous and captured(row) < captured(previous.payload):
            continue
        values.append(dict(id=uid(), shop_id=row['shop_id'], kind=kind, external_id=key,
                           payload=row, batch_id=batch_id, updated_at=now()))
    for offset in range(0, len(values), 300):
        statement = insert(OpsRecord).values(values[offset:offset + 300])
        db.execute(statement.on_conflict_do_update(index_elements=['shop_id', 'kind', 'external_id'],
            set_={'payload': statement.excluded.payload, 'batch_id': batch_id, 'updated_at': now()}))
    for row in rows if kind == 'tasks' else []:
        if kind == 'tasks' and not db.get(OpsTask, row['task_id']):
            db.add(OpsTask(id=row['task_id'], shop_id=row['shop_id'], work_date=date.fromisoformat(row['date']),
                template_id=row['template_id'], title=row['title'], owner=row['owner_id'],
                due_at=datetime.fromisoformat(row['due_at']), priority=row['priority'], status=row['status'],
                evidence=row.get('evidence') or '', note='导入的测试任务', context={'source_batch': batch_id, 'completed_at': row.get('completed_at')}, history=[]))
    db.flush()
    # Advance the analysis clock only with timestamped source data, never by file upload time.
    if kind in ['daily_reports', 'inventory', 'orders', 'video_daily', 'shops']:
        for sid in {r['shop_id'] for r in rows}:
            values_for_shop = [r for r in rows if r['shop_id'] == sid]
            sources = [r.get(k) for r in values_for_shop for k in ['extracted_at', 'snapshot_at', 'updated_at', 'data_as_of'] if r.get(k)]
            if sources:
                latest = max(sources, key=datetime.fromisoformat)
                current = db.scalar(select(OpsRecord).where(OpsRecord.kind == 'clock', OpsRecord.shop_id == sid))
                if not current:
                    db.add(OpsRecord(shop_id=sid, kind='clock', external_id='reference', payload={'as_of': latest, 'is_demo': any(r.get('is_demo') for r in values_for_shop)}))
                elif datetime.fromisoformat(latest) > datetime.fromisoformat(current.payload['as_of']):
                    current.payload = {**current.payload, 'as_of': latest}
    db.flush()
    return len(values)


def commit(db, user, batch_id):
    batch = db.scalar(select(OpsImport).where(OpsImport.id == batch_id, OpsImport.user_id == user.id).with_for_update())
    if not batch:
        fail(404, '导入批次不存在')
    if batch.kind not in KEYS:
        fail(410, '该报表类型已停用')
    if sorted(batch.shop_scope) != sorted(scope_shop_ids(user)):
        fail(409, '店铺范围已变化，请在当前范围重新校验')
    for sid in batch.shop_scope:
        allowed(user, sid)
    if batch.errors:
        fail(422, '报表存在错误，未导入任何记录')
    if batch.status == 'committed':
        return {'id': batch.id, 'imported': 0, 'duplicate_batch': True}
    count = apply_rows(db, user, batch.kind, batch.rows, batch.id)
    batch.status = 'committed'
    audit(db, user, 'ops.import', {'batch_id': batch.id, 'kind': batch.kind, 'count': count})
    db.commit()
    return {'id': batch.id, 'imported': count, 'duplicate_batch': False}


def overview(db, user, days=7, end=None):
    if not 1 <= days <= 90:
        fail(422, '周期必须为1～90天')
    as_of = clock(db, user)
    end = end or as_of.date()
    start, previous_start = end - timedelta(days=days), end - timedelta(days=days * 2)
    daily = records(db, user, 'daily_reports')
    current = [r for r in daily if start.isoformat() <= r['date'] < end.isoformat()]
    previous = [r for r in daily if previous_start.isoformat() <= r['date'] < start.isoformat()]
    fields = ['paid_fen', 'refund_fen', 'net_receipts_fen', 'paid_parent_orders', 'paid_sub_orders', 'visitors', 'paid_buyers']
    by_shop = []
    def known_sum(rows, field):
        return sum(r[field] for r in rows) if rows and all(r.get(field) is not None for r in rows) else None
    for sid in scope_shop_ids(user):
        rows = [r for r in current if r['shop_id'] == sid]
        result = {key: known_sum(rows, key) for key in fields}
        prev = sum(r['paid_fen'] for r in previous if r['shop_id'] == sid and r.get('paid_fen') is not None)
        covered = {r['date'] for r in rows if r.get('coverage_complete', True)}
        missing = [(start + timedelta(days=i)).isoformat() for i in range(days) if (start + timedelta(days=i)).isoformat() not in covered]
        previous_covered = {r['date'] for r in previous if r['shop_id'] == sid and r.get('coverage_complete', True)}
        previous_missing = [(previous_start + timedelta(days=i)).isoformat() for i in range(days)
                            if (previous_start + timedelta(days=i)).isoformat() not in previous_covered]
        result.update(shop_id=sid, shop_name=shop_info(user, sid)['name'], previous_paid_fen=prev,
                      growth_percent=round((result['paid_fen'] / prev - 1) * 100, 2) if not missing and not previous_missing and prev else None,
                      buyer_day_conversion=result['paid_buyers'] / result['visitors'] if result['paid_buyers'] is not None and result['visitors'] else None,
                      missing_dates=missing, complete=not missing, previous_missing_dates=previous_missing,
                      latest_date=max((r['date'] for r in daily if r['shop_id'] == sid), default=None))
        by_shop.append(result)
    total = {key: known_sum(current, key) for key in fields}
    total['buyer_day_conversion'] = total['paid_buyers'] / total['visitors'] if total['paid_buyers'] is not None and total['visitors'] else None
    comparable = bool(by_shop) and all(r['complete'] and not r['previous_missing_dates'] for r in by_shop)
    total['growth_percent'] = round((total['paid_fen'] / sum(r['paid_fen'] for r in previous) - 1) * 100, 2) if comparable and sum(r['paid_fen'] for r in previous) else None
    paid_orders = [r for r in records(db, user, 'orders') if r.get('paid_at') and start <= datetime.fromisoformat(r['paid_at']).astimezone(ZONE).date() < end]
    refund_rows = [r for r in records(db, user, 'refunds') if r.get('completed_at') and start <= datetime.fromisoformat(r['completed_at']).astimezone(ZONE).date() < end]
    reasons = Counter()
    for refund in refund_rows:
        reasons[refund['reason']] += refund['refund_fen']
    inventory = records(db, user, 'inventory')
    risks = [dict(sku_id=r['sku_id'], shop_id=r['shop_id'], available_stock=r['available_stock'],
                  sold_units_7d=r['sold_units_7d'], snapshot_at=r['snapshot_at'])
                      for r in inventory if r.get('sold_units_7d') is not None and r.get('available_stock') is not None and r['sold_units_7d'] > r['available_stock']]
    overdue = [r for r in records(db, user, 'orders') if r['paid_at'] and not r['shipped_at'] and r['status'] not in ['cancelled', 'refunded'] and r.get('promised_ship_at') and datetime.fromisoformat(r['promised_ship_at']) < as_of]
    reconciled = {'payments': sum(r['paid_fen'] for r in paid_orders) == total['paid_fen'] if paid_orders and current else None,
                  'refunds': sum(r['refund_fen'] for r in refund_rows) == total['refund_fen'] if refund_rows and current else None}
    return {'period': {'start': start.isoformat(), 'end_exclusive': end.isoformat(), 'timezone': 'Asia/Shanghai'},
            'as_of': as_of.isoformat(), 'source': 'imported_reports', 'is_demo': any(r.get('is_demo') for r in daily),
            'total': total, 'by_shop': by_shop, 'complete': bool(by_shop) and all(r['complete'] for r in by_shop),
            'low_stock': risks[:50], 'overdue_shipments': [{k: r[k] for k in ['sub_order_id', 'shop_id', 'promised_ship_at']} for r in overdue[:100]],
            'overdue_shipment_count': len(overdue), 'refund_by_reason': dict(reasons), 'reconciliation': reconciled,
            'trend': [dict(date=(start + timedelta(days=i)).isoformat(),
                           paid_fen=sum(r['paid_fen'] for r in current if r['date'] == (start + timedelta(days=i)).isoformat())
                           if all((start + timedelta(days=i)).isoformat() not in s['missing_dates'] for s in by_shop) else None)
                      for i in range(days)],
            'notes': ['金额单位为整数分；净收款不是利润。', '人数为买家日与访客日，不是跨日去重人数。', '缺失日期的汇总为已覆盖日期小计，不能当作完整周期比较。']}


def videos(db, user):
    metrics = records(db, user, 'video_daily')
    result = []
    for item in records(db, user, 'videos'):
        rows = sorted([r for r in metrics if r['video_id'] == item['video_id'] and r['shop_id'] == item['shop_id']], key=lambda r: (r['date'], datetime.fromisoformat(r['extracted_at']).timestamp()))
        grains = {r['metric_grain'] for r in rows}
        if len(grains) > 1:
            result.append({**item, 'views': None, 'click_rate': None, 'completion_rate': None, 'warning': '混合累计与增量口径，请先统一报表'})
            continue
        used = rows[-1:] if grains == {'cumulative'} else rows
        values = {key: sum(r[key] for r in used) if used and all(r.get(key) is not None for r in used) else None
                  for key in ['views', 'likes', 'comments', 'shares', 'follows', 'completed_views', 'product_clicks', 'attributed_sub_orders', 'attributed_paid_fen']}
        values['click_rate'] = values['product_clicks'] / values['views'] if values['views'] and values['product_clicks'] is not None else None
        values['completion_rate'] = values['completed_views'] / values['views'] if values['views'] and values['completed_views'] is not None else None
        warning = '缺少完整播放次数' if used and values['completed_views'] is None else ''
        if values['views'] and values['views'] >= 100000 and values['click_rate'] is not None and values['click_rate'] < .01:
            warning = '高播放低点击：建议测试商品关联与转化引导'
        result.append({**item, **values, 'warning': warning, 'metric_grain': next(iter(grains), None), 'latest_date': rows[-1]['date'] if rows else None})
    return {'items': sorted(result, key=lambda r: r.get('views') or 0, reverse=True)}
