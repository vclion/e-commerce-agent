"""Business views over synchronized records, shared by UI and Agent tools."""
from types import SimpleNamespace
from sqlalchemy import select
from .models import OpsRecord, PlatformConnection, now, uid
from .auth import scope_shop_ids


def shop_ids(user):
    return [sid for sid in scope_shop_ids(user) if sid in getattr(user, 'shop_infos', {})]


def product_rows(db, user):
    for row in db.scalars(select(OpsRecord).where(OpsRecord.shop_id.in_(shop_ids(user)), OpsRecord.kind == 'products')):
        p = row.payload
        yield SimpleNamespace(id=p['sku_id'], shop_id=row.shop_id, group_id=p['group_id'], title=p['title'],
            category=p.get('category') or '未提供品类', brand=p.get('brand') or '', description=p.get('description') or '',
            price_fen=p['price_fen'], stock=p.get('available_stock'), cost_fen=p.get('cost_fen'),
            specs={key: p[field] for key, field in [('尺码', 'size'), ('颜色', 'color'), ('面料', 'material'),
                ('版型', 'fit'), ('季节', 'season'), ('场景', 'occasion')] if p.get(field) is not None},
            status=p.get('status', 'published'), version=1, is_demo=p.get('is_demo', False), source=p.get('source'),
            external_sku_id=p.get('external_sku_id'), stock_days=None, quality_samples=None, quality_issues=None)


def empty_records(db, user, resource):
    scoped = scope_shop_ids(user)
    if not scoped or any(sid not in getattr(user, 'shop_infos', {}) for sid in scoped):
        return None
    connections = list(db.scalars(select(PlatformConnection).where(PlatformConnection.shop_id.in_(scoped),
                                                                  PlatformConnection.provider == 'doudian')))
    covered = {row.shop_id: row.capabilities.get(resource, {}) for row in connections}
    available = all(covered.get(sid, {}).get('status') == 'available' for sid in scoped)
    return {'items': [], 'source': 'platform_sync', 'is_demo': any(user.shop_infos[sid]['mode'] == 'simulation' for sid in scoped),
            'data_available': available, 'coverage': covered,
            'notice': '已同步范围内没有记录。' if available else '尚未完成该资源的授权同步，空列表不代表无订单或无售后。'}


def analytics(db, user, days):
    from .ops_data import overview
    from .auth import shop_view
    result = overview(db, user, days)
    total = result['total']
    orders = total['paid_sub_orders']
    paid = total['paid_fen']
    return {**result, 'shop': shop_view(user), 'shop_id': user.shop_id, 'paid_fen': paid,
        'order_count': orders, 'aov_fen': round(paid / orders) if orders and paid is not None else None,
        'refund_fen': total['refund_fen'], 'net_receipts_fen': total['net_receipts_fen'],
        'previous_paid_fen': None, 'growth_percent': total['growth_percent'], 'channels': [],
        'size_sales': [], 'query_id': uid(), 'refund_reasons': [],
        'by_shop': [{**r, 'order_count': r['paid_sub_orders']} for r in result['by_shop']],
        'trend': [{**r, 'orders': None} for r in result['trend']]}
