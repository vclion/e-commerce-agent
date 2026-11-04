"""Reproducible original apparel data; legacy demo history is retained and archived."""
import random
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select
from .auth import password_hash
from .catalog import DATASET, MEN, WOMEN, SHOPS
from .config import settings
from .db import initialize_schema, SessionLocal
from .models import Action, Draft, Manifest, Membership, Order, Product, Competitor, User, now


def garment_specs(shop, index, row, color, size, grade):
    title, category, price, fabric, fit, season, occasion, colors = row
    specs = {'款号': f'{"SY" if shop == "a" else "ZZ"}{index:03d}', '性别': SHOPS[shop]['gender'],
             '颜色': color, '尺码': size, '面料': fabric, '版型': fit, '季节': season,
             '场景': occasion, '风格': SHOPS[shop]['positioning'],
             '洗护': '按演示洗标轻柔洗涤，深浅分开；无实物洗护验证',
             '测量说明': '平铺测量后换算围度，演示尺寸允许±2cm误差；均为成衣尺寸，不是人体尺寸'}
    male = shop == 'a'
    if category in ['长裤', '短裤', '半身裙']:
        specs.update({'腰围cm': (78 if male else 62) + grade * 4,
                      '臀围cm': (104 if male else 88) + grade * 4,
                      '裤长cm' if category != '半身裙' else '裙长cm':
                          (49 if category == '短裤' else 78 if category == '半身裙' else 100) + grade})
    else:
        ease = 8 if fit == '宽松' else 4 if fit == '直筒' else 0
        specs.update({'胸围cm': (104 if male else 84) + grade * 4 + ease,
                      '衣长cm': (70 if male else 55) + grade * 2,
                      '肩宽cm': (44 if male else 35) + grade + (2 if fit == '宽松' else 0)})
        if category == '连衣裙':
            specs.update({'衣长cm': 108 + grade * 2, '腰围cm': 64 + grade * 4})
    if category == '羽绒服':
        specs['充绒量g'] = (115 if index == 8 else 190) + grade * 10
    return specs


