"""Connection lifecycle, durable synchronization, and normalized business projections."""
import asyncio
import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .auth import scoped_actor, scope_shop_ids
from .business import fail, audit
from .db import SessionLocal
from .models import PlatformConnection, PlatformShop, PlatformSync, Membership, OpsRecord, User, now, uid
from .ops_data import ZONE, FIXTURES
from . import platform_mapping as mapping
from .platform_client import Client, PlatformError
from .platform_vault import encrypt, decrypt

RESOURCES = {'doudian': ['products', 'orders', 'refunds'], 'douyin': ['videos', 'video_metrics']}
logger = logging.getLogger('commerce.platform')


def connection(db, user, identifier, lock=False):
    stmt = select(PlatformConnection).where(PlatformConnection.id == identifier,
        PlatformConnection.user_id == user.id, PlatformConnection.shop_id.in_(scope_shop_ids(user)))
    # Connections remain manageable when the default business scope prefers live shops.
    if user.shop_id is None:
        stmt = select(PlatformConnection).where(PlatformConnection.id == identifier,
            PlatformConnection.user_id == user.id, PlatformConnection.shop_id.in_(user.allowed_shop_ids))
    row = db.scalar(stmt.with_for_update() if lock else stmt)
    if not row:
        fail(404, '接入配置不存在或不可访问')
    return row


def view(row):
    credentials = decrypt(row.credentials)
    missing = []
    if row.mode == 'live':
        token_only = row.provider == 'douyin' and credentials.get('access_token')
        if not row.app_key and not token_only:
            missing.append('App Key / Client Key')
        if not credentials.get('app_secret') and not token_only:
            missing.append('App Secret / Client Secret')
        if not row.external_id or row.external_id.startswith('pending-'):
            missing.append('店铺ID' if row.provider == 'doudian' else '账号授权 / Open ID')
    return dict(id=row.id, shop_id=row.shop_id, provider=row.provider, mode=row.mode,
        external_id='' if row.external_id.startswith('pending-') else row.external_id, app_key=row.app_key,
        has_secret=bool(credentials.get('app_secret')), has_token=bool(credentials.get('access_token')),
        status=row.status, error=row.error, capabilities=row.capabilities, expires_at=row.expires_at,
        last_sync_at=row.last_sync_at, next_sync_at=row.next_sync_at, enabled=row.enabled,
        interval_minutes=row.interval_minutes, version=row.version, missing=missing,
        options={k: v for k, v in row.options.items() if k in ['redirect_uri', 'scopes']},
        resources=RESOURCES[row.provider], created_at=row.created_at)


def sync_view(row):
    return {k: getattr(row, k) for k in ['id', 'connection_id', 'status', 'resources', 'days',
            'attempts', 'result', 'error', 'retry_at', 'created_at', 'finished_at']}


