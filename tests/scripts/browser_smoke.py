"""Browser regression for the operations-only workbench. No real model calls."""
import json
from pathlib import Path
from uuid import uuid4
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import Draft

ROOT = Path(__file__).resolve().parents[2]
shots = ROOT / 'tests/artifacts/screenshots'
shots.mkdir(parents=True, exist_ok=True)
checks, errors, created_drafts = [], [], []


def login(browser, mobile=False):
    context = browser.new_context(viewport={'width': 390 if mobile else 1440, 'height': 844 if mobile else 1000})
    # Exercise the local UI without waiting for an optional remote font service.
    context.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, content_type='text/css', body=''))
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:8000', wait_until='domcontentloaded')
    expect(page.locator('.login-form select')).to_have_count(0)
    expect(page.get_by_label('运营账号', exact=True)).to_have_value('operator')
    if not mobile:
        page.screenshot(path=str(shots / 'operator-login.png'), full_page=True)
    page.get_by_label('登录密码', exact=True).fill(settings.demo_password)
    page.get_by_role('button', name='进入工作台', exact=True).click()
    expect(page.locator('.sidebar')).to_be_visible()
    return context, page


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = login(browser)
            page.get_by_label('工作日期', exact=True).fill('2026-10-16')
            page.get_by_label('工作日期', exact=True).dispatch_event('change')
            expect(page.locator('.metric-grid')).to_be_visible()
            expect(page.locator('.ops-task').first).to_be_visible()
            expect(page.get_by_label('店铺范围')).to_have_value('')
            assert '消费者' not in page.locator('.sidebar').inner_text() and '商家' not in page.locator('.sidebar').inner_text()
            expect(page.get_by_role('button', name='新增商品', exact=True)).to_have_count(0)
            checks.extend(['operator_only_login', 'authorized_shop_overview', 'no_retired_role_navigation'])
            page.screenshot(path=str(shots / 'operator-overview.png'), full_page=True)
            page.get_by_role('button', name='店铺订单', exact=True).click()
            expect(page.locator('.order-card').first).to_be_visible()
            expect(page.get_by_role('button', name='申请售后', exact=True)).to_have_count(0)
            checks.append('operator_reads_orders_without_customer_actions')
            page.get_by_role('button', name='商品资料', exact=True).click()
            expect(page.locator('.product-card')).to_have_count(12)
            page.get_by_label('最高售价', exact=True).fill('300')
            page.get_by_label('服装尺码').select_option('L')
            page.get_by_label('服装面料').select_option('棉')
            page.get_by_label('商品分类').select_option('衬衫')
            page.get_by_role('button', name='筛选', exact=True).click()
            expect(page.get_by_role('button', name='筛选', exact=True)).to_be_enabled()
            expect(page.locator('.product-card').first).to_be_visible()
            for value in page.locator('.product-footer strong').all_text_contents():
                assert float(value.replace('¥', '').replace(',', '')) <= 300
            page.locator('.compare-check').nth(0).click()
            page.locator('.compare-check').nth(1).click()
            page.get_by_role('button', name='对比已选 2 件').click()
            expect(page.locator('.comparison')).to_be_visible()
            page.locator('.modal').get_by_role('button', name='关闭', exact=True).click()
            checks.extend(['product_fact_filters', 'product_comparison'])
            page.get_by_label('店铺范围').select_option('b')
            expect(page.get_by_label('店铺范围')).to_be_enabled()
            expect(page.get_by_role('button', name='对比已选 2 件')).to_have_count(0)
            expect(page.get_by_label('商品分类')).to_have_value('')
            assert all('知织女装' in text for text in page.locator('.product-info>small').all_text_contents())
            page.reload(wait_until='domcontentloaded')
            expect(page.get_by_label('店铺范围')).to_have_value('b')
            expect(page.locator('.sidebar nav button.active')).to_contain_text('商品资料')
            page.get_by_role('button', name='今日工作', exact=True).click()
            page.get_by_label('工作日期', exact=True).fill('2026-10-16')
            page.get_by_label('工作日期', exact=True).dispatch_event('change')
            expect(page.locator('.ops-task').first).to_be_visible()
            checks.extend(['switch_shop_clears_filters_and_comparison', 'shop_scope_survives_reload'])
            page.get_by_role('button', name='店铺订单', exact=True).click()
            expect(page.locator('.order-card').first).to_be_visible()
            assert all('知织女装' in row for row in page.locator('.order-top').all_text_contents())
            with page.expect_response(lambda r: '/api/selection?' in r.url) as selection_response:
                page.get_by_role('button', name='选品研究', exact=True).click()
            selection_data = selection_response.value.json()
            if selection_data['items']:
                expect(page.locator('.rank-row').first).to_be_visible()
            else:
                expect(page.get_by_text('没有符合条件且资料完整的候选', exact=True)).to_be_visible()
                if selection_data['incomplete']:
                    expect(page.locator('.data-details')).to_contain_text('查看资料不足的候选')
                    assert all(row['score'] is None and row['missing'] for row in selection_data['incomplete'])
            expect(page.locator('table tbody tr').first).to_be_visible()
            checks.extend(['orders_follow_shop_scope', 'selection_and_competitor_facts'])
            test_text = '浏览器运营文案验收-' + uuid4().hex[:8]
            event_data = [('start', {'run_id': 'browser-mock'}), ('delta', {'text': test_text}), ('done', {'status': 'succeeded'})]
            body = ''.join(f'event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n' for event, data in event_data)
            page.route('**/api/chat', lambda route: route.fulfill(status=200, content_type='text/event-stream', body=body))
            page.get_by_label('给助手发送消息').fill('生成一段测试文案')
            page.get_by_label('发送消息', exact=True).click()
            expect(page.locator('.chat-message.assistant').last).to_contain_text(test_text)
            expect(page.get_by_label('给助手发送消息')).to_be_enabled()
            page.get_by_label('保存内容类型').select_option('marketing')
            with page.expect_response(lambda r: r.url.endswith('/api/content') and r.request.method == 'POST') as response:
                page.locator('.save-answer').get_by_role('button', name='保存回复', exact=True).click()
            saved = response.value.json()
            created_drafts.append(saved['id'])
            assert saved['kind'] == 'marketing' and saved['shop_id'] == 'b'
            page.get_by_role('button', name='文案与报告', exact=True).click()
            expect(page.locator('.draft-card').filter(has_text=test_text)).to_be_visible()
            expect(page.get_by_role('button', name='预览上架', exact=True)).to_have_count(0)
            checks.append('operator_saves_marketing_without_publishing')
            page.screenshot(path=str(shots / 'operator-content.png'), full_page=True)
            context.close()
            context, page = login(browser, mobile=True)
            close = page.get_by_role('button', name='关闭助手', exact=True)
            if close.is_visible():
                close.click()
            expect(page.locator('.metric-grid')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.screenshot(path=str(shots / 'operator-mobile.png'), full_page=True)
            checks.append('mobile_no_horizontal_overflow')
            context.close()
            browser.close()
        assert not errors, errors
        report = dict(status='passed', checks=checks, browser_errors=errors, real_model_requests=0)
        (ROOT / 'tests/artifacts/reports/operator-browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('Operations browser checks passed:', len(checks), '; JS errors:', len(errors))
    finally:
        if created_drafts:
            with SessionLocal() as db:
                db.execute(delete(Draft).where(Draft.id.in_(created_drafts), Draft.user_id == 'operator-a'))
                db.commit()
    

if __name__ == '__main__':
    main()

