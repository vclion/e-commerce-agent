"""Inspect reports and explicitly adapt source columns before existing validation."""
import base64
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from fastapi import HTTPException
from sqlalchemy import select

from .auth import scope_shop_ids
from .business import fail
from .models import OpsRecord
from .ops_data import FIXTURES, KEYS, allowed, read_text

MAX_ROWS = 25000
MAX_COLUMNS = 150
LABELS = {
    'shop_id': '店铺ID', 'sku_id': 'SKU编号', 'group_id': '款式ID', 'title': '商品标题',
    'sub_order_id': '子订单编号', 'parent_order_id': '父订单编号', 'buyer_id': '买家编号',
    'category': '品类', 'price_fen': '售价', 'cost_fen': '成本金额', 'size': '尺码', 'color': '颜色',
    'material': '面料', 'quantity': '数量', 'paid_fen': '支付金额', 'refund_fen': '退款金额',
    'net_receipts_fen': '净收款', 'requested_fen': '申请退款金额', 'date': '日期',
    'created_at': '下单时间', 'paid_at': '支付时间', 'updated_at': '更新时间',
    'shipped_at': '发货时间', 'delivered_at': '签收时间', 'promised_ship_at': '承诺发货时间',
    'requested_at': '申请时间', 'completed_at': '完成时间', 'extracted_at': '数据提取时间',
    'snapshot_at': '快照时间', 'status': '状态', 'channel': '渠道', 'video_id': '视频ID',
    'refund_id': '售后编号', 'reason': '退款原因', 'refund_type': '售后类型',
    'paid_parent_orders': '支付父订单数', 'paid_sub_orders': '支付子订单数',
    'visitors': '访客数', 'paid_buyers': '支付买家数', 'available_stock': '可售库存',
    'reserved_stock': '锁定库存', 'inbound_stock': '在途库存', 'sold_units_7d': '近7日销量',
    'restock_lead_days': '补货周期天数', 'sold_units': '销售件数', 'ads_spend_fen': '广告消耗',
    'source': '数据来源', 'account_id': '账号ID', 'published_at': '发布时间',
    'duration_seconds': '视频时长秒', 'shop_name': '店铺名称', 'platform': '平台',
    'likes': '点赞数', 'comments': '评论数', 'shares': '分享数', 'follows': '关注数',
    'attributed_sub_orders': '归因子订单数',
    'views': '播放量', 'product_clicks': '商品点击量', 'completed_views': '完整播放量',
    'attributed_paid_fen': '归因支付金额', 'metric_grain': '指标口径',
    'dataset_version': '数据来源标识', 'is_demo': '是否测试数据',
}
ALIASES = {
    'sku_id': ['商家SKU编码', '商家编码', 'SKU ID', 'sku编码'],
    'sub_order_id': ['子订单号', '子订单ID', '订单明细编号'],
    'parent_order_id': ['主订单号', '父订单号', '主订单编号'],
    'title': ['商品名称', '商品名', '标题'], 'paid_fen': ['实付金额', '订单实付金额', '成交金额'],
    'refund_fen': ['退款成功金额', '期间退款金额'], 'date': ['统计日期', '业务日期'],
    'quantity': ['商品数量', '购买数量'], 'status': ['订单状态', '售后状态'],
    'price_fen': ['销售价格', '商品售价'], 'material': ['面料成分'],
}
VALUE_MAPS = {
    ('orders', 'status'): {'待支付': 'pending_payment', '待发货': 'paid', '已支付': 'paid',
        '已发货': 'shipped', '已签收': 'delivered', '已完成': 'completed',
        '已取消': 'cancelled', '已关闭': 'cancelled', '已退款': 'refunded'},
    ('video_daily', 'metric_grain'): {'日增量': 'daily_increment', '累计快照': 'cumulative'},
    ('orders', 'channel'): {'直播': 'live', '商城': 'mall', '货架': 'mall', '短视频': 'short_video'},
    ('refunds', 'status'): {'已完成': 'completed', '退款成功': 'completed', '待处理': 'pending'},
    ('refunds', 'refund_type'): {'全额': 'full', '全额退款': 'full', '部分': 'partial', '部分退款': 'partial'},
}
ENUMS = {
    ('orders', 'status'): {'pending_payment', 'paid', 'shipped', 'delivered', 'completed', 'cancelled', 'refunded'},
    ('video_daily', 'metric_grain'): {'daily_increment', 'cumulative'},
    ('tasks', 'status'): {'pending', 'in_progress', 'blocked', 'review', 'completed', 'cancelled'},
    ('orders', 'channel'): {'live', 'mall', 'short_video'},
    ('refunds', 'status'): {'completed', 'pending'},
    ('refunds', 'refund_type'): {'full', 'partial'},
}