def save(db, user, body, identifier=None):
    if identifier:
        row = connection(db, user, identifier, True)
        if row.version != body.expected_version:
            fail(409, '接入配置已更新，请刷新后重试')
        if db.scalar(select(PlatformSync.id).where(PlatformSync.connection_id == row.id,
                                                PlatformSync.status.in_(['queued', 'running', 'retry_wait']))):
            fail(409, '同步尚未结束，请等待或取消排队任务后修改配置')
        if (row.provider, row.mode) != (body.provider, body.mode):
            fail(422, '接入平台和演练/正式模式不可更换，请新建连接')
        if body.external_id and body.external_id != row.external_id and row.last_sync_at:
            fail(409, '已有同步数据的连接不能更换外部账号，请新建连接')
    else:
        if body.shop_id:
            shop = db.get(PlatformShop, body.shop_id)
            if not shop or shop.id not in user.allowed_shop_ids or shop.mode != body.mode:
                fail(403, '只能关联已有授权且模式一致的平台店铺')
            if body.provider == 'doudian' and db.scalar(select(PlatformConnection.id).where(
                PlatformConnection.shop_id == shop.id, PlatformConnection.provider == 'doudian')):
                fail(409, '该本地店铺已经关联抖店，请编辑原连接，避免混入其他店铺数据')
        else:
            shop = PlatformShop(id='s' + uid()[:18], name=body.name, mode=body.mode)
            db.add(shop)
            db.flush()
            db.add(Membership(user_id=user.id, shop_id=shop.id))
        row = PlatformConnection(id=uid(), user_id=user.id, shop_id=shop.id,
            provider=body.provider, mode=body.mode, external_id=body.external_id or 'pending-' + uid(),
            options={}, capabilities={}, status='unconfigured')
        db.add(row)
        db.flush()
    secrets_value = decrypt(row.credentials)
    changed_key = body.app_key is not None and body.app_key != row.app_key
    changed_auth = changed_key or bool(body.app_secret) or bool(body.external_id and body.external_id != row.external_id)
    if changed_auth:
        secrets_value = {'app_secret': secrets_value['app_secret']} if not changed_key and secrets_value.get('app_secret') else {}
        row.expires_at = None
        row.next_sync_at = None
        row.status, row.enabled = 'unconfigured', False
        row.capabilities = {}
    if body.app_key is not None:
        row.app_key = body.app_key
    if body.app_secret:
        secrets_value['app_secret'] = body.app_secret.get_secret_value()
    if body.access_token:
        secrets_value['access_token'] = body.access_token.get_secret_value()
        row.expires_at = now() + timedelta(seconds=body.token_expires_in)
        row.status = 'configured'
    if body.refresh_token:
        secrets_value['refresh_token'] = body.refresh_token.get_secret_value()
    if body.external_id:
        row.external_id = body.external_id
    row.credentials = encrypt(secrets_value) if secrets_value else ''
    row.options = {**(row.options or {}), 'redirect_uri': body.redirect_uri, 'scopes': body.scopes}
    row.interval_minutes = body.interval_minutes
    db.get(PlatformShop, row.shop_id).name = body.name
    row.version = (row.version or 0) + 1
    row.error = None
    audit(db, user, 'platform.config.save', {'id': row.id, 'provider': row.provider, 'mode': row.mode})
    db.commit()
    return view(row)


def store_token(row, value, previous):
    credentials = {**previous, 'access_token': value['access_token']}
    for key in ['refresh_token', 'scope']:
        if value.get(key):
            credentials[key] = value[key]
    if row.provider == 'douyin' and value.get('open_id'):
        incoming = str(value['open_id'])
        if not row.external_id.startswith('pending-') and row.external_id != incoming:
            raise PlatformError('authorization', '账号授权与当前连接不匹配，请新建连接。')
        row.external_id = incoming
    row.credentials = encrypt(credentials)
    row.expires_at = now() + timedelta(seconds=int(value['expires_in']))
    row.status, row.error = 'connected', None
    return credentials


def connect(db, user, row, code=None):
    missing = view(row)['missing']
    if row.provider == 'douyin':
        missing = [m for m in missing if not m.startswith('账号授权')]
    if missing:
        fail(422, '请先填写：' + '、'.join(missing))
    credentials = decrypt(row.credentials)
    client = Client(row, credentials)
    try:
        if row.provider == 'douyin' and credentials.get('access_token') and not code:
            # An actual read validates a supplied API credential; presence is not treated as success.
            next(iter(client.video_pages()), None)
            row.status, row.error = 'connected', None
        else:
            store_token(row, client.token(code), credentials)
        audit(db, user, 'platform.connect', {'id': row.id, 'mode': row.mode})
        db.commit()
    except PlatformError as exc:
        row.status, row.error = 'authorization_required' if exc.category == 'authorization' else 'error', str(exc)
        db.commit()
        fail(502, str(exc))
    finally:
        client.close()
    return view(row)


def enqueue(db, user, row, days=7, resources=None):
    if row.status != 'connected':
        fail(409, '请先完成连接检查与授权')
    requested = resources or RESOURCES[row.provider]
    if not requested or set(requested) - set(RESOURCES[row.provider]):
        fail(422, '同步资源与接入平台不匹配')
    previous = db.scalar(select(PlatformSync).where(PlatformSync.connection_id == row.id,
        PlatformSync.status.in_(['queued', 'running', 'retry_wait'])))
    if previous:
        return sync_view(previous)
    # Dependency order is fixed; a user cannot accidentally run refunds before orders in the same job.
    requested = [r for r in RESOURCES[row.provider] if r in requested]
    job = PlatformSync(connection_id=row.id, days=days, resources=requested, result={})
    db.add(job)
    audit(db, user, 'platform.sync.queue', {'connection_id': row.id, 'resources': requested, 'days': days})
    db.commit()
    return sync_view(job)


