import statistics
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .auth import require_role, scope_shop_ids, shop_view, shop_info
from .catalog import DATASET, SHOPS
from .models import AfterSale, Audit, Competitor, Order, Product, User, now, uid
from .schemas import Search
from .config import settings


def fail(status, message):
    raise HTTPException(status, message)


def audit(db, user, operation, details, request_id=None):
    row=Audit(id=uid(),user_id=user.id, operation=operation, details={**details, 'shop_id': user.shop_id}, request_id=request_id or uid())
    db.add(row)
    if settings.memory_enabled and operation.startswith(('ops.','platform.')):
        # Insert the durable receipt event in the very transaction committing the business change.
        from .memory.service import enqueue
        from .memory.policy import preferences
        from .auth import scope_fingerprint
        row.details={**row.details,'data_scope':scope_fingerprint(user)}
        enqueue(db,user.id,'receipt',row.id,preferences(db,user.id).epoch)


def product_view(p: Product, user: User):
    result = {k: getattr(p, k) for k in ['id', 'shop_id', 'group_id', 'title', 'category', 'brand', 'description',
                                       'price_fen', 'stock', 'specs', 'status', 'version']}
    require_role(user, 'operator')
    result['cost_fen'] = p.cost_fen
    result.update(shop_name=shop_info(user, p.shop_id)['name'], as_of=now().isoformat(), is_demo=getattr(p, 'is_demo', True))
    if getattr(p, 'source', None):
        result.update(source=p.source, external_sku_id=p.external_sku_id)
    return result


def scoped_product(db, user, product_id):
    p = db.scalar(select(Product).where(Product.id == product_id, Product.shop_id.in_(scope_shop_ids(user)),
                                        Product.status == 'published'))
    if not p:
        from .platform_data import product_rows
        matches = [p for p in product_rows(db, user) if (p.id == product_id or p.external_sku_id == product_id) and p.status == 'published']
        if len(matches) > 1:
            fail(422, '多个授权店铺存在相同SKU标识，请先选择单店')
        p = matches[0] if matches else None
        if not p:
            fail(404, '商品不存在或不可访问')
    return p


def search_products(db, user, query: Search):
    stmt = select(Product).where(Product.shop_id.in_(scope_shop_ids(user)), Product.status == 'published',
                                 Product.price_fen.between(query.min_price_fen, query.max_price_fen))
    if query.query:
        stmt = stmt.where((Product.title + ' ' + Product.description).contains(query.query, autoescape=True))
    if query.category:
        stmt = stmt.where(Product.category == query.category)
    if query.in_stock:
        stmt = stmt.where(Product.stock > 0)
    products = list(db.scalars(stmt.order_by(Product.price_fen, Product.id)))
    from .platform_data import product_rows
    platform_products = list(product_rows(db, user))
    categories = sorted(set(db.scalars(select(Product.category).where(Product.shop_id.in_(scope_shop_ids(user)),
        Product.status == 'published'))) | {p.category for p in platform_products if p.status == 'published'})
    products.extend(p for p in platform_products if p.status == 'published'
        and query.min_price_fen <= p.price_fen <= query.max_price_fen
        and (not query.in_stock or (p.stock is not None and p.stock > 0))
        and (not query.category or p.category == query.category)
        and (not query.query or query.query in p.title + ' ' + p.description or query.query == p.external_sku_id))
    products.sort(key=lambda p: (p.price_fen, p.id))
    for field, key in [('size', '尺码'), ('color', '颜色'), ('fit', '版型'), ('material', '面料'), ('season', '季节'), ('occasion', '场景')]:
        value = getattr(query, field)
        if value:
            products = [p for p in products if (value.casefold() == str(p.specs.get(key, '')).casefold()
                        if field in ['size', 'color', 'fit', 'occasion'] else value in str(p.specs.get(key, '')))]
    items = [product_view(p, user) for p in products]
    if query.group_styles:
        grouped = {}
        for p, item in zip(products, items):
            grouped.setdefault((p.shop_id, p.group_id), []).append(item)
        items = []
        for variants in grouped.values():
            sizes = shop_info(user, variants[0]['shop_id'])['sizes'] or sorted({str(v['specs']['尺码']) for v in variants if v['specs'].get('尺码')})
            variants.sort(key=lambda v: (v['specs'].get('颜色', ''), sizes.index(v['specs'].get('尺码')) if v['specs'].get('尺码') in sizes else 99))
            item = dict(next((v for v in variants if v['specs'].get('尺码') == 'M'), variants[0]))
            item.update(style_title=item['title'].split(' · ')[0],
                        available_sizes=[s for s in sizes if any(v['specs'].get('尺码') == s for v in variants)],
                        available_colors=list(dict.fromkeys(v['specs'].get('颜色') for v in variants)),
                        style_stock=sum(v['stock'] for v in variants), variants=variants)
            items.append(item)
    return {'items': items[query.offset:query.offset + query.limit], 'total': len(items),
            'categories': categories, 'unit': '款式' if query.group_styles else 'SKU', 'as_of': now().isoformat()}


