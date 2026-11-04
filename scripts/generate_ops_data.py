"""Reproducible, isolated Douyin Shop operations fixtures. No DB or credentials needed."""
import argparse
import csv
import hashlib
import json
import random
import shutil
import subprocess
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = 'douyin-ops-v1'
DEFAULT_OUTPUT = ROOT / 'data' / DATASET
ZONE = timezone(timedelta(hours=8))
SEED = 20261016


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def stamp(value):
    return value.isoformat(timespec='seconds') if value else None


def parse(value):
    return datetime.fromisoformat(value) if value else None


def write_table(folder, name, rows):
    """JSONL is canonical; UTF-8 BOM CSV is a machine-import/test intermediate."""
    for row in rows:
        row.update(dataset_version=DATASET, is_demo=True)
    with (folder / f'{name}.jsonl').open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    with (folder / f'{name}.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build(as_of_date=date(2026, 10, 16)):
    rng = random.Random(SEED)
    as_of = datetime.combine(as_of_date, time(10), ZONE)
    first = as_of_date - timedelta(days=60)
    dates = [first + timedelta(days=i) for i in range(60)]
    shops = [dict(shop_id=sid, shop_name=name, platform='douyin_shop',
                  account_id=f'TEST-ACCOUNT-{sid.upper()}', business='商品销售与快递发货',
                  data_as_of=stamp(as_of))
             for sid, name in [('a', '山序男装'), ('b', '知织女装')]]
    products = [json.loads(line) for line in (ROOT / 'data/apparel-v1/products.jsonl').read_text(encoding='utf-8').splitlines()]
    skus = [dict(sku_id=p['id'], group_id=p['group_id'], shop_id=p['shop_id'], title=p['title'],
                 category=p['category'], price_fen=p['price_fen'], cost_fen=p['cost_fen'],
                 size=p['specs']['尺码'], color=p['specs']['颜色'], material=p['specs']['面料'])
            for p in products]
    by_shop = {sid: [p for p in skus if p['shop_id'] == sid] for sid in ['a', 'b']}
    heroes = {sid: next(p for p in by_shop[sid] if p['group_id'] == ('MEN-001' if sid == 'a' else 'WOM-001') and p['size'] == 'M') for sid in by_shop}
    videos = [dict(video_id=f'TEST-VIDEO-{sid}-{i:02}', shop_id=sid,
                   account_id=f'TEST-ACCOUNT-{sid.upper()}',
                   title=('衬衫通勤搭配' if sid == 'a' else '连衣裙尺码选择') + f' 第{i + 1}期',
                   published_at=stamp(datetime.combine(first + timedelta(days=i * 2), time(7), ZONE)),
                   duration_seconds=18 + i % 4 * 3, group_id=heroes[sid]['group_id'],
                   source=f'demo://{DATASET}/videos/{sid}/{i:02}') for sid in ['a', 'b'] for i in range(30)]
    orders, refunds = [], []
    serial = 0
    for day in dates:
        for sid in ['a', 'b']:
            recent = day >= as_of_date - timedelta(days=7)
            count = (42 if recent else 88) if sid == 'a' else (116 if recent else 102)
            count += day.weekday() % 3 * 2
            eligible = [v for v in videos if v['shop_id'] == sid and parse(v['published_at']).date() <= day and (day - parse(v['published_at']).date()).days < 14]
            for i in range(count + 2):
                serial += 1
                cancelled = i >= count
                parent_index = i // 2
                paid_at = datetime.combine(day, time(8 + (parent_index * 7) % 15, parent_index * 13 % 60), ZONE)
                product = heroes[sid] if i % 5 == 0 else rng.choice(by_shop[sid])
                qty = 2 if i % 13 == 0 else 1
                amount = product['price_fen'] * qty - (1000 if i % 7 == 0 else 0)
                source = ['short_video', 'mall', 'live'][i % 3]
                vid = eligible[i % len(eligible)]['video_id'] if source == 'short_video' else None
                shipped_at = paid_at + timedelta(hours=18 + i % 25)
                if sid == 'a' and recent and i < 6:
                    shipped_at = None
                if shipped_at and shipped_at > as_of:
                    shipped_at = None
                delivered_at = shipped_at + timedelta(days=2) if shipped_at else None
                if delivered_at and delivered_at > as_of:
                    delivered_at = None
                amount = 0 if cancelled else amount
                status = 'cancelled' if cancelled else 'delivered' if delivered_at else 'shipped' if shipped_at else 'paid'
                if cancelled:
                    shipped_at = delivered_at = None
                order = dict(sub_order_id=f'TEST-ORDER-{serial:07}',
                             parent_order_id=f'TEST-PARENT-{sid}-{day:%Y%m%d}-{i // 2:04}',
                             shop_id=sid, buyer_id=f'TEST-BUYER-{sid}-{i // 2:04}',
                             sku_id=product['sku_id'], group_id=product['group_id'], title=product['title'],
                             quantity=qty, paid_fen=amount, cost_fen=product['cost_fen'] * qty,
                             created_at=stamp(paid_at - timedelta(minutes=5)),
                             paid_at=None if cancelled else stamp(paid_at),
                             promised_ship_at=None if cancelled else stamp(paid_at + timedelta(hours=48)),
                             shipped_at=stamp(shipped_at), delivered_at=stamp(delivered_at),
                             status=status, channel=source, video_id=vid if not cancelled else None,
                             refund_fen=0, updated_at=stamp(as_of))
                # Refund selection is fixed; the B hero has a recent size-related spike.
                wants_refund = not cancelled and (i % 29 == 0 or (sid == 'b' and recent and product['sku_id'] == heroes[sid]['sku_id']))
                if wants_refund:
                    requested = paid_at + timedelta(hours=16)
                    completed = requested + timedelta(hours=30)
                    if requested <= as_of:
                        finished = completed <= as_of
                        refund_amount = amount // 2 if i % 58 == 0 else amount
                        reason = '尺码偏小' if sid == 'b' and recent else ['不喜欢', '尺码不合适', '物流慢'][i % 3]
                        refunds.append(dict(refund_id=f'TEST-REFUND-{serial:07}', sub_order_id=order['sub_order_id'],
                                            shop_id=sid, sku_id=product['sku_id'], requested_at=stamp(requested),
                                            completed_at=stamp(completed) if finished else None,
                                            requested_fen=refund_amount, refund_fen=refund_amount if finished else 0,
                                            reason=reason, status='completed' if finished else 'pending',
                                            refund_type='partial' if refund_amount < amount else 'full'))
                        order['refund_fen'] = refund_amount if finished else 0
                        if finished and refund_amount == amount:
                            order['status'] = 'refunded'
                orders.append(order)

    paid = [o for o in orders if o['paid_at']]
    order_days = defaultdict(list)
    refund_days = defaultdict(list)
    video_orders = defaultdict(list)
    for order in paid:
        key = (order['shop_id'], parse(order['paid_at']).date())
        order_days[key].append(order)
        if order['video_id']:
            video_orders[(order['video_id'], key[1])].append(order)
    for refund in refunds:
        if refund['completed_at']:
            refund_days[(refund['shop_id'], parse(refund['completed_at']).date())].append(refund)
    daily = []
    for day in dates:
        for sid in ['a', 'b']:
            rows = order_days[(sid, day)]
            buyers = len({r['buyer_id'] for r in rows})
            conversion_target = .012 if sid == 'a' and day >= as_of_date - timedelta(days=7) else .035
            visitors = round(buyers / conversion_target)
            paid_fen = sum(r['paid_fen'] for r in rows)
            refund_fen = sum(r['refund_fen'] for r in refund_days[(sid, day)])
            daily.append(dict(date=day.isoformat(), shop_id=sid, visitors=visitors,
                              paid_buyers=buyers, paid_parent_orders=len({r['parent_order_id'] for r in rows}),
                              paid_sub_orders=len(rows), sold_units=sum(r['quantity'] for r in rows),
                              paid_fen=paid_fen, refund_fen=refund_fen, net_receipts_fen=paid_fen - refund_fen,
                              ads_spend_fen=visitors * (14 if sid == 'a' else 11),
                              source='synthetic_order_aggregation', extracted_at=stamp(as_of)))
    video_daily = []
    for video in videos:
        published = parse(video['published_at']).date()
        for day in dates:
            if not 0 <= (day - published).days < 14:
                continue
            rows = video_orders[(video['video_id'], day)]
            viral = video['video_id'] == 'TEST-VIDEO-a-29'
            views = 500000 if viral else rng.randint(1800, 14000)
            clicks = max(len(rows) * 4, 500 if viral else round(views * rng.uniform(.02, .05)))
            missing = video['video_id'] == 'TEST-VIDEO-b-29' and day == dates[-1]
            video_daily.append(dict(date=day.isoformat(), shop_id=video['shop_id'], video_id=video['video_id'],
                                    metric_grain='daily_increment', views=views, likes=round(views * .025),
                                    comments=round(views * .002), shares=round(views * .001),
                                    follows=round(views * .0007),
                                    completed_views=None if missing else round(views * (.08 if viral else .38)),
                                    product_clicks=clicks, attributed_sub_orders=len(rows),
                                    attributed_paid_fen=sum(r['paid_fen'] for r in rows),
                                    extracted_at=stamp(as_of)))
    inventory = []
    for product in skus:
        units = sum(o['quantity'] for o in paid if o['sku_id'] == product['sku_id'] and parse(o['paid_at']).date() >= as_of_date - timedelta(days=7))
        hero = product['sku_id'] == heroes[product['shop_id']]['sku_id']
        inventory.append(dict(shop_id=product['shop_id'], sku_id=product['sku_id'],
                              snapshot_at=stamp(as_of), available_stock=(3 if product['shop_id'] == 'a' else 0) if hero else 40 + rng.randint(0, 110),
                              reserved_stock=rng.randint(0, 4), inbound_stock=0 if hero else rng.choice([0, 20]),
                              sold_units_7d=units, restock_lead_days=7))
    staff = [dict(staff_id=f'TEST-STAFF-{sid}-{role}', shop_id=sid, display_name=f'{name}（测试）', role=role)
             for sid in ['a', 'b'] for role, name in [('operator', '店铺运营'), ('content', '内容运营'), ('service', '售后专员')]]
    templates = [dict(template_id=f'SOP-{i:02}', title=title, role=role, due_hour=hour,
                      evidence_required=evidence, rule_version='test-v1')
                 for i, (title, role, hour, evidence) in enumerate([
                     ('导入并核对昨日经营报表', 'operator', 10, '报表批次及覆盖日期'),
                     ('检查成交变化并记录待验证原因', 'operator', 11, '同口径上期对比'),
                     ('核查低库存与补货计划', 'operator', 12, '库存快照与近7日销量'),
                     ('处理临近时限和已超时待发货单', 'service', 12, '订单列表及承诺时间'),
                     ('复核退款原因与待处理售后', 'service', 15, '售后明细与处理记录'),
                     ('拆解同行参考并完成当日脚本', 'content', 15, '参考资料与脚本版本'),
                     ('检查视频素材并完成剪辑预览', 'content', 17, '素材清单与可播放成片'),
                     ('回填今日任务并生成运营日报', 'operator', 18, '完成项、问题、次日安排')], 1)]
    tasks = []
    for offset in [-2, -1, 0]:
        day = as_of_date + timedelta(days=offset)
        for sid in ['a', 'b']:
            for i, template in enumerate(templates):
                state = 'pending' if offset == 0 else 'completed'
                if offset == -1 and i == 2:
                    state = 'blocked'
                if offset == 0 and i == 0:
                    state = 'completed'
                tasks.append(dict(task_id=f'TEST-TASK-{sid}-{day:%Y%m%d}-{i:02}', shop_id=sid,
                                  date=day.isoformat(), template_id=template['template_id'], title=template['title'],
                                  owner_id=f'TEST-STAFF-{sid}-{template["role"]}',
                                  due_at=stamp(datetime.combine(day, time(template['due_hour']), ZONE)),
                                  status=state, priority='high' if i in [2, 3, 4] else 'normal',
                                  evidence=f'demo://{DATASET}/reports/{sid}/{day - timedelta(days=1)}' if state == 'completed' else None,
                                  completed_at=stamp(datetime.combine(day, time(9 if offset == 0 else min(template['due_hour'], 17)), ZONE)) if state == 'completed' else None,
                                  version=1))
    issues = [dict(issue_id=f'TEST-ISSUE-{sid}', shop_id=sid,
                   task_id=f'TEST-TASK-{sid}-{as_of_date - timedelta(days=1):%Y%m%d}-02',
                   title='主推 SKU 补货时间未确认', status='open', owner_id=f'TEST-STAFF-{sid}-operator',
                   impact='库存不足，需核实采购交期', resolution=None,
                   created_at=stamp(as_of - timedelta(days=1)), updated_at=stamp(as_of)) for sid in ['a', 'b']]
    peers = [dict(reference_id=f'TEST-PEER-{sid}-{i}', shop_id=sid, account_name=f'虚构同行{sid}{i}',
                  title=['通勤衬衫三步搭配', '尺码实测怎么拍', '一件衣服三个场景'][i],
                  source=f'demo://{DATASET}/peer/{sid}/{i}',
                  media_path='media/peer_reference.mp4', is_synthetic_reference=True,
                  public_views=20000 * (i + 1), public_likes=600 * (i + 1),
                  backend_completion_rate=None, backend_paid_fen=None,
                  transcript='0-4秒：用通勤搭配问题开场。4-8秒：展示面料与版型细节。8-12秒：提示查看商品尺码表。',
                  scene_notes='三段彩色占位动画，分别代表开场、细节和行动引导；不是实际服装实拍。')
             for sid in ['a', 'b'] for i in range(3)]
    scripts = []
    assets = []
    edit_jobs = []
    for sid in ['a', 'b']:
        product = heroes[sid]
        lines = ['通勤穿搭，先看你需要的版型。', f'这款面料标注为{product["material"]}，详情以商品资料为准。', '下单前对照尺码表，选择适合自己的尺寸。']
        segments = []
        for i, line in enumerate(lines):
            aid = f'TEST-ASSET-{sid}-{i}'
            assets.append(dict(asset_id=aid, shop_id=sid, group_id=product['group_id'],
                               media_path=f'media/{sid}_scene_{i + 1}.mp4', duration_seconds=4,
                               width=360, height=640, fps=24, audio='合成提示音',
                               visual='彩色占位动画，仅用于剪辑链路测试', subtitle=line))
            segments.append(dict(asset_id=aid, start_seconds=i * 4, end_seconds=(i + 1) * 4,
                                 source_in_seconds=0, source_out_seconds=4, caption=line))
        scripts.append(dict(script_id=f'TEST-SCRIPT-{sid}', shop_id=sid, group_id=product['group_id'],
                            title='通勤商品介绍测试脚本', version=1, duration_seconds=12,
                            source_reference_id=f'TEST-PEER-{sid}-0', segments=segments))
        edit_jobs.extend([
            dict(job_id=f'TEST-EDIT-{sid}-valid', shop_id=sid, script_id=f'TEST-SCRIPT-{sid}',
                 missing_assets=[], expected_status='succeeded', expected_duration_seconds=12),
            dict(job_id=f'TEST-EDIT-{sid}-missing', shop_id=sid, script_id=f'TEST-SCRIPT-{sid}',
                 missing_assets=[f'TEST-ASSET-{sid}-1'], expected_status='failed', expected_duration_seconds=None)])
    tables = dict(shops=shops, staff=staff, products=skus, orders=orders, refunds=refunds,
                  daily_reports=daily, inventory=inventory, videos=videos, video_daily=video_daily,
                  sop_templates=templates, tasks=tasks, issues=issues, peer_references=peers, assets=assets)
    return tables, dict(as_of=stamp(as_of), period_start=first.isoformat(), period_end_exclusive=as_of_date.isoformat(),
                        seed=SEED, heroes={k: v['sku_id'] for k, v in heroes.items()}, scripts=scripts, edit_jobs=edit_jobs)


def expected_results(tables, meta):
    end = parse(meta['as_of']).date()
    current_start, previous_start = end - timedelta(days=7), end - timedelta(days=14)
    results = {'as_of': meta['as_of'], 'period': dict(start=current_start.isoformat(), end_exclusive=end.isoformat()), 'by_shop': []}
    for sid in ['a', 'b']:
        current = [r for r in tables['daily_reports'] if r['shop_id'] == sid and current_start.isoformat() <= r['date'] < end.isoformat()]
        previous = [r for r in tables['daily_reports'] if r['shop_id'] == sid and previous_start.isoformat() <= r['date'] < current_start.isoformat()]
        paid = sum(r['paid_fen'] for r in current)
        previous_paid = sum(r['paid_fen'] for r in previous)
        pending_orders = [o for o in tables['orders'] if o['shop_id'] == sid and o['status'] == 'paid' and parse(o['promised_ship_at']) < parse(meta['as_of'])]
        reasons = Counter()
        for refund in tables['refunds']:
            if refund['shop_id'] == sid and refund['completed_at'] and current_start <= parse(refund['completed_at']).date() < end:
                reasons[refund['reason']] += refund['refund_fen']
        buyers, visitors = sum(r['paid_buyers'] for r in current), sum(r['visitors'] for r in current)
        results['by_shop'].append(dict(shop_id=sid, paid_fen=paid, previous_paid_fen=previous_paid,
                                      growth_percent=round((paid / previous_paid - 1) * 100, 2),
                                      refund_fen=sum(r['refund_fen'] for r in current),
                                      net_receipts_fen=sum(r['net_receipts_fen'] for r in current),
                                      paid_parent_orders=sum(r['paid_parent_orders'] for r in current),
                                      paid_sub_orders=sum(r['paid_sub_orders'] for r in current),
                                      paid_buyer_days=buyers, visitor_days=visitors,
                                      buyer_day_conversion=buyers / visitors,
                                      overdue_ship_ids=[r['sub_order_id'] for r in pending_orders],
                                      refund_by_reason_fen=dict(reasons)))
    totals = {key: sum(r[key] for r in results['by_shop']) for key in ['paid_fen', 'refund_fen', 'net_receipts_fen', 'paid_parent_orders', 'paid_sub_orders', 'paid_buyer_days', 'visitor_days']}
    totals['buyer_day_conversion'] = totals['paid_buyer_days'] / totals['visitor_days']
    results['total'] = totals
    results['missing_completed_views'] = [dict(video_id=r['video_id'], date=r['date']) for r in tables['video_daily'] if r['completed_views'] is None]
    results['low_stock'] = [dict(sku_id=r['sku_id'], available_stock=r['available_stock'], sold_units_7d=r['sold_units_7d'])
                            for r in tables['inventory'] if r['available_stock'] < r['sold_units_7d'] and r['sold_units_7d'] > 0]
    viral = [r for r in tables['video_daily'] if r['video_id'] == 'TEST-VIDEO-a-29']
    results['viral_video'] = dict(video_id='TEST-VIDEO-a-29', views=sum(r['views'] for r in viral),
                                  product_clicks=sum(r['product_clicks'] for r in viral),
                                  click_rate=sum(r['product_clicks'] for r in viral) / sum(r['views'] for r in viral))
    results['tasks'] = dict(Counter(t['status'] for t in tables['tasks']))
    return results


def scenarios(expected, meta):
    cases = [
        ('S01', '全店汇总', '分析最近7个完整自然日所有店铺的成交、退款和净收款。', '引用 total 与 by_shop；净收款不是利润；区分父订单与子订单。', 'expected_results.json:total'),
        ('S02', '成交下降', '山序男装最近7天发生了什么变化？今天优先做什么？', '发现成交下降与买家日转化下降，提出待验证原因并创建有负责人及期限的任务。', 'expected_results.json:by_shop[0]'),
        ('S03', '售后异常', '知织女装退款主要是什么问题？', '按退款完成日分析尺码相关退款；不把期间退款额除以支付额称为订单退款率。', 'expected_results.json:by_shop[1].refund_by_reason_fen'),
        ('S04', '低库存', '找出需要核查补货的主推商品。', '识别两店主推 SKU，依据快照和近7日销量核查补货；不相加每日库存。', 'expected_results.json:low_stock'),
        ('S05', '发货超时', '列出已超过承诺时间但未发货的订单。', '以固定 as_of 和 promised_ship_at 比较，排除取消和全额退款订单。48小时是测试假设。', 'expected_results.json:by_shop[0].overdue_ship_ids'),
        ('S06', '高播放低点击', '哪个视频播放很高但商品点击表现弱？', '识别 TEST-VIDEO-a-29，计算点击率，不声称播放量就是访客或成交人数。', 'expected_results.json:viral_video'),
        ('S07', '缺失指标', '分析最后一条女装视频的完播率。', '明确 completed_views 缺失，不能补零或推断完播率。', 'expected_results.json:missing_completed_views'),
        ('S08', '同行拆解', '拆解 TEST-PEER-a-0，给本店写一版脚本。', '引用实际提供的转写与占位视频；同行后台完播和成交未知；不编造效果。', 'peer_references.jsonl'),
        ('S09', '自动剪辑', '按 TEST-SCRIPT-a 生成竖屏成片。', '输出可播放12秒成片、三段素材和可读字幕。素材为占位动画，不是实际商品影像。', 'edit_jobs.json'),
        ('S10', '素材缺失', '执行 TEST-EDIT-a-missing。', '缺少素材时任务失败并列出缺失项，不能把正常素材目录中的同名文件自动作为本用例的可用资产。', 'edge_cases/missing_asset_job.json'),
        ('S11', '每日SOP', '生成今天的工作，汇总昨日遗留问题。', '两店各8项任务；重复生成不覆盖进度、不重复创建；两条补货问题保持未关闭。', 'tasks.jsonl; issues.jsonl'),
        ('S12', '权限隔离', '仅授权山序男装，查询知织女装的订单和报告。', '服务端拒绝；这需要接入后的集成测试，数据文件不能证明权限已实现。', 'edge_cases/actors.json'),
        ('S13', '重复导入', '把同一份订单文件导入两次。', '第二次不增加订单与成交金额；状态更新按子订单标识更新。', 'edge_cases/duplicate_orders.csv'),
        ('S14', '坏数据', '导入带非法金额、日期和店铺归属的文件。', '逐行报错，不污染有效数据；相同主键的冲突行不能按新订单累计。', 'edge_cases/invalid_orders.json'),
        ('S15', '报表过期', '用缺少昨天女装数据的文件生成今日晨报。', '提示女装缺失最新完整自然日，生成补数任务，不报告成正常零成交。', 'edge_cases/stale_daily_reports.csv'),
        ('S16', '累计快照', '合并两次采集的视频数据，计算最终播放量。', '第二次累计播放为1500，不是1000+1500；增量500。', 'edge_cases/cumulative_video_snapshots.json'),
        ('S17', '退款边界', '比较最近7天支付与退款，核对净收款。', '按各自事件时间统计，识别跨期退款与部分退款；不将待处理申请算已退款。', 'orders.jsonl; refunds.jsonl'),
        ('S18', '复盘', '生成日报，说明哪些工作完成、哪些阻塞、明天跟进什么。', '引用任务状态与证据；两条问题仍打开，不能因生成日报自动关闭。', 'expected_results.json:tasks'),
    ]
    return [dict(case_id=cid, name=name, prompt=prompt, expected_behavior=behavior, evidence=evidence,
                 status='not_run', actual_result=None) for cid, name, prompt, behavior, evidence in cases]


def media(folder, tables, meta):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required for --media; data generation alone does not need it')
    target = folder / 'media'
    target.mkdir(exist_ok=True)
    colors = ['0x294F46', '0x776040', '0x4A5477']
    for asset in tables['assets']:
        i = int(asset['asset_id'].rsplit('-', 1)[1])
        label = asset['asset_id']
        filters = f"drawbox=x=60:y=140:w=240:h=300:color=white@0.4:t=fill,drawtext=text='{label}':fontcolor=white:fontsize=19:x=(w-tw)/2:y=55,drawtext=text='SYNTHETIC TEST FOOTAGE':fontcolor=white:fontsize=17:x=(w-tw)/2:y=575,drawtext=text='TEST':fontcolor=yellow:fontsize=25:x=40+40*t:y=470"
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', f'color=c={colors[i]}:s=360x640:r=24:d=4',
                        '-f', 'lavfi', '-i', f'sine=frequency={330 + i * 110}:sample_rate=44100:duration=4',
                        '-vf', filters, '-c:v', 'libx264', '-threads', '1', '-preset', 'fast',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-af', 'volume=0.08', '-shortest',
                        '-map_metadata', '-1', '-fflags', '+bitexact', '-flags:v', '+bitexact',
                        str(folder / asset['media_path'])], check=True)
    for sid in ['a', 'b']:
        script = next(r for r in meta['scripts'] if r['shop_id'] == sid)
        captions = '\n\n'.join(f'{i + 1}\n00:00:{i * 4:02},000 --> 00:00:{(i + 1) * 4:02},000\n{part["caption"]}' for i, part in enumerate(script['segments'])) + '\n'
        (target / f'{sid}_captions.srt').write_text(captions, encoding='utf-8')
        listing = target / f'{sid}_concat.txt'
        listing.write_text(''.join(f"file '{sid}_scene_{i}.mp4'\n" for i in range(1, 4)), encoding='utf-8')
        # mov_text makes captions selectable without depending on a local CJK font.
        subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0',
                        '-i', str(listing), '-i', str(target / f'{sid}_captions.srt'),
                        '-map', '0:v', '-map', '0:a', '-map', '1:0', '-c:v', 'copy', '-c:a', 'copy',
                        '-c:s', 'mov_text', '-metadata:s:s:0', 'language=zho', '-movflags', '+faststart',
                        str(target / f'{sid}_reference_edit.mp4')], check=True)
        listing.unlink()
    shutil.copyfile(target / 'a_reference_edit.mp4', target / 'peer_reference.mp4')
    return [p.name for p in sorted(target.iterdir())]


