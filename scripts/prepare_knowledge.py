"""Create and import 30 clearly labeled synthetic knowledge documents.

Preserve the configured index technique, including an upgraded hybrid index.
"""
import json
from pathlib import Path
import httpx
from .dify_console import DifyConsole, ROOT
from commerce.knowledge_retrieval import retrieval_model

PUBLIC = [
    ('运营工作范围', '衣序仅服务运营人员，统一管理授权的抖店商品电商店铺。工作围绕经营分析、商品事实、选品与营销内容，不提供消费者导购工作台或商家上架流程。'),
    ('售后数据复核', '运营读取授权店铺的售后与退款记录，检查原因、金额和状态。待处理申请不等于已退款，不能根据分析结果声称已经提交申请或完成退款。'),
    ('运营内容与执行', '运营可保存营销文案和经营报告草稿。生成文字不等于已发布视频、已上架商品或已完成业务任务；不存在的工具能力必须明确说明。'),
    ('店铺订单与物流', '运营可以查询授权店铺的订单，不能查询未授权店铺。当前物流为模拟状态，没有连接真实承运商，不提供有保证的到货日期。'),
    ('订单金额', '订单展示购买时保存的金额快照，当前商品调价不会改写历史订单。用户界面以人民币元展示，业务接口的金额字段以分计。'),
    ('服装选购条件', '推荐前了解预算、穿搭场景、品类、尺码偏好、面料及版型硬条件；店内只推荐当前有库存且符合条件的具体SKU。'),
    ('成衣尺寸与人体尺寸', '尺码表为成衣胸围、腰围、臀围及衣长等厘米数，不等于人体围度；应与用户现有合身衣物平铺测量结果对照，不能直接把人体胸围当作衣服胸围。'),
    ('尺码选择方法', '同为M码的不同款式也可能尺寸不同。只有身高体重时先询问关键围度、常穿衣服尺寸和穿着偏好，给出参考范围与不确定性，不能保证某码一定合身。'),
    ('男装版型与搭配', '山序男装偏都市通勤和户外休闲；合体衬衫搭直筒或锥形长裤，宽松夹克可叠穿。搭配是建议，面料、尺码和库存仍以具体SKU事实为准。'),
    ('女装版型与搭配', '知织女装偏简约通勤和轻法式；收腰连衣裙要同时核对胸腰围，半身裙关注腰臀围和裙长。不得使用显瘦若干斤等无法验证的效果承诺。'),
    ('服装面料成分', '棉、羊毛、亚麻、粘纤、锦纶和聚酯纤维只是成分事实。含40%羊毛不能表述成纯羊毛，混纺含棉不能表述成纯棉；不从面料名称直接推断抗菌或防水性能。'),
    ('服装颜色与色差', '颜色名来自具体SKU。页面为本项目绘制的服装款式示意图，不是实拍照片；屏幕颜色不作为真实色差承诺。'),
    ('服装季节与场景', '春秋、秋冬、冬季和四季是演示穿搭标签，不代表保证适用温度。没有实测依据时不宣称零下保暖温度、防风等级或户外防护性能。'),
    ('服装洗护资料', '按商品现有洗护说明回答，资料缺失则明确未知。页面演示洗护描述未经实物验证，不能替代真实服装洗标，不编造可机洗、可烘干或不缩水承诺。'),
    ('尺码与颜色库存', '每个颜色和尺码拥有独立SKU和库存。同款可选库存是筛选后SKU合计，不代表某一个尺码有这么多件；缺货尺码不推荐为可买。'),
    ('商品库存', '库存属于变化中的业务数据，必须调用商品接口查询并显示查询时间。知识文档和历史对话里的库存不能替代当前库存。'),
    ('商品价格', '商品推荐卡片以商品接口返回的当前报价为准，字段price_fen单位为分。没有有效活动依据时，不承诺优惠券、满减、到手价或最低价。'),
    ('商品对比', '一次最多比较3件商品。优先比较价格、面料成分、版型、具体尺码、成衣围度、衣长和库存。缺失参数显示未知，不能填造。'),
    ('资料不完整时如何推荐', '预算内没有满足硬条件的商品时，明确说明没有匹配结果，再询问是否调整预算或条件。不能为了给出推荐自动放宽用户的硬条件。'),
    ('演示数据说明', 'douyin-ops-v1测试数据可导入当前运营工作台，包含日报、订单、视频、任务和素材。新运营分析优先使用ops_overview等工具读取已导入记录，原始apparel-v1与导入数据不混算。合成数据明确标注；素材占位动画不是真实商品影像。'),
]
INTERNAL = [
    ('品牌文案语气', '山序男装采用克制、利落的语气，知织女装采用柔和、简洁的语气。描述商品怎样满足已知用途，避免空泛夸张和缺乏证据的性能承诺。'),
    ('商品事实与内容依据', '运营生成文案前查询对应SKU的标题、品类、价格、库存、面料和规格。缺失字段明确列出，不得凭空填写尺寸、认证或成本，不把混纺写成纯天然成分。'),
    ('草稿与执行边界', '运营可在工作台保存营销文案与经营报告。商品上架、售后提交和外部平台发布未开放；保存草稿不能表述为执行成功。'),
    ('营销活动依据', '生成文案前查商品事实。演示版没有活动接口时，只能生成不含促销承诺的文案；不能添加限时折扣、赠品、全网最低等未经支持的信息。'),
    ('支付成交额口径', '支付成交额按支付时间归属，汇总同一窗口内成功支付订单行金额。支付订单数采用去重订单数，客单价为成交额除以订单数，分母为零时未知。'),
    ('退款与净收款口径', '期间退款额按退款发生时间统计，可以包含历史订单退款。期间净收款等于期间成交额减期间退款额，不能将其称作利润或同一批订单的净销售额。'),
    ('时间窗口与环比', '默认分析最近7个完整自然日，上海时区。环比比较同长度上一窗口，上一窗口为零时标为无可比基数，不展示无限增长百分比。'),
    ('选品评分方法', '演示规则权重为需求30%、毛利25%、价格机会20%、供货15%、质量10%。规则服务计算分数，模型只解释。缺失需求、成本或观察样本的候选单独展示，不与完整候选混排。'),
    ('竞品可比性', '只有币种、品类、面料成分、版型、尺码及优惠条件可对齐的竞品快照才能比较价格。价格机会评分需要至少3条7天内的同规格快照。合成快照和导入快照明确区别，不虚构外部销量。'),
    ('经营诊断的证据', '经营报告先列数据事实，再列可能原因和验证动作。未建立因果证据时不能把相关性写成确定原因。没有流量数据不计算转化率，没有成本和费用数据不声称净利润。'),
]