def put(db, row, kind, key, payload):
    if kind == 'products':
        previous = db.scalar(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == kind,
                                                    OpsRecord.external_id == key).with_for_update())
        if previous and previous.payload.get('operator_facts'):
            facts = previous.payload['operator_facts']
            payload = {**payload, **facts, 'operator_facts': facts,
                       'operator_facts_note': previous.payload.get('operator_facts_note', '')}
    if kind in ['orders', 'refunds']:
        previous = db.scalar(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == kind,
                                                    OpsRecord.external_id == key).with_for_update())
        if previous and previous.payload.get('updated_at') and payload.get('updated_at') and datetime.fromisoformat(previous.payload['updated_at']) > datetime.fromisoformat(payload['updated_at']):
            return
    statement = insert(OpsRecord).values(id=uid(), shop_id=row.shop_id, kind=kind, external_id=key,
        payload=payload, batch_id='api-' + row.id, updated_at=now())
    db.execute(statement.on_conflict_do_update(index_elements=['shop_id', 'kind', 'external_id'],
        set_={'payload': statement.excluded.payload, 'batch_id': statement.excluded.batch_id, 'updated_at': now()}))


def initialize_shop(db, row):
    put(db, row, 'shops', row.shop_id, {**mapping.base(row), 'shop_name': db.get(PlatformShop, row.shop_id).name,
        'platform': row.provider, 'account_id': row.external_id, 'data_as_of': now().isoformat()})
    existing = db.scalar(select(OpsRecord.id).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'sop_templates'))
    if not existing:
        for line in (FIXTURES / 'sop_templates.jsonl').read_text().splitlines():
            template = json.loads(line)
            put(db, row, 'sop_templates', template['template_id'], {**template, **mapping.base(row), 'source': 'local_sop_template'})


def aggregate_daily(db, row):
    orders_cap, refunds_cap = row.capabilities.get('orders', {}), row.capabilities.get('refunds', {})
    if orders_cap.get('status') != 'available':
        return
    start = datetime.fromisoformat(orders_cap['covered_from']).astimezone(ZONE).date()
    covered_at = datetime.fromisoformat(orders_cap['covered_from']).astimezone(ZONE)
    if covered_at.time() != datetime.min.time():
        start += timedelta(days=1)
    end = datetime.fromisoformat(orders_cap['covered_to']).astimezone(ZONE).date()
    orders = [r.payload for r in db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'orders'))]
    refunds = [r.payload for r in db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'refunds'))]
    by_date, ref_by_date = defaultdict(list), defaultdict(list)
    for order in orders:
        if order.get('paid_at'):
            by_date[datetime.fromisoformat(order['paid_at']).astimezone(ZONE).date()].append(order)
    for refund in refunds:
        if refund.get('completed_at'):
            ref_by_date[datetime.fromisoformat(refund['completed_at']).astimezone(ZONE).date()].append(refund)
    while start < end:
        paid_rows, refund_rows = by_date[start], ref_by_date[start]
        refund_complete = refunds_cap.get('status') == 'available' and datetime.fromisoformat(refunds_cap['covered_from']).astimezone(ZONE).date() <= start < datetime.fromisoformat(refunds_cap['covered_to']).astimezone(ZONE).date()
        paid = sum(r['paid_fen'] for r in paid_rows)
        refunded = sum(r['refund_fen'] for r in refund_rows) if refund_complete else None
        put(db, row, 'daily_reports', start.isoformat(), {**mapping.base(row), 'date': start.isoformat(),
            'paid_fen': paid, 'refund_fen': refunded, 'net_receipts_fen': paid - refunded if refunded is not None else None,
            'paid_parent_orders': len({r['parent_order_id'] for r in paid_rows}), 'paid_sub_orders': len(paid_rows),
            'visitors': None, 'paid_buyers': None, 'coverage_complete': refund_complete,
            'source': row.provider + '_order_aggregation', 'unavailable_metrics': ['visitors', 'paid_buyers'],
            'note': '根据已同步订单和退款明细按上海自然日汇总；不等同于平台罗盘的全部指标。'})
        start += timedelta(days=1)