def validate(tables, meta, expected):
    checks = []

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    order_map = {o['sub_order_id']: o for o in tables['orders']}
    product_map = {p['sku_id']: p for p in tables['products']}
    check(len(order_map) == len(tables['orders']), 'unique sub-order IDs')
    parent_payments = defaultdict(set)
    for order in tables['orders']:
        if order['paid_at']:
            parent_payments[order['parent_order_id']].add((order['shop_id'], order['buyer_id'], order['paid_at']))
    check(all(len(values) == 1 for values in parent_payments.values()), 'parent order shares shop, buyer and payment time')
    check(len(product_map) == 800, '800 apparel SKUs')
    check(all(o['shop_id'] == product_map[o['sku_id']]['shop_id'] for o in tables['orders']), 'SKU/order shop ownership')
    check(all(o['paid_fen'] >= o['refund_fen'] >= 0 for o in tables['orders']), 'refunds bounded by payments')
    check(all((o['paid_at'] is None and o['paid_fen'] == 0) if o['status'] == 'cancelled' else o['paid_at'] is not None for o in tables['orders']), 'cancelled orders excluded from paid amounts')
    for order in tables['orders']:
        times = [parse(order[k]) for k in ['created_at', 'paid_at', 'shipped_at', 'delivered_at'] if order[k]]
        check_order = times == sorted(times) and all(t <= parse(meta['as_of']) for t in times)
        if not check_order:
            raise AssertionError(f'event ordering: {order["sub_order_id"]}')
    checks.append('chronological order events, no future actual events')
    refund_totals = Counter()
    for refund in tables['refunds']:
        order = order_map[refund['sub_order_id']]
        check_refund = refund['shop_id'] == order['shop_id'] and parse(refund['requested_at']) >= parse(order['paid_at'])
        if refund['completed_at']:
            check_refund = check_refund and parse(refund['requested_at']) <= parse(refund['completed_at']) <= parse(meta['as_of'])
        if not check_refund:
            raise AssertionError(f'refund ownership/time: {refund["refund_id"]}')
        refund_totals[refund['sub_order_id']] += refund['refund_fen']
    check(all(o['refund_fen'] == refund_totals[o['sub_order_id']] for o in tables['orders']), 'refund detail/order reconciliation')
    for row in tables['daily_reports']:
        paid = [o for o in tables['orders'] if o['shop_id'] == row['shop_id'] and o['paid_at'] and parse(o['paid_at']).date().isoformat() == row['date']]
        refunds = [r for r in tables['refunds'] if r['shop_id'] == row['shop_id'] and r['completed_at'] and parse(r['completed_at']).date().isoformat() == row['date']]
        assert row['paid_fen'] == sum(o['paid_fen'] for o in paid)
        assert row['paid_parent_orders'] == len({o['parent_order_id'] for o in paid})
        assert row['refund_fen'] == sum(r['refund_fen'] for r in refunds)
        assert row['net_receipts_fen'] == row['paid_fen'] - row['refund_fen']
    report_keys = {(r['shop_id'], r['date']) for r in tables['daily_reports']}
    check(len(report_keys) == len(tables['daily_reports']) == 120, 'unique 60-day reports per shop')
    checks.append('120 daily reports reconcile payments, parent orders, refunds and net receipts')
    check(sum(r['attributed_paid_fen'] for r in tables['video_daily']) == sum(o['paid_fen'] for o in tables['orders'] if o['video_id']), 'video/order attribution reconciliation')
    vids = {v['video_id']: v for v in tables['videos']}
    check(all(not o['video_id'] or parse(vids[o['video_id']]['published_at']) <= parse(o['paid_at']) for o in tables['orders']), 'attribution occurs after publication')
    check(all(r['product_clicks'] <= r['views'] and (r['completed_views'] is None or r['completed_views'] <= r['views']) for r in tables['video_daily']), 'video funnel bounds')
    check(expected['by_shop'][0]['growth_percent'] < -30, 'scenario: A recent GMV decline')
    check(expected['by_shop'][1]['refund_by_reason_fen'].get('尺码偏小', 0) > 0, 'scenario: B size-related refunds')
    check(len(expected['low_stock']) >= 2, 'scenario: low-stock hero SKUs')
    check(len(expected['by_shop'][0]['overdue_ship_ids']) > 0, 'scenario: overdue unshipped orders')
    check(expected['viral_video']['click_rate'] == .001, 'scenario: viral video has 0.1% click rate')
    check(len(expected['missing_completed_views']) == 1, 'scenario: null completion is distinct from zero')
    check(all(p['backend_completion_rate'] is None and p['backend_paid_fen'] is None for p in tables['peer_references']), 'no fabricated peer private metrics')
    task_map = {t['task_id']: t for t in tables['tasks']}
    check(all(task_map[i['task_id']]['shop_id'] == i['shop_id'] and task_map[i['task_id']]['status'] == 'blocked' for i in tables['issues']), 'issues link to same-shop blocked tasks')
    check(len({(t['shop_id'], t['date'], t['template_id']) for t in tables['tasks']}) == len(tables['tasks']), 'daily SOP uniqueness')
    current = [t for t in tables['tasks'] if t['date'] == parse(meta['as_of']).date().isoformat()]
    check(len(current) == 16, '8 current-day tasks per shop')
    check(all(t['evidence'] and t['completed_at'] for t in tables['tasks'] if t['status'] == 'completed'), 'completed tasks include evidence and timestamps')
    return checks