def order_view(order):
    return {k: getattr(order, k) for k in ['id', 'shop_id', 'title', 'quantity', 'paid_fen', 'status', 'paid_at',
                                          'delivered_at', 'refund_fen', 'version', 'specs_snapshot', 'refund_reason']}


def get_orders(db, user):
    require_role(user, 'operator')
    from .ops_data import records
    imported = records(db, user, 'orders')
    if imported:
        refunds = {(r['shop_id'], r['sub_order_id']): r for r in records(db, user, 'refunds')}
        products = {(r['shop_id'], r['sku_id']): r for r in records(db, user, 'products')}
        result = []
        for row in sorted(imported, key=lambda r: r.get('paid_at') or r['created_at'], reverse=True)[:30]:
            p = products.get((row['shop_id'], row['sku_id']), {})
            result.append({**{k: row.get(k) for k in ['shop_id', 'title', 'quantity', 'paid_fen', 'status', 'created_at', 'paid_at', 'delivered_at', 'refund_fen']},
                'id': row['sub_order_id'], 'parent_order_id': row['parent_order_id'], 'version': 1,
                'specs_snapshot': {'颜色': p.get('color', ''), '尺码': p.get('size', '')},
                'refund_reason': refunds.get((row['shop_id'], row['sub_order_id']), {}).get('reason')})
        return {'items': result, 'source': 'imported_reports', 'is_demo': any(r.get('is_demo') for r in imported)}
    from .platform_data import empty_records
    pending = empty_records(db, user, 'orders')
    if pending is not None:
        return pending
    orders = db.scalars(select(Order).where(Order.shop_id.in_(scope_shop_ids(user)), Order.dataset_version == DATASET)
                        .order_by(Order.paid_at.desc()).limit(30))
    return {'items': [order_view(o) for o in orders], 'is_demo': True,
            'logistics_note': '物流为模拟状态，未连接真实承运商'}


def get_after_sales(db, user):
    require_role(user, 'operator')
    from .ops_data import records
    imported = records(db, user, 'refunds')
    if imported:
        return {'items': [dict(id=r['refund_id'], shop_id=r['shop_id'], order_id=r['sub_order_id'],
                quantity=None, reason=r['reason'], status=r['status'], created_at=r['requested_at'])
                for r in sorted(imported, key=lambda r: r['requested_at'], reverse=True)[:100]], 'source': 'imported_reports'}
    from .platform_data import empty_records
    pending = empty_records(db, user, 'refunds')
    if pending is not None:
        return pending
    return {'items': [{'id': s.id, 'shop_id': s.shop_id, 'order_id': s.order_id, 'quantity': s.quantity,
                       'reason': s.reason, 'status': s.status, 'created_at': s.created_at}
                      for s in db.scalars(select(AfterSale).where(AfterSale.shop_id.in_(scope_shop_ids(user)))
                                           .order_by(AfterSale.created_at.desc()))]}


