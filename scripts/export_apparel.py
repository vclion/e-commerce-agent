"""Export only the active synthetic clothing collection; never export user credentials."""
import json
from pathlib import Path
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from commerce.catalog import DATASET, MEN, WOMEN, SHOPS
from commerce.db import SessionLocal
from commerce.models import Competitor, Manifest, Order, Product

ROOT = Path(__file__).resolve().parents[1]


def main():
    folder = ROOT / 'data' / DATASET
    folder.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        products = list(db.scalars(select(Product).where(Product.id.like('MEN-%') | Product.id.like('WOM-%')).order_by(Product.id)))
        orders = list(db.scalars(select(Order).where(Order.dataset_version == DATASET).order_by(Order.id)))
        competitors = list(db.scalars(select(Competitor).where(Competitor.source.like('demo://apparel-v1/%')).order_by(Competitor.id)))
        counts = {}
        for name, rows, fields in [
            ('products', products, ['id', 'group_id', 'shop_id', 'title', 'category', 'brand', 'price_fen', 'cost_fen', 'stock', 'specs', 'stock_days', 'quality_samples', 'quality_issues']),
            ('orders', orders, ['id', 'shop_id', 'user_id', 'product_id', 'title', 'quantity', 'paid_fen', 'cost_fen', 'status', 'paid_at', 'delivered_at', 'refund_at', 'refund_fen', 'refund_reason', 'channel', 'specs_snapshot']),
            ('competitors', competitors, ['id', 'shop_id', 'product_id', 'title', 'category', 'specs', 'price_fen', 'currency', 'source', 'captured_at', 'is_demo'])]:
            path = folder / f'{name}.jsonl'
            with path.open('w', encoding='utf-8') as out:
                for row in rows:
                    data = {k: getattr(row, k) for k in fields}
                    data.update(dataset_version=DATASET, is_demo=True)
                    out.write(json.dumps(jsonable_encoder(data), ensure_ascii=False) + '\n')
            counts[name] = len(rows)
        styles = []
        for shop, rows in [('a', MEN), ('b', WOMEN)]:
            for i, row in enumerate(rows, 1):
                styles.append(dict(zip(['title', 'category', 'price_yuan', 'material', 'fit', 'season', 'occasion', 'colors'], row),
                                   group_id=f'{"MEN" if shop == "a" else "WOM"}-{i:03d}', shop_id=shop, sizes=SHOPS[shop]['sizes']))
        (folder / 'styles.json').write_text(json.dumps(styles, ensure_ascii=False, indent=2))
        manifest = dict(db.get(Manifest, DATASET).data)
        manifest['export_counts'] = counts
        manifest['files'] = ['styles.json', 'products.jsonl', 'orders.jsonl', 'competitors.jsonl']
        (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('Exported apparel-v1:', counts)


if __name__ == '__main__':
    main()