def fields(kind):
    if kind not in KEYS:
        fail(422, '不支持的报表类型')
    return json.loads((FIXTURES / 'schema.json').read_text())[kind]['fields']


def read_source(body):
    """Return source row numbers so Excel blanks never shift error locations."""
    fields(body.kind)
    if body.content_base64 is None:
        if body.filename.lower().endswith(('.xlsx', '.xls')):
            fail(422, 'Excel文件请按二进制上传；旧版.xls需另存为.xlsx')
        rows = read_text(body.kind, body.filename, body.text)
        headers = list(dict.fromkeys(k for r in rows if isinstance(r, dict) for k in r if k is not None))
        if len(headers) > MAX_COLUMNS:
            fail(422, f'报表最多{MAX_COLUMNS}列')
        return dict(headers=headers, rows=list(enumerate(rows, 2)), sheets=[], sheet=None)
    if not body.filename.lower().endswith('.xlsx'):
        fail(422, '二进制报表目前支持.xlsx；.xls请先另存为.xlsx')
    try:
        binary = base64.b64decode(body.content_base64, validate=True)
        if len(binary) > 16 * 1024 * 1024:
            fail(413, 'Excel文件最大16MB')
        with ZipFile(io.BytesIO(binary)) as archive:
            if sum(z.file_size for z in archive.infolist()) > 64 * 1024 * 1024:
                fail(413, 'Excel解压后超过64MB，请拆分报表')
        workbook = load_workbook(io.BytesIO(binary), read_only=True, data_only=False, keep_links=False)
        try:
            sheets = workbook.sheetnames
            sheet_name = body.sheet or sheets[0]
            if sheet_name not in sheets:
                fail(422, '工作表不存在，请重新选择')
            worksheet = workbook[sheet_name]
            # Ignore dimensions reported by exporters; stop at our own row/column bounds.
            worksheet.reset_dimensions()
            iterator = worksheet.iter_rows(max_col=MAX_COLUMNS + 1)
            cells = next(iterator, ())
            populated = [i for i, c in enumerate(cells) if c.value is not None]
            if not populated:
                fail(422, '第一行必须是字段表头')
            width = max(populated) + 1
            if width > MAX_COLUMNS:
                fail(422, f'报表最多{MAX_COLUMNS}列')
            if any(c.data_type == 'f' for c in cells[:width]):
                fail(422, '表头不能使用公式')
            headers = [str(c.value).strip() if c.value is not None else '' for c in cells[:width]]
            if any(not h for h in headers) or len(set(headers)) != len(headers):
                fail(422, 'Excel存在空白或重复表头')
            rows = []
            for number, cells in enumerate(iterator, 2):
                if number > MAX_ROWS + 1:
                    fail(422, f'Excel最多{MAX_ROWS}个数据行，请拆分报表')
                if any(c.value is not None for c in cells[width:]):
                    fail(422, f'第{number}行存在没有对应表头的数据')
                if not any(c.value is not None for c in cells[:width]):
                    continue
                value = {}
                for header, cell in zip(headers, cells[:width]):
                    if cell.data_type == 'f':
                        value[header] = {'error': '公式单元格请先粘贴为值'}
                    elif cell.data_type == 'e':
                        value[header] = {'error': 'Excel单元格包含错误值'}
                    else:
                        value[header] = cell.value
                rows.append((number, value))
            if not rows:
                fail(422, '工作表没有数据行')
            return dict(headers=headers, rows=rows, sheets=sheets, sheet=sheet_name)
        finally:
            workbook.close()
    except HTTPException:
        raise
    except Exception as exc:
        fail(422, f'无法读取Excel，请检查文件格式：{str(exc)[:100]}')


