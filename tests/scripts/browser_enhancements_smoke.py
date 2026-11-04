"""Verify report mapping and one real Dify rewrite with a temporary operator."""
import io
import json
from datetime import datetime
from uuid import uuid4

import httpx
from openpyxl import Workbook
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete, select

from commerce.auth import password_hash
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import AccessToken, Audit, Membership, OpsImport, OpsRecord, OpsScript, User


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    actor = 'enhance-' + uuid4().hex[:12]
    title = '文案增强验收-' + uuid4().hex[:8]
    screenshots = ROOT / 'tests/artifacts/screenshots'
    screenshots.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with SessionLocal() as db:
        db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='临时增强验收',
                    password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add(Membership(user_id=actor, shop_id='a'))
        db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=120) as api:
            login = api.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password})
            login.raise_for_status()
            api.headers['Authorization'] = 'Bearer ' + login.json()['access_token']
            data = api.get('/api/ops/assets').json()
            product = next(p for p in data['products'] if any(a['group_id'] == p['group_id'] and a['metadata']['duration_seconds'] >= 3 for a in data['items']))
            asset = next(a for a in data['items'] if a['group_id'] == product['group_id'] and a['metadata']['duration_seconds'] >= 3)
            script = api.post('/api/ops/scripts', json=dict(product_id=product['sku_id'], title=title, segments=[
                dict(asset_id=asset['id'], in_seconds=0, out_seconds=1, caption='纯羊毛面料，限时五折，只要1元'),
                dict(asset_id=asset['id'], in_seconds=1, out_seconds=2, caption='商品面料：' + product['material']),
                dict(asset_id=asset['id'], in_seconds=2, out_seconds=3, caption='防水面料', locked=True)]))
            script.raise_for_status()
            original = script.json()
            workbook = Workbook()
            workbook.active.title = '说明'
            workbook.active.append(['说明'])
            workbook.active.append(['合成验收数据，仅作预览，不提交经营数据'])
            sheet = workbook.create_sheet('日报')
            sheet.append(['日期', '访客数', '支付买家数', '支付父订单数', '支付子订单数', '销售件数',
                          '支付金额', '退款金额', '净收款', '广告消耗', '数据来源', '数据提取时间'])
            sheet.append([datetime(2026, 10, 17), 100, 2, 2, 3, 3, '1234.56', '34.56', '1200.00',
                          '0.00', 'synthetic-browser-preview', datetime(2026, 10, 18, 9)])
            buffer = io.BytesIO()
            workbook.save(buffer)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                context.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, body=''))
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8000', wait_until='domcontentloaded')
                page.get_by_label('运营账号', exact=True).fill(actor)
                page.get_by_label('登录密码', exact=True).fill(settings.demo_password)
                page.get_by_role('button', name='进入工作台', exact=True).click()
                expect(page.locator('.sidebar')).to_be_visible()
                page.get_by_role('button', name='数据导入', exact=True).click()
                expect(page.get_by_label('报表文件', exact=True)).to_be_enabled()
                page.get_by_label('报表文件', exact=True).set_input_files({'name': '合成日报.xlsx',
                    'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'buffer': buffer.getvalue()})
                expect(page.get_by_label('工作表', exact=True)).to_be_enabled()
                page.get_by_label('工作表', exact=True).select_option('日报')
                expect(page.get_by_label('支付金额源列', exact=True)).to_have_value('支付金额')
                expect(page.get_by_label('支付金额金额单位', exact=True)).to_have_value('yuan')
                page.get_by_role('button', name='校验报表', exact=True).click()
                expect(page.locator('.import-preview')).to_contain_text('有效 1 行')
                parsed = json.loads(page.locator('.import-preview pre').inner_text())
                assert parsed[0]['paid_fen'] == 123456 and parsed[0]['net_receipts_fen'] == 120000
                assert parsed[0]['extracted_at'] == '2026-10-18T09:00:00+08:00'
                checks.extend(['excel_sheet_selection', 'chinese_column_mapping', 'yuan_to_fen_and_timezone'])
                page.get_by_label('映射模板名称', exact=True).fill('合成日报表头')
                page.get_by_role('button', name='保存映射模板', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('字段映射模板已保存')
                page.get_by_label('复用映射模板', exact=True).select_option('0')
                page.get_by_role('button', name='校验报表', exact=True).click()
                expect(page.locator('.import-preview')).to_contain_text('有效 1 行')
                checks.append('saved_mapping_reused')
                page.get_by_label('支付金额金额单位', exact=True).select_option('fen')
                expect(page.locator('.import-preview')).to_have_count(0)
                page.get_by_role('button', name='校验报表', exact=True).click()
                expect(page.locator('.import-preview')).to_contain_text('金额格式无效')
                expect(page.get_by_role('button', name='确认导入', exact=True)).to_be_disabled()
                checks.append('mapping_change_invalidates_preview_and_bad_units_block_commit')
                page.get_by_label('支付金额金额单位', exact=True).select_option('yuan')
                page.get_by_role('button', name='校验报表', exact=True).click()
                expect(page.locator('.import-preview')).to_contain_text('有效 1 行')
                page.screenshot(path=str(screenshots / 'enhancements-import.png'), full_page=True)
                page.get_by_role('combobox', name='报表类型', exact=True).select_option('video_daily')
                expect(page.get_by_label('报表文件', exact=True)).to_be_enabled()
                metric = dict(date='2026-10-17', shop_id='a', video_id='TEST-VIDEO-a-00', metric_grain='单日新增',
                              views=100, likes=10, comments=0, shares=0, follows=0, completed_views=60,
                              product_clicks=5, attributed_sub_orders=0, attributed_paid_fen=0,
                              extracted_at='2026-10-18T09:00:00+08:00', dataset_version='synthetic-preview', is_demo=True)
                page.get_by_label('报表文件', exact=True).set_input_files({'name': '合成视频指标.json',
                    'mimeType': 'application/json', 'buffer': json.dumps([metric]).encode()})
                try:
                    expect(page.get_by_role('checkbox', name='使用字段映射与格式转换', exact=True)).to_be_enabled()
                except AssertionError:
                    raise AssertionError({'browser_errors': errors, 'visible_status': page.locator('[role=status]').all_text_contents()}) from None
                page.get_by_role('checkbox', name='使用字段映射与格式转换', exact=True).check()
                field = page.locator('.mapping-field').filter(has=page.locator('strong', has_text='指标口径'))
                field.locator('summary').click()
                text = page.get_by_label('指标口径自定义值映射', exact=True)
                text.press_sequentially('单日新增=daily_increment')
                expect(text).to_have_value('单日新增=daily_increment')
                page.get_by_role('button', name='校验报表', exact=True).click()
                expect(page.locator('.import-preview')).to_contain_text('有效 1 行')
                parsed = json.loads(page.locator('.import-preview pre').inner_text())
                assert parsed[0]['metric_grain'] == 'daily_increment'
                checks.append('custom_value_mapping_can_be_typed_and_applied')
                page.get_by_role('button', name='短视频运营', exact=True).click()
                card = page.locator('article.ops-peer').filter(has=page.locator('strong', has_text=title))
                expect(card).to_have_count(1)
                card.get_by_role('button', name='编辑脚本', exact=True).click()
                expect(page.get_by_label('锁定本段字幕，定向改写时保留').nth(2)).to_be_checked()
                page.get_by_label('关闭脚本', exact=True).click()
                card.get_by_role('button', name='检查文案事实', exact=True).click()
                expect(card.locator('.script-review')).to_contain_text('纯羊毛')
                with page.expect_response(lambda response: response.url.endswith('/rewrite'), timeout=120000) as pending:
                    card.get_by_role('button', name='定向改写一轮', exact=True).click()
                rewrite_response = pending.value
                assert rewrite_response.status == 200, rewrite_response.text()
                expect(card.locator('.rewrite-preview')).to_be_visible()
                expect(card.locator('.rewrite-preview')).to_contain_text('尚未保存')
                changed = api.get('/api/ops/scripts').json()['items']
                assert next(s for s in changed if s['id'] == original['id'])['version'] == 1
                card.scroll_into_view_if_needed()
                page.screenshot(path=str(screenshots / 'enhancements-rewrite.png'), full_page=False)
                card.get_by_role('button', name='采用并保存新版本', exact=True).click()
                expect(card).to_contain_text('v2')
                changed = next(s for s in api.get('/api/ops/scripts').json()['items'] if s['id'] == original['id'])
                assert changed['payload']['segments'][1:] == original['payload']['segments'][1:]
                assert changed['payload']['segments'][0]['caption'] != original['payload']['segments'][0]['caption']
                checks.extend(['fact_findings_visible', 'segment_lock_visible', 'real_dify_rewrite_preview',
                               'explicit_adoption_saves_version', 'good_and_locked_captions_preserved'])
                context.close()
                mobile = browser.new_context(viewport={'width': 390, 'height': 844})
                mobile.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, body=''))
                mobile_page = mobile.new_page()
                mobile_page.on('pageerror', lambda error: errors.append(str(error)))
                mobile_page.goto('http://127.0.0.1:8000', wait_until='domcontentloaded')
                mobile_page.get_by_label('运营账号', exact=True).fill(actor)
                mobile_page.get_by_label('登录密码', exact=True).fill(settings.demo_password)
                mobile_page.get_by_role('button', name='进入工作台', exact=True).click()
                expect(mobile_page.locator('.sidebar')).to_be_visible()
                close = mobile_page.get_by_role('button', name='关闭助手', exact=True)
                if close.is_visible():
                    close.click()
                mobile_page.get_by_role('button', name='数据导入', exact=True).click()
                expect(mobile_page.get_by_label('报表文件', exact=True)).to_be_enabled()
                mobile_page.get_by_label('报表文件', exact=True).set_input_files({'name': '合成日报.xlsx',
                    'mimeType': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'buffer': buffer.getvalue()})
                expect(mobile_page.get_by_label('工作表', exact=True)).to_be_enabled()
                mobile_page.get_by_label('工作表', exact=True).select_option('日报')
                expect(mobile_page.get_by_label('支付金额源列', exact=True)).to_have_value('支付金额')
                assert mobile_page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                mobile_page.screenshot(path=str(screenshots / 'enhancements-mobile.png'), full_page=True)
                checks.append('mobile_mapping_no_horizontal_overflow')
                mobile.close()
                browser.close()
        assert not errors, errors
        report = dict(status='passed', checks=checks, browser_errors=errors, real_model_requests=1,
                      live_report_commits=0, temporary_data_cleaned=True)
        (ROOT / 'tests/artifacts/reports/enhancements-browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('Enhancement browser checks passed:', len(checks), '; real model requests: 1')
    finally:
        with SessionLocal() as db:
            db.execute(delete(OpsScript).where(OpsScript.user_id == actor))
            db.execute(delete(OpsImport).where(OpsImport.user_id == actor))
            db.execute(delete(OpsRecord).where(OpsRecord.kind == 'import_mappings', OpsRecord.payload['owner_id'].as_string() == actor))
            db.execute(delete(Audit).where(Audit.user_id == actor))
            db.execute(delete(AccessToken).where(AccessToken.user_id == actor))
            db.execute(delete(Membership).where(Membership.user_id == actor))
            db.execute(delete(User).where(User.id == actor))
            db.commit()


if __name__ == '__main__':
    main()