def analytics(db, user, days=7):
    require_role(user, 'operator')
    from .platform_data import shop_ids, analytics as synced_analytics
    if shop_ids(user):
        return synced_analytics(db, user, days)
    if not 1 <= days <= 90:
        fail(422, '时间范围必须为 1～90 天')
    zone = ZoneInfo('Asia/Shanghai')
    end = datetime.combine(now().astimezone(zone).date(), time(), zone)
    start, previous_start = end - timedelta(days=days), end - timedelta(days=days * 2)
    records = list(db.scalars(select(Order).where(Order.shop_id.in_(scope_shop_ids(user)),
                                                 Order.dataset_version == DATASET, Order.paid_at >= previous_start, Order.paid_at < end)))
    current = [o for o in records if o.paid_at >= start]
    previous = [o for o in records if o.paid_at < start]
    refund_fen = db.scalar(select(func.coalesce(func.sum(Order.refund_fen), 0)).where(
        Order.shop_id.in_(scope_shop_ids(user)), Order.dataset_version == DATASET, Order.refund_at >= start, Order.refund_at < end))
    paid, prev = sum(o.paid_fen for o in current), sum(o.paid_fen for o in previous)
    trend = []
    for i in range(days):
        day = start + timedelta(days=i)
        subset = [o for o in current if day <= o.paid_at < day + timedelta(days=1)]
        trend.append({'date': day.date().isoformat(), 'paid_fen': sum(o.paid_fen for o in subset), 'orders': len(subset)})
    channels = [{'channel': c, 'paid_fen': sum(o.paid_fen for o in current if o.channel == c),
                 'orders': sum(1 for o in current if o.channel == c)}
                for c in sorted(set(o.channel for o in current))]
    refunded = list(db.scalars(select(Order).where(Order.shop_id.in_(scope_shop_ids(user)), Order.dataset_version == DATASET,
                                                  Order.refund_at >= start, Order.refund_at < end)))
    reasons = sorted(set(o.refund_reason or '未记录' for o in refunded))
    return {'shop': shop_view(user), 'shop_id': user.shop_id,
            'shops': [{'id': sid, **shop_info(user, sid)} for sid in scope_shop_ids(user)],
            'by_shop': [{'shop_id': sid, 'shop_name': shop_info(user, sid)['name'],
                         'paid_fen': sum(o.paid_fen for o in current if o.shop_id == sid),
                         'order_count': sum(o.shop_id == sid for o in current),
                         'refund_fen': sum(o.refund_fen for o in refunded if o.shop_id == sid)}
                        for sid in scope_shop_ids(user)],
            'refund_reasons': [{'reason': r, 'orders': sum((o.refund_reason or '未记录') == r for o in refunded),
                                'refund_fen': sum(o.refund_fen for o in refunded if (o.refund_reason or '未记录') == r)} for r in reasons],
            'size_sales': [{'size': s, 'units': sum(o.quantity for o in current if o.specs_snapshot.get('尺码') == s)} for s in shop_view(user)['sizes']],
            'period': {'start': start.isoformat(), 'end_exclusive': end.isoformat(), 'timezone': 'Asia/Shanghai'},
            'paid_fen': paid, 'order_count': len(current), 'aov_fen': round(paid / len(current)) if current else None,
            'refund_fen': refund_fen, 'net_receipts_fen': paid - refund_fen,
            'previous_paid_fen': prev, 'growth_percent': round((paid - prev) / prev * 100, 2) if prev else None,
            'trend': trend, 'channels': channels, 'query_id': uid(), 'as_of': now().isoformat(),
            'notes': ['净收款包含本期发生的历史订单退款，不等于利润。', '无流量数据，不展示转化率。'], 'is_demo': True}


def analytics_for_model(data):
    """Convert monetary units once in deterministic code, before model input."""
    def money(value):
        return f'{Decimal(value) / Decimal(100):,.2f} 元' if value is not None else '未知'
    return {'period': data['period'], '支付成交额': money(data['paid_fen']), '支付订单数': data['order_count'],
            '店铺': data['shop'], '尺码销量': data['size_sales'],
            '分店表现': [{'店铺': x['shop_name'], '支付成交额': money(x['paid_fen']),
                         '支付订单数': x['order_count'], '期间退款额': money(x['refund_fen'])} for x in data['by_shop']],
            '退款原因': [{'原因': x['reason'], '退款订单数': x['orders'], '退款金额': money(x['refund_fen'])} for x in data['refund_reasons']],
            '客单价': money(data['aov_fen']), '期间退款额': money(data['refund_fen']),
            '期间净收款': money(data['net_receipts_fen']), '上期支付成交额': money(data['previous_paid_fen']),
            '成交额环比': f'{data["growth_percent"]}%' if data['growth_percent'] is not None else '无可比基数',
            'daily_trend': [{'date': x['date'], '支付成交额': money(x['paid_fen']), '订单数': x['orders']} for x in data['trend']],
            'channels': [{'channel': x['channel'], '支付成交额': money(x['paid_fen']), '订单数': x['orders']} for x in data['channels']],
            'query_id': data['query_id'], 'as_of': data['as_of'], '口径说明': data['notes'], 'is_demo': True,
            '金额说明': '全部金额已由业务服务从整数分精确转换为人民币元；直接引用，不再换算。'}


def competitor_view(c):
    return {'id': c.id, 'shop_id': c.shop_id, 'product_id': c.product_id, 'title': c.title, 'category': c.category,
            'specs': c.specs, 'price_fen': c.price_fen, 'currency': c.currency, 'source': c.source,
            'captured_at': c.captured_at, 'is_demo': c.is_demo}


def competitors(db, user, product_id=None):
    require_role(user, 'operator')
    stmt = select(Competitor).where(Competitor.shop_id.in_(scope_shop_ids(user)),
        (Competitor.product_id.is_(None)) | Competitor.product_id.in_(select(Product.id).where(Product.status == 'published')))
    if product_id:
        scoped_product(db, user, product_id)
        stmt = stmt.where(Competitor.product_id == product_id)
    return {'items': [competitor_view(c) for c in db.scalars(stmt.order_by(Competitor.captured_at.desc()).limit(100))]}