def header_key(value):
    return ''.join(str(value).lower().split()).replace('（', '(').replace('）', ')')


def suggest(kind, headers):
    result, units, ambiguous = {}, {}, []
    for key in fields(kind):
        names = [key, LABELS.get(key, key), *ALIASES.get(key, [])]
        matches = []
        for h in headers:
            if header_key(h) == header_key(key):
                matches = [h]
                break
            if any(header_key(h) == header_key(n + suffix) for n in names for suffix in ['', '(元)', '(分)']):
                matches.append(h)
        if len(matches) == 1:
            result[key] = matches[0]
            if key.endswith('_fen'):
                units[key] = 'fen' if header_key(matches[0]) == header_key(key) or '(分)' in header_key(matches[0]) else 'yuan'
        elif len(matches) > 1:
            ambiguous.append(LABELS.get(key, key))
    # One source column must not silently populate two semantic fields.
    for column in set(result.values()):
        targets = [k for k, v in result.items() if v == column]
        if len(targets) > 1:
            for key in targets:
                del result[key]
                ambiguous.append(LABELS.get(key, key))
    return dict(columns=result, money_units=units, defaults={}, value_maps={}, timezone='Asia/Shanghai'), ambiguous


def inspect(body):
    table = read_source(body)
    mapping, ambiguous = suggest(body.kind, table['headers'])
    return dict(headers=table['headers'], sheets=table['sheets'], sheet=table['sheet'],
        row_count=len(table['rows']), sample=[{'row': n, 'values': r} for n, r in table['rows'][:5]],
        mapping=mapping, ambiguous=ambiguous,
        fields=[dict(key=k, label=LABELS.get(k, k), **v, values=sorted(ENUMS.get((body.kind, k), []))) for k, v in fields(body.kind).items()],
        note='自动匹配仅为建议；请确认字段、金额单位和时间所属时区后校验。')


def validate_mapping(kind, mapping, headers=None):
    specs = fields(kind)
    for group in [mapping.columns, mapping.defaults, mapping.money_units, mapping.value_maps]:
        if any(key not in specs for key in group):
            fail(422, '映射中存在不属于该报表的字段')
    columns = list(mapping.columns.values())
    if len(columns) != len(set(columns)):
        fail(422, '一个源列不能同时映射到多个目标字段')
    if headers is not None and any(c not in headers for c in columns):
        fail(422, '映射引用了当前报表不存在的源列，请重新匹配')
    if any(not k.endswith('_fen') for k in mapping.money_units):
        fail(422, '金额单位只能设置在金额字段上')
    if any(len(str(v)) > 12000 for group in [mapping.defaults, mapping.columns] for v in group.values()):
        fail(422, '映射内容过长')


