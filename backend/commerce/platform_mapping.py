"""Conservative native response projections: no buyer PII and no invented metrics."""
import hashlib
from datetime import datetime, timezone
from .platform_client import PlatformError


def first(row, *names, default=None):
    return next((row[k] for k in names if row.get(k) is not None), default)


def required(row, *names):
    value = first(row, *names)
    if value is None or value == '':
        raise PlatformError('schema', '平台数据缺少必填字段：' + '/'.join(names))
    return value


def integer(value, field):
    try:
        valid = not isinstance(value, bool) and str(value).strip() == str(int(value)) and int(value) >= 0
    except (ValueError, TypeError, OverflowError):
        valid = False
    if not valid:
        raise PlatformError('schema', field + '必须为非负整数，未进行静默换算。')
    return int(value)


def iso(value):
    if value in (None, '', 0, '0'):
        return None
    try:
        if str(value).isdigit():
            return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
        parsed = datetime.fromisoformat(str(value))
        if not parsed.tzinfo:
            raise ValueError()
        return parsed.isoformat()
    except (ValueError, TypeError, OverflowError):
        raise PlatformError('schema', '平台时间字段不符合秒级时间戳或带时区ISO格式。')


def local_id(shop_id, remote):
    return 'p' + hashlib.sha256((shop_id + ':' + str(remote)).encode()).hexdigest()[:31]


def base(connection):
    return dict(shop_id=connection.shop_id, is_demo=connection.mode == 'simulation',
                source=connection.provider + ('_simulation' if connection.mode == 'simulation' else '_openapi'),
                connection_id=connection.id, extracted_at=datetime.now(timezone.utc).isoformat())


def products(connection, row):
    pid = str(required(row, 'product_id', 'id'))
    title = str(required(row, 'name', 'product_name', 'title'))[:160]
    attrs = row.get('product_format_new')
    material = attrs.get('面料') if isinstance(attrs, dict) else None
    skus = row.get('_skus')
    if not isinstance(skus, list):
        raise PlatformError('schema', '商品没有SKU列表。')
    for sku in skus:
        remote = str(required(sku, 'sku_id', 'id'))
        yield 'products', local_id(connection.shop_id, remote), {**base(connection),
            'sku_id': local_id(connection.shop_id, remote), 'external_sku_id': remote, 'external_product_id': pid,
            'group_id': local_id(connection.shop_id, pid), 'title': title,
            'category': str(first(row, 'category_name', 'third_cname', default='未提供品类'))[:100],
            'brand': first(row, 'brand_name', default=''), 'description': str(row.get('description') or '')[:5000],
            'size': first(sku, 'size'), 'color': first(sku, 'color'), 'material': material,
            'spec_labels': [sku.get('spec_detail_name' + str(i)) for i in range(1, 4) if sku.get('spec_detail_name' + str(i))],
            'price_fen': integer(required(sku, 'price', 'sale_price'), 'SKU售价'),
            'available_stock': integer(required(sku, 'stock_num', 'stock'), 'SKU库存'),
            'cost_fen': None, 'fit': None, 'season': None, 'occasion': None,
            'status': 'published' if str(row.get('status')) == '0' else 'offline'}


ORDER_STATES = {'1': 'pending', '2': 'paid', '3': 'shipped', '4': 'cancelled', '5': 'delivered'}


def orders(connection, row):
    parent = str(required(row, 'shop_order_id', 'p_id', 'order_id'))
    children = row.get('sku_order_list')
    if not isinstance(children, list):
        raise PlatformError('schema', '订单缺少SKU子订单列表，未用父订单金额替代。')
    for child in children:
        oid = str(required(child, 'order_id', 's_id'))
        status_code = str(first(child, 'order_status', default=row.get('order_status')))
        sku = str(required(child, 'sku_id'))
        yield 'orders', oid, {**base(connection), 'sub_order_id': oid, 'parent_order_id': parent,
            'sku_id': local_id(connection.shop_id, sku), 'external_sku_id': sku,
            'title': str(required(child, 'product_name', 'title'))[:160],
            'quantity': integer(required(child, 'item_num', 'quantity'), '购买数量'),
            'paid_fen': integer(required(child, 'pay_amount'), '子订单支付金额'),
            'created_at': iso(first(child, 'create_time', default=required(row, 'create_time'))),
            'paid_at': iso(first(child, 'pay_time', default=row.get('pay_time'))),
            'updated_at': iso(required(row, 'update_time')), 'shipped_at': iso(child.get('ship_time') or row.get('ship_time')),
            'delivered_at': iso(child.get('finish_time')), 'promised_ship_at': iso(child.get('exp_ship_time')),
            'refund_fen': first(child, 'refund_amount'), 'status': ORDER_STATES.get(status_code, 'unknown'),
            'platform_status': status_code, 'channel': 'unknown', 'video_id': None}


def refunds(connection, row):
    process = row.get('process_info') or row
    info, order = process.get('after_sale_info', process), row.get('order_info', {})
    if not first(order, 'sku_order_id', 'order_id') and len(order.get('sku_order_infos', [])) == 1:
        order = order['sku_order_infos'][0]
    aid = str(required(info, 'after_sale_id'))
    oid = str(required(order, 'sku_order_id', 'order_id'))
    code = str(required(info, 'after_sale_status'))
    # Completion requires an explicit refund timestamp; status alone cannot imply money movement.
    completed = iso(info.get('refund_time'))
    yield 'refunds', aid, {**base(connection), 'refund_id': aid, 'sub_order_id': oid,
        'requested_at': iso(required(info, 'apply_time')), 'completed_at': completed,
        'updated_at': iso(required(info, 'update_time')),
        'refund_fen': integer(required(info, 'refund_total_amount', 'refund_amount'), '退款金额'),
        'reason': str(first(info, 'reason', 'reason_text', default='平台未提供原因'))[:500],
        'status': 'completed' if completed else 'pending', 'platform_status': code}


def videos(connection, row):
    vid = str(required(row, 'item_id'))
    yield 'videos', vid, {**base(connection), 'video_id': vid, 'account_id': connection.external_id,
        'title': str(first(row, 'title', default='未提供标题'))[:500],
        'published_at': iso(required(row, 'create_time')), 'url': row.get('share_url'), 'group_id': None}


def video_metric(connection, vid, data):
    result = data.get('result')
    if not isinstance(result, dict):
        raise PlatformError('schema', '视频统计缺少result。')
    stamp = datetime.now(timezone.utc).isoformat()
    stats = {k: None for k in ('views', 'likes', 'comments', 'shares', 'follows', 'completed_views',
                               'product_clicks', 'attributed_sub_orders', 'attributed_paid_fen')}
    for field, native in [('views', 'total_play'), ('likes', 'total_like'), ('comments', 'total_comment'), ('shares', 'total_share')]:
        if result.get(native) is not None:
            stats[field] = integer(result[native], native)
    # This API documents a recent-video / 30-day statistic. Never mix it with lifetime or daily increments.
    return 'video_daily', vid + '|recent30', {**base(connection), **stats, 'video_id': vid,
        'date': stamp[:10], 'metric_grain': 'cumulative', 'metric_window': 'platform_recent_30_days',
        'extracted_at': stamp}