def selection(db, user, category='', min_margin=.2, max_price_fen=10000000):
    require_role(user, 'operator')
    if not 0 <= min_margin < 1 or max_price_fen <= 0:
        fail(422, '毛利门槛必须为 0～1，最高价格必须大于零')
    ps = list(db.scalars(select(Product).where(Product.shop_id.in_(scope_shop_ids(user)),
                                              Product.status == 'published', Product.stock > 0, Product.price_fen <= max_price_fen)))
    if category:
        ps = [p for p in ps if p.category == category]
    since = now() - timedelta(days=30)
    counts = dict(db.execute(select(Order.product_id, func.sum(Order.quantity)).where(
        Order.shop_id.in_(scope_shop_ids(user)), Order.dataset_version == DATASET, Order.paid_at >= since).group_by(Order.product_id)).all())
    cs = list(db.scalars(select(Competitor).where(Competitor.shop_id.in_(scope_shop_ids(user)),
                                                 Competitor.captured_at >= now() - timedelta(days=7))))
    ranked, incomplete = [], []
    for p in ps:
        margin = (p.price_fen - p.cost_fen) / p.price_fen
        if margin < min_margin:
            continue
        # Compare observed demand within category and a 100-CNY price band.
        peers = [counts.get(x.id, 0) for x in ps if x.category == p.category
                 and x.price_fen // 10000 == p.price_fen // 10000]
        count = counts.get(p.id, 0)
        demand = 50 if len(set(peers)) <= 1 else 100 * sum(v < count for v in peers) / max(len(peers)-1, 1)
        snapshots = [c for c in cs if c.product_id == p.id and c.currency == 'CNY' and c.specs == p.specs]
        missing = []
        if not count:
            missing.append('近30天需求样本')
        if len(snapshots) < 3:
            missing.append('至少3条7天内同规格竞品')
        if p.stock_days is None:
            missing.append('30天供货观察')
        if not p.quality_samples or p.quality_samples < 20:
            missing.append('至少20单质量观察')
        row = {'product': product_view(p, user), 'margin_percent': round(margin * 100, 2),
               'observed_units': count, 'missing': missing, 'evidence_ids': [c.id for c in snapshots],
               'score_version': 'demo-v1', 'is_demo': True}
        if missing:
            row['score'] = None
            incomplete.append(row)
            continue
        median = statistics.median(c.price_fen for c in snapshots)
        parts = {'demand': min(demand, 100), 'margin': margin * 100,
                 'competition': max(0, min(100, (median-p.price_fen)/median*100 + 50)),
                 'supply': p.stock_days / 30 * 100,
                 'quality': (1-p.quality_issues/p.quality_samples)*100}
        weights = dict(demand=.30, margin=.25, competition=.20, supply=.15, quality=.10)
        row.update(parts={k: round(v, 2) for k, v in parts.items()},
                   score=round(sum(parts[k]*weights[k] for k in weights), 2))
        ranked.append(row)
    ranked.sort(key=lambda x: (-x['score'], -x['parts']['demand'], -x['parts']['margin'], x['product']['id']))
    # One representative SKU per style prevents size/color clones occupying every recommendation.
    seen = set()
    unique = []
    for row in ranked:
        if row['product']['group_id'] not in seen:
            unique.append(row)
            seen.add(row['product']['group_id'])
    ranked = unique
    from .platform_data import product_rows
    for p in product_rows(db, user):
        if p.status == 'published' and (not category or p.category == category) and p.price_fen <= max_price_fen:
            margin = (p.price_fen - p.cost_fen) / p.price_fen if p.cost_fen is not None and p.price_fen else None
            if margin is not None and margin < min_margin:
                continue
            incomplete.append({'product': product_view(p, user), 'margin_percent': round(margin*100, 2) if margin is not None else None, 'observed_units': None,
                'missing': ([] if margin is not None else ['采购成本与毛利']) + ['同规格竞品观察', '供货与质量观察'], 'evidence_ids': [],
                'score': None, 'score_version': 'pending_observations', 'is_demo': p.is_demo})
            ps.append(p)
    return {'items': ranked[:5], 'incomplete': incomplete[:10], 'evaluated': len(ps),
            'as_of': now().isoformat(), 'notes': ['规则评分，不是销量预测。',
                '平台商品缺少供货、质量或竞品观察时不打分；演示商品观察为合成数据。']}