def convert_value(kind, key, value, spec, mapping):
    if isinstance(value, dict):
        raise ValueError(f'{LABELS.get(key, key)}：{value.get("error", "无效单元格")}')
    if value is None or value == '':
        return None
    label = LABELS.get(key, key)
    if key.endswith('_fen'):
        unit = mapping.money_units.get(key)
        if unit is None:
            raise ValueError(f'{label}请明确选择元或分')
        try:
            raw = str(value).strip()
            if unit == 'fen' and (raw.startswith(('¥', '￥')) or raw.endswith('元')):
                raise ValueError()
            raw = raw.lstrip('¥￥').removesuffix('元').strip()
            if len(raw) > 80 or not re.fullmatch(r'[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?', raw):
                raise ValueError()
            with localcontext() as decimal_context:
                decimal_context.prec = max(28, len(raw) + 4)
                amount = Decimal(raw.replace(',', '')) * (100 if unit == 'yuan' else 1)
            if not amount.is_finite() or amount != amount.to_integral_value():
                raise ValueError()
            return str(int(amount))
        except (InvalidOperation, ValueError, OverflowError):
            raise ValueError(f'{label}金额格式无效或精度不足，不能四舍五入')
    if key.endswith('_at') or key in ['data_as_of']:
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace('/', '-'))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(mapping.timezone))
            return parsed.isoformat()
        except (ValueError, TypeError):
            raise ValueError(f'{label}时间格式无效')
    if key == 'date':
        if isinstance(value, (date, datetime)):
            return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
        return str(value).strip().replace('/', '-')
    if spec['type'] == 'str':
        if key.endswith('_id') and isinstance(value, (int, float)):
            if abs(value) >= 10**15:
                raise ValueError(f'{label}为长数字，Excel可能已丢失精度，请用文本格式重新导出')
            if int(value) != value:
                raise ValueError(f'{label}不能是小数')
            value = str(int(value))
        elif isinstance(value, (date, datetime, bool)):
            raise ValueError(f'{label}需要文本')
        else:
            value = str(value).strip()
        builtins = VALUE_MAPS.get((kind, key), {})
        value = mapping.value_maps.get(key, {}).get(value, builtins.get(value, value))
        if (kind, key) in ENUMS and value not in ENUMS[kind, key]:
            raise ValueError(f'{label}未知值「{value}」，请设置值映射；可用值：' + '、'.join(sorted(ENUMS[kind, key])))
    if spec['type'] == 'bool':
        value = {'是': 'true', '否': 'false', '1': 'true', '0': 'false'}.get(str(value), str(value).lower())
    if spec['type'] == 'int' and isinstance(value, float) and value.is_integer():
        value = int(value)
    return value


def prepare(body, user):
    table = read_source(body)
    mapping = body.mapping
    if mapping is None:
        if body.content_base64 is not None:
            fail(422, 'Excel请先确认字段映射、金额单位和时区')
        return [(n, r, None) for n, r in table['rows']]
    validate_mapping(body.kind, mapping, table['headers'])
    if mapping.shop_id:
        allowed(user, mapping.shop_id)
    result = []
    for number, source in table['rows']:
        try:
            if not isinstance(source, dict) or None in source:
                raise ValueError('每行需为字段对象，且列数不能超过表头')
            row = {}
            for key, spec in fields(body.kind).items():
                column = mapping.columns.get(key)
                value = source.get(column) if column else mapping.defaults.get(key)
                if key == 'shop_id' and value in [None, '']:
                    value = mapping.shop_id
                if key == 'dataset_version' and value in [None, '']:
                    value = 'manual-import-v1'
                if key == 'is_demo' and value in [None, '']:
                    value = False
                row[key] = convert_value(body.kind, key, value, spec, mapping)
            if body.kind == 'sop_templates' and mapping.shop_id:
                row['shop_id'] = mapping.shop_id
            if mapping.shop_id and row.get('shop_id') != mapping.shop_id:
                raise ValueError('源数据店铺与所选导入店铺不一致')
            result.append((number, row, None))
        except (ValueError, TypeError, OverflowError) as exc:
            result.append((number, None, str(exc)))
    return result


def templates(db, user):
    return {'items': [r.payload for r in db.scalars(select(OpsRecord).where(
        OpsRecord.kind == 'import_mappings', OpsRecord.shop_id.in_(scope_shop_ids(user))))
        if r.payload.get('owner_id') == user.id]}


def save_template(db, user, body):
    validate_mapping(body.kind, body.mapping)
    if not body.mapping.shop_id:
        fail(422, '保存映射模板时请选择店铺')
    allowed(user, body.mapping.shop_id)
    key = hashlib.sha256(f'{user.id}|{body.kind}|{body.name}'.encode()).hexdigest()
    row = db.scalar(select(OpsRecord).where(OpsRecord.kind == 'import_mappings',
        OpsRecord.shop_id == body.mapping.shop_id, OpsRecord.external_id == key))
    payload = dict(name=body.name, kind=body.kind, owner_id=user.id, mapping=body.mapping.model_dump())
    if row:
        row.payload = payload
    else:
        db.add(OpsRecord(kind='import_mappings', shop_id=body.mapping.shop_id, external_id=key, payload=payload))
    db.commit()
    return payload