def main():
    console = DifyConsole()
    lock_path = ROOT / 'dify/knowledge.lock.json'
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    lock = {k: v for k, v in lock.items() if v.get('domain') == 'apparel-v1'}
    all_datasets = console.request('GET', '/datasets', params={'page': 1, 'limit': 100})['data']
    current_ids = {x['id'] for x in all_datasets}
    for scope, label in [('public', '公开使用与售后资料'), ('internal', '内部运营方法资料')]:
        name = '衣序服装 v1 · ' + label
        if scope not in lock or lock[scope]['id'] not in current_ids:
            record = next((x for x in all_datasets if x['name'] == name), None)
            record = record or console.request('POST', '/datasets', json={'name': name,
                'permission': 'only_me', 'indexing_technique': 'economy', 'description': '本项目自编的合成演示资料，不含真实店铺数据。'})
            lock[scope] = {'id': record['id'], 'name': name,
                           'indexing_technique': record.get('indexing_technique') or 'economy', 'domain': 'apparel-v1',
                           **{key: record[key] for key in ('embedding_model', 'embedding_model_provider') if record.get(key)}}
    key_path = ROOT / '.runtime/knowledge-key.json'
    if not key_path.exists():
        key_path.write_text(json.dumps(console.request('POST', '/datasets/api-keys', json={})))
        key_path.chmod(0o600)
    key = json.loads(key_path.read_text())['token']
    docs_path = ROOT / 'knowledge/manifest.json'
    manifest = json.loads(docs_path.read_text()) if docs_path.exists() else []
    valid_datasets = {x['id'] for x in lock.values()}
    manifest = [x for x in manifest if x['dataset_id'] in valid_datasets]
    imported = {(x['scope'], x['file']): x for x in manifest}
    with httpx.Client(base_url='http://127.0.0.1:8088/v1', headers={'Authorization': 'Bearer ' + key}, timeout=30) as client:
        for scope, topics in [('public', PUBLIC), ('internal', INTERNAL)]:
            folder = ROOT / 'knowledge' / scope
            folder.mkdir(parents=True, exist_ok=True)
            for i, (title, content) in enumerate(topics, 1):
                name = f'apparel-{scope}-{i:02d}-{title}-v1'
                body = f'# {title}\n\n版本：v2；适用范围：{scope}；性质：合成演示资料；编写日期：2026-10-16。\n\n{content}\n'
                file = folder / f'{i:02d}.md'
                previous = file.read_text() if file.exists() else None
                file.write_text(body)
                old = imported.get((scope, str(file.relative_to(ROOT))))
                if old and previous == body and old.get('title') == title and old.get('version') == 'v2':
                    continue
                endpoint = (f'/datasets/{lock[scope]["id"]}/documents/{old["document_id"]}/update-by-text' if old else
                            f'/datasets/{lock[scope]["id"]}/document/create-by-text')
                index = lock[scope]
                response = client.post(endpoint, json={
                    'name': name, 'text': body, 'indexing_technique': index['indexing_technique'],
                    **{key: index[key] for key in ('embedding_model', 'embedding_model_provider') if index.get(key)},
                    'retrieval_model': retrieval_model(index), 'doc_form': 'text_model',
                    'doc_language': 'Chinese', 'process_rule': {'mode': 'custom', 'rules': {
                        'pre_processing_rules': [{'id': 'remove_extra_spaces', 'enabled': True},
                                                 {'id': 'remove_urls_emails', 'enabled': False}],
                        'segmentation': {'separator': '\n\n', 'max_tokens': 500}}}})
                if response.status_code != 200:
                    raise RuntimeError(f'Knowledge import HTTP {response.status_code}: ' + response.text[:300])
                record = response.json()['document']
                entry = {'name': name, 'title': title, 'scope': scope, 'version': 'v2',
                                 'dataset_id': lock[scope]['id'], 'document_id': record['id'],
                                 'file': str(file.relative_to(ROOT))}
                if old:
                    manifest[manifest.index(old)] = entry
                else:
                    manifest.append(entry)
                docs_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
                print('Imported:', name)
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2))
    env = ROOT / '.env'
    values = {'DIFY_KNOWLEDGE_KEY': key, 'DIFY_PUBLIC_DATASET': lock['public']['id'],
              'DIFY_INTERNAL_DATASET': lock['internal']['id']}
    lines = [line for line in env.read_text().splitlines() if line.split('=', 1)[0] not in values]
    env.write_text('\n'.join(lines + [k + '=' + v for k, v in values.items()]) + '\n')
    env.chmod(0o600)
    print(f'{len(manifest)} documents ready; index techniques:', {scope: row['indexing_technique'] for scope, row in lock.items()})


if __name__ == '__main__':
    main()