def project_inventory(db, row):
    end = datetime.combine(now().astimezone(ZONE).date(), datetime.min.time(), ZONE)
    start = end - timedelta(days=7)
    coverage = row.capabilities.get('orders', {})
    complete = coverage.get('status') == 'available' and datetime.fromisoformat(coverage['covered_from']) <= start and datetime.fromisoformat(coverage['covered_to']) >= end
    units = defaultdict(int)
    if complete:
        for record in db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'orders')):
            order = record.payload
            if order.get('paid_at') and start <= datetime.fromisoformat(order['paid_at']) < end:
                units[order['sku_id']] += order['quantity']
    for record in db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'products')):
        product = record.payload
        put(db, row, 'inventory', product['sku_id'], {**mapping.base(row), 'sku_id': product['sku_id'],
            'available_stock': product.get('available_stock'), 'snapshot_at': product['extracted_at'],
            'sold_units_7d': units[product['sku_id']] if complete else None,
            'reserved_stock': None, 'inbound_stock': None, 'restock_lead_days': None})


def fetch_resource(db, row, client, resource, start, end):
    if resource == 'products':
        return [item for value in client.product_pages() for item in mapping.products(row, value)]
    if resource in ['orders', 'refunds']:
        results = []
        # Bound each API time window; the successful resource is committed atomically.
        cursor = start
        while cursor < end:
            stop = min(cursor + timedelta(days=1), end)
            iterator = client.order_pages(cursor, stop) if resource == 'orders' else client.refund_pages(cursor, stop)
            mapper = mapping.orders if resource == 'orders' else mapping.refunds
            results.extend(item for value in iterator for item in mapper(row, value))
            cursor = stop
        return results
    if resource == 'videos':
        return [item for value in client.video_pages() for item in mapping.videos(row, value)]
    rows = list(db.scalars(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'videos')))
    results = []
    if not rows:
        raise PlatformError('dependency', '请先同步视频列表，再同步视频统计。')
    for video in rows:
        if video.payload.get('connection_id') != row.id:
            continue
        vid = video.payload['video_id']
        published = video.payload.get('published_at')
        if published and datetime.fromisoformat(published) < end - timedelta(days=30):
            continue  # The official base-data API only covers recently created videos.
        data = client.douyin('/data/external/item/base/', {'open_id': row.external_id, 'item_id': vid})
        results.append(mapping.video_metric(row, vid, data))
    return results