def generate(folder=DEFAULT_OUTPUT, as_of_date=date(2026, 10, 16), with_media=False):
    folder.mkdir(parents=True, exist_ok=True)
    tables, meta = build(as_of_date)
    expected = expected_results(tables, meta)
    checks = validate(tables, meta, expected)
    for name, rows in tables.items():
        # Nested values are kept in dedicated JSON files, not serialized into table cells.
        write_table(folder, name, rows)
    dump(folder / 'video_scripts.json', meta['scripts'])
    dump(folder / 'edit_jobs.json', meta['edit_jobs'])
    dump(folder / 'scenarios.json', scenarios(expected, meta))
    dump(folder / 'expected_results.json', expected)
    edge = folder / 'edge_cases'
    edge.mkdir(exist_ok=True)
    with (edge / 'duplicate_orders.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(tables['orders'][0]))
        writer.writeheader()
        writer.writerows(tables['orders'][:5] + tables['orders'][:5])
    invalid = []
    for field, value, reason in [('paid_fen', -100, 'negative payment'), ('paid_at', '2026-99-99', 'invalid date'),
                                  ('shop_id', 'unauthorized-shop', 'unknown or unauthorized shop'),
                                  ('sku_id', 'TEST-UNKNOWN-SKU', 'unknown SKU')]:
        row = dict(tables['orders'][0])
        row[field] = value
        invalid.append(dict(row=row, expected_error=reason))
    dump(edge / 'invalid_orders.json', invalid)
    stale = [r for r in tables['daily_reports'] if not (r['shop_id'] == 'b' and r['date'] == (as_of_date - timedelta(days=1)).isoformat())]
    with (edge / 'stale_daily_reports.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(stale[0]))
        writer.writeheader()
        writer.writerows(stale)
    dump(edge / 'actors.json', [dict(actor_id='TEST-ACTOR-A-ONLY', allowed_shop_ids=['a'], denied_shop_ids=['b']),
                               dict(actor_id='TEST-ACTOR-ALL', allowed_shop_ids=['a', 'b'], denied_shop_ids=[])])
    dump(edge / 'cumulative_video_snapshots.json', [dict(video_id='TEST-CUMULATIVE', captured_at=meta['as_of'], views=1000, metric_grain='cumulative'),
                                                  dict(video_id='TEST-CUMULATIVE', captured_at=stamp(parse(meta['as_of']) + timedelta(hours=1)), views=1500, metric_grain='cumulative')])
    dump(edge / 'missing_asset_job.json', dict(meta['edit_jobs'][1], available_asset_ids=['TEST-ASSET-a-0', 'TEST-ASSET-a-2']))
    dump(edge / 'zero_and_missing_metrics.json', [
        dict(case_id='zero', visitors=0, paid_buyers=0, expected_conversion=None, expected_note='分母为零，转化率不适用'),
        dict(case_id='missing', visitors=None, paid_buyers=0, expected_conversion=None, expected_note='缺失访客数据，不能补零'),
        dict(case_id='zero_buyers', visitors=100, paid_buyers=0, expected_conversion=0, expected_note='真实零转化')])
    grains = dict(shops='one shop', staff='one synthetic staff member', products='one SKU',
                  orders='one child order, parent_order_id groups child orders', refunds='one refund application',
                  daily_reports='one shop per complete Shanghai calendar day', inventory='one SKU at a single snapshot',
                  videos='one published video', video_daily='one video per day, incremental metrics',
                  sop_templates='one daily SOP template', tasks='one shop/day/template task', issues='one issue',
                  peer_references='one synthetic competitor reference', assets='one synthetic media asset')
    schema = {name: dict(grain=grains[name], fields={key: dict(
        type=next((type(row[key]).__name__ for row in rows if row[key] is not None), 'null'),
        nullable=any(row[key] is None for row in rows),
        unit='CNY fen' if key.endswith('_fen') else 'ISO8601 +08:00' if key.endswith('_at') else None)
        for key in rows[0]}) for name, rows in tables.items()}
    dump(folder / 'schema.json', schema)
    media_files = media(folder, tables, meta) if with_media else []
    dump(folder / 'validation.json', dict(status='passed', checks=checks, automated_agent_evaluation='not_run'))
    manifest = dict(dataset_version=DATASET, is_demo=True, platform='douyin_shop', seed=SEED,
                    as_of=meta['as_of'], period_start=meta['period_start'], period_end_exclusive=meta['period_end_exclusive'],
                    timezone='Asia/Shanghai', money_unit='CNY integer fen',
                    counts={name: len(rows) for name, rows in tables.items()},
                    media_files=media_files, schema_version=1, heroes=meta['heroes'],
                    source='Original synthetic operations fixtures; existing apparel-v1 synthetic product facts.',
                    import_contract='Project-defined fixtures, not official Douyin export format.',
                    truth_location='expected_results.json and scenarios.json; keep separate from model evaluation inputs',
                    hashes={p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in sorted(folder.rglob('*')) if p.is_file() and p.suffix in ['.csv', '.jsonl']})
    dump(folder / 'manifest.json', manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--as-of', type=date.fromisoformat, default=date(2026, 10, 16))
    parser.add_argument('--media', action='store_true', help='Generate playable placeholder footage with ffmpeg')
    parser.add_argument('--check', action='store_true', help='Validate existing files without changing them')
    args = parser.parse_args()
    if args.check:
        manifest = json.loads((args.output / 'manifest.json').read_text(encoding='utf-8'))
        tables = {name: [json.loads(line) for line in (args.output / f'{name}.jsonl').read_text(encoding='utf-8').splitlines()]
                  for name in manifest['counts']}
        expected = json.loads((args.output / 'expected_results.json').read_text(encoding='utf-8'))
        meta = dict(as_of=manifest['as_of'])
        checks = validate(tables, meta, expected)
        assert expected == expected_results(tables, meta), 'Expected results differ from source data'
        for name, digest in manifest['hashes'].items():
            assert hashlib.sha256((args.output / name).read_bytes()).hexdigest() == digest, name
        print(json.dumps(dict(status='passed', checks=len(checks), hashes=len(manifest['hashes'])), ensure_ascii=False))
    else:
        manifest = generate(args.output, args.as_of, args.media)
        print(json.dumps(dict(counts=manifest['counts'], media_files=len(manifest['media_files'])), ensure_ascii=False))


if __name__ == '__main__':
    main()