def seed():
    initialize_schema()
    with SessionLocal() as db:
        # Upgrade demo permissions even when the apparel dataset is already present.
        for role in ['operator']:
            for shop in SHOPS:
                account = db.get(User, f'{role}-{shop}')
                if account:
                    for sid in SHOPS:
                        if not db.get(Membership, (account.id, sid)):
                            db.add(Membership(user_id=account.id, shop_id=sid))
        db.commit()
        if db.get(Manifest, DATASET):
            return
        for p in db.scalars(select(Product).where(Product.id.like('SKU-%'))):
            p.status = 'archived'
        for action in db.scalars(select(Action).where(Action.status == 'prepared')):
            action.status = 'cancelled'
        for draft in db.scalars(select(Draft).where(Draft.kind == 'listing')):
            if draft.payload.get('category') not in {r[1] for r in MEN + WOMEN}:
                draft.status = 'archived'
        rng = random.Random(20261016)
        anchor = datetime.combine(now().astimezone(ZoneInfo('Asia/Shanghai')).date(), time(),
                                  ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)
        encoded = password_hash(settings.demo_password)
        for shop in SHOPS:
            for role, label in [('buyer', '测试买家'), ('operator', '运营')]:
                account_id = f'customer-{shop}' if role == 'buyer' else f'{role}-{shop}'
                user = db.get(User, account_id)
                if user:
                    user.display_name = f'{label} · {SHOPS[shop]["name"]}'
                else:
                    db.add(User(id=account_id, username=account_id, password_hash=encoded,
                                role=role, shop_id=shop, display_name=f'{label} · {SHOPS[shop]["name"]}'))
            for i in range(2, 11):
                if not db.get(User, f'customer-{shop}-{i}'):
                    db.add(User(id=f'customer-{shop}-{i}', username=f'customer-{shop}-{i}',
                                password_hash=encoded, role='buyer', shop_id=shop,
                                display_name=f'演示顾客 {shop.upper()}{i}'))
        db.flush()
        for role in ['operator']:
            for shop in SHOPS:
                for sid in SHOPS:
                    if not db.get(Membership, (f'{role}-{shop}', sid)):
                        db.add(Membership(user_id=f'{role}-{shop}', shop_id=sid))
        products = {}
        for shop, collection in [('a', MEN), ('b', WOMEN)]:
            products[shop] = []
            for index, row in enumerate(collection, 1):
                title, category, price, fabric, fit, season, occasion, colors = row
                group = f'{"MEN" if shop == "a" else "WOM"}-{index:03d}'
                cost = int(price * 100 * rng.uniform(.42, .66))
                for ci, color in enumerate(colors.split('/'), 1):
                    for grade, size in enumerate(SHOPS[shop]['sizes']):
                        stock = rng.randint(8, 45)
                        if index in [1, 3, 4] and grade in [1, 2]:
                            stock = 0 if ci == 1 and grade == 1 else rng.randint(1, 4)
                        if season == '夏季': stock = rng.randint(90, 180)
                        p = Product(id=f'{group}-{ci:02d}-{size}', group_id=group, shop_id=shop,
                                    title=f'{title} · {color} / {size}', category=category,
                                    brand=SHOPS[shop]['name'], price_fen=price * 100, cost_fen=cost, stock=stock,
                                    description=f'{title}，{fit}剪裁，适用于{season}的{occasion}穿搭。面料成分：{fabric}。'
                                        '成衣尺寸见规格；合成商品资料，不代表真实商品或实测性能。',
                                    specs=garment_specs(shop, index, row, color, size, grade), status='published',
                                    version=1, stock_days=27 if index % 9 else 20,
                                    quality_samples=rng.randint(30, 120), quality_issues=rng.randint(0, 3))
                        products[shop].append(p)
                        db.add(p)
        db.flush()
        count, refunds = 0, Counter()
        for day in range(90):
            for shop in SHOPS:
                n = (24 if shop == 'a' else 32) + rng.randint(-5, 6)
                n += (5 if shop == 'a' else 18) if day < 7 else 0
                if day % 7 in [4, 5]: n += 5
                ps = products[shop]
                weights = []
                for p in ps:
                    idx = int(p.group_id.split('-')[1])
                    weight = 4.5 if idx in [1, 3, 4] else 1.0
                    if p.specs['季节'] == '夏季': weight *= .12 if day < 35 else 1.4
                    if p.specs['季节'] in ['秋冬', '冬季']: weight *= 2.0 if day < 25 else .35
                    if p.specs['尺码'] in (['L', 'XL'] if shop == 'a' else ['S', 'M']): weight *= 1.8
                    weights.append(weight)
                for p in rng.choices(ps, weights=weights, k=n):
                    paid = anchor - timedelta(days=day, hours=rng.randint(1, 23), minutes=rng.randint(0, 59))
                    qty = 2 if rng.random() < .08 else 1
                    rate = (.065 if shop == 'a' else .14) + (.035 if p.specs['版型'] in ['收腰', '合体'] else 0)
                    refunded = day >= 6 and rng.random() < rate
                    reason = rng.choices(['尺码偏小', '尺码偏大', '版型不合适', '颜色与预期不符', '面料触感不符合预期', '做工问题'],
                                         weights=[32, 18, 23, 12, 11, 4])[0] if refunded else None
                    consumer = rng.randint(1, 10)
                    uid = f'customer-{shop}' + (f'-{consumer}' if consumer > 1 else '')
                    channel = rng.choices(['自然搜索', '社媒内容', '老客回访'],
                                          weights=([52, 18, 30] if shop == 'a' else [25, 55 if day < 7 else 35, 20]))[0]
                    db.add(Order(id=f'APP-ORD-{count:05d}', shop_id=shop, user_id=uid, product_id=p.id,
                                 title=p.title, specs_snapshot=dict(p.specs), dataset_version=DATASET,
                                 quantity=qty, paid_fen=p.price_fen * qty, cost_fen=p.cost_fen * qty,
                                 status='refunded' if refunded else 'delivered' if day >= 3 else 'shipping',
                                 paid_at=paid, delivered_at=paid + timedelta(days=2) if day >= 3 else None,
                                 refund_fen=p.price_fen * qty if refunded else 0,
                                 refund_at=paid + timedelta(days=rng.randint(4, 6)) if refunded else None,
                                 refund_reason=reason, channel=channel, version=1))
                    if refunded: refunds[shop] += 1
                    count += 1
        for shop in SHOPS:
            p = products[shop][2]
            db.add(Order(id=f'FIT-{shop.upper()}', shop_id=shop, user_id=f'customer-{shop}', product_id=p.id,
                         title=p.title, specs_snapshot=dict(p.specs), dataset_version=DATASET, quantity=2,
                         paid_fen=p.price_fen * 2, cost_fen=p.cost_fen * 2, status='delivered',
                         paid_at=anchor - timedelta(days=4), delivered_at=anchor - timedelta(days=2),
                         channel='自然搜索', version=1))
        for shop, ps in products.items():
            for p in ps:
                for i in range(3):
                    db.add(Competitor(id=f'C-{p.id}-{i}', shop_id=shop, product_id=p.id,
                                     title=f'虚构样本{i+1} · {p.category}', category=p.category, specs=dict(p.specs),
                                     price_fen=round(p.price_fen * [.94, 1.06, 1.15][i] / 100) * 100,
                                     currency='CNY', source=f'demo://apparel-v1/{p.id}/{i}',
                                     captured_at=anchor - timedelta(days=i), is_demo=True))
        db.add(Manifest(id=DATASET, data={'version': DATASET, 'seed': 20261016, 'anchor': anchor.isoformat(),
                   'shops': 2, 'shop_profiles': SHOPS, 'spus': len(MEN) + len(WOMEN), 'skus': 800,
                   'orders': count + 2, 'competitors': 2400, 'refund_orders_by_shop': dict(refunds),
                   'is_demo': True, 'provenance': '商品、尺码、经营事件及竞品均为本项目原创合成数据；没有采集真实店铺记录。',
                   'reference': 'https://github.com/cvdfoundation/fashionpedia',
                   'quality_and_supply': '合成聚合观察，非真实经营记录；库存为当前快照，不用于推导历史流水'}))
        db.commit()


if __name__ == '__main__':
    seed()
    print('Apparel dataset ready; existing accounts and legacy history preserved.')