def execute_job(db, job):
    row = db.scalar(select(PlatformConnection).where(PlatformConnection.id == job.connection_id).with_for_update())
    actor = scoped_actor(db, db.get(User, row.user_id), row.shop_id)
    if row.status != 'connected':
        raise PlatformError('authorization', '接入已暂停或需要重新授权。')
    credentials = decrypt(row.credentials)
    client = Client(row, credentials)
    results = dict(job.result or {})
    transient = False
    try:
        if not credentials.get('access_token') or not row.expires_at or row.expires_at <= now() + timedelta(minutes=5):
            credentials = store_token(row, client.token(), credentials)
            client.credentials = credentials
            db.commit()
        initialize_shop(db, row)
        db.commit()
        for resource in job.resources:
            if results.get(resource, {}).get('status') == 'succeeded':
                continue
            cap = (row.capabilities or {}).get(resource, {})
            end = now()
            start = datetime.combine((end.astimezone(ZONE) - timedelta(days=job.days)).date(), datetime.min.time(), ZONE)
            if cap.get('covered_to') and cap.get('covered_from') and start >= datetime.fromisoformat(cap['covered_from']):
                start = max(start, datetime.fromisoformat(cap['covered_to']) - timedelta(minutes=10))
            start = max(start, end - timedelta(days=90))
            try:
                items = fetch_resource(db, row, client, resource, start, end)
                # Native response records are normalized first; a schema failure cannot partially project a resource.
                unique = {(kind, key): payload for kind, key, payload in items}
                for (kind, key), payload in unique.items():
                    put(db, row, kind, key, payload)
                covered_from = min(start, datetime.fromisoformat(cap['covered_from'])) if cap.get('covered_from') else start
                row.capabilities = {**row.capabilities, resource: {'status': 'available',
                    'covered_from': covered_from.isoformat(), 'covered_to': end.isoformat()}}
                results[resource] = {'status': 'succeeded', 'records': len(unique), 'from': start.isoformat(), 'to': end.isoformat()}
                row.last_sync_at = end
                put(db, row, 'clock', 'reference', {'as_of': end.isoformat(), 'is_demo': row.mode == 'simulation'})
                job.result = dict(results)
                audit(db, actor, 'platform.sync.resource', {'connection_id': row.id, 'resource': resource, 'records': len(unique)})
                db.commit()
            except PlatformError as exc:
                db.rollback()
                row = db.get(PlatformConnection, row.id)
                transient = transient or exc.retryable
                results[resource] = {'status': 'failed', 'category': exc.category, 'error': str(exc)}
                row.capabilities = {**row.capabilities, resource: {**cap, 'status': 'blocked' if exc.category == 'authorization' else 'error', 'error': str(exc)}}
                job.result = dict(results)
                db.commit()
        if row.provider == 'doudian':
            aggregate_daily(db, row)
            project_inventory(db, row)
        failures = [r for r in results.values() if r.get('status') == 'failed']
        job.status = 'partial' if failures and len(failures) < len(job.resources) else 'failed' if failures else 'succeeded'
        job.error = '部分资源未同步，请查看资源结果。' if failures else None
        if transient and job.attempts < 3:
            job.status, job.retry_at = 'retry_wait', now() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
        job.finished_at = now() if job.status != 'retry_wait' else None
        row.next_sync_at = now() + timedelta(minutes=row.interval_minutes) if row.enabled else None
        db.commit()
    finally:
        client.close()


def tick():
    with SessionLocal() as db:
        schedules = list(db.scalars(select(PlatformConnection).where(PlatformConnection.enabled.is_(True),
            PlatformConnection.status == 'connected', PlatformConnection.next_sync_at <= now()).with_for_update(skip_locked=True)))
        for row in schedules:
            try:
                actor = scoped_actor(db, db.get(User, row.user_id), row.shop_id)
                enqueue(db, actor, row, 7)
                row.next_sync_at = now() + timedelta(minutes=row.interval_minutes)
                db.commit()
            except Exception:
                db.rollback()
                row.enabled, row.error = False, '自动同步已停止，请检查店铺授权和接入状态。'
                db.commit()
        job = db.scalar(select(PlatformSync).where((PlatformSync.status == 'queued') |
            ((PlatformSync.status == 'retry_wait') & (PlatformSync.retry_at <= now())))
            .order_by(PlatformSync.created_at).with_for_update(skip_locked=True).limit(1))
        if not job:
            return False
        job.status, job.attempts = 'running', job.attempts + 1
        db.commit()
        try:
            execute_job(db, job)
        except Exception as exc:
            db.rollback()
            logger.warning('Platform sync failed: job=%s category=%s', job.id,
                           exc.category if isinstance(exc, PlatformError) else type(exc).__name__)
            job.status, job.finished_at = 'failed', now()
            job.error = str(exc) if isinstance(exc, PlatformError) else '同步执行失败，请检查接入配置与任务结果后重试。'
            if isinstance(exc, PlatformError) and exc.retryable and job.attempts < 3:
                job.status, job.finished_at = 'retry_wait', None
                job.retry_at = now() + timedelta(seconds=30 * 2 ** (job.attempts - 1))
            if isinstance(exc, PlatformError) and exc.category == 'authorization':
                row = db.get(PlatformConnection, job.connection_id)
                row.status, row.enabled, row.error = 'authorization_required', False, str(exc)
            db.commit()
        return True


def recover():
    with SessionLocal() as db:
        for row in db.scalars(select(PlatformSync).where(PlatformSync.status == 'running')):
            row.status = 'queued'
        db.commit()


async def run():
    await asyncio.to_thread(recover)
    while True:
        try:
            worked = await asyncio.to_thread(tick)
        except Exception as exc:
            logger.warning('Platform worker tick failed: %s', type(exc).__name__)
            worked = False
        await asyncio.sleep(.5 if worked else 5)
