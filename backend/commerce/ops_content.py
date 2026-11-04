"""Evidence checks for script text. Findings are editing aids, not certification."""
import re
from decimal import Decimal


MATERIALS = ('棉', '羊毛', '羊绒', '亚麻', '真丝', '桑蚕丝', '涤纶', '聚酯纤维', '锦纶', '氨纶', '粘纤', '莫代尔')
PERFORMANCE = ('抗菌', '抑菌', '防水', '防晒', '防紫外线', '不起球', '不缩水', '不掉色', '零甲醛', '免烫')
FIBRE_ALIASES = {'涤纶': '聚酯纤维', '粘胶纤维': '粘纤'}


def canonical_fibres(text):
    for alias, name in FIBRE_ALIASES.items():
        text = text.replace(alias, name)
    return text.replace('％', '%')


def review(product, title, segments):
    issues = []
    material = str(product.get('material') or '')
    canonical_material = canonical_fibres(material)

    def add(index, category, claim, evidence, suggestion):
        issues.append(dict(segment_index=index, category=category, claim=claim,
                           evidence=evidence, suggestion=suggestion,
                           locked=index >= 0 and bool(segments[index].get('locked'))))

    for index, text in [(-1, title), *((i, p['caption']) for i, p in enumerate(segments))]:
        text = canonical_fibres(text)
        # A fibre name alone does not support a claim of 100% composition.
        for fibre in MATERIALS:
            pattern = rf'(?:纯{fibre}|(?:100|百分之百)\s*%?\s*{fibre}|{fibre}\s*(?:100\s*%|含量\s*100\s*%))'
            for match in re.finditer(pattern, text):
                if not re.search(rf'(?:100\s*%\s*{fibre}|{fibre}\s*100\s*%|纯{fibre})', canonical_material):
                    add(index, 'material', match.group(), f'商品面料：{material or "未提供"}',
                        f'改用资料中的面料表述：{material}' if material else '删除无依据的面料成分声明')
        for fibre in MATERIALS:
            if fibre in text and fibre not in canonical_material and not any(i['segment_index'] == index and i['category'] == 'material' and fibre in i['claim'] for i in issues):
                add(index, 'material', fibre, f'商品面料：{material or "未提供"}', '核实该成分，或改用商品资料中的面料表述')
            pattern = rf'(\d+(?:\.\d+)?)\s*%\s*{fibre}|{fibre}\s*(?:含量|占比)?\s*(\d+(?:\.\d+)?)\s*%'
            expected = {Decimal(a or b) for a, b in re.findall(pattern, canonical_material)}
            if f'纯{fibre}' in canonical_material:
                expected.add(Decimal(100))
            for match in re.finditer(pattern, text):
                if Decimal(match.group(1) or match.group(2)) not in expected:
                    add(index, 'material', match.group(), f'商品面料：{material or "未提供"}', '按商品资料中的实际含量表述')
        for match in re.finditer(r'(?:[¥￥]\s*|(?:售价|价格|到手价|仅售|只要)\s*)(\d+(?:\.\d{1,2})?)\s*元?|(?<![\d.])(\d+(?:\.\d{1,2})?)\s*元', text):
            amount = Decimal(match.group(1) or match.group(2)) * 100
            if product.get('price_fen') is None or amount != product['price_fen']:
                price = f'{product["price_fen"] / 100:.2f}元' if product.get('price_fen') is not None else '未提供'
                add(index, 'price', match.group(), f'该SKU售价：{price}', f'按该SKU售价{price}表述，或删除价格')
        for match in re.finditer(r'限时|限量|满\s*\d+\s*减\s*\d+|[一二三四五六七八九\d.]+折|买[一二三\d]+送[一二三\d]+|优惠券|立减\s*\d+|包邮|赠品', text):
            add(index, 'promotion', match.group(), '当前商品资料未包含有效促销、赠品或运费承诺', '删除该承诺，或先补充并核实有效活动资料')
        for claim in PERFORMANCE:
            if claim in text:
                add(index, 'performance', claim, '当前结构化商品资料没有该性能的验证依据', '删除该性能承诺，改为已知商品事实')
        for match in re.finditer(r'(?<![A-Za-z0-9])(?:[2-5]XL|XXXL|XXL|XL|XS|S|M|L)码?(?![A-Za-z0-9])', text, re.I):
            size = match.group().upper().removesuffix('码')
            aliases = {'XXL': '2XL', 'XXXL': '3XL'}
            actual_size = str(product.get('size') or '').upper()
            if aliases.get(size, size) != aliases.get(actual_size, actual_size):
                add(index, 'size', match.group(), f'当前SKU尺码：{product.get("size") or "未提供"}', '核对具体SKU，其他尺码需要另查商品资料')
    unique = {(x['segment_index'], x['category'], x['claim']): x for x in issues}
    return dict(issues=list(unique.values()), issue_count=len(unique),
                checked_fields=['价格', '面料成分', '尺码', '促销与常见性能声明'],
                source_facts={key: product.get(key) for key in ['sku_id', 'title', 'price_fen', 'material', 'size']},
                note='依据当前SKU资料进行规则检查；未命中不代表所有表述都已验证，请核对其他描述。')
