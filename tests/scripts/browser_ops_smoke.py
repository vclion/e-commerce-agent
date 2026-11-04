"""Browser acceptance of imported operations and actual local media."""
import json
import re
from playwright.sync_api import expect, sync_playwright
from .acceptance import (fixture_client, fixture_rows, check_financials,
                         write_report, SHOTS as shots, ROOT)

errors = []


def login(browser, token, mobile=False):
    context = browser.new_context(viewport={'width': 390 if mobile else 1440, 'height': 844 if mobile else 1000})
    context.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, content_type='text/css', body=''))
    page = context.new_page()
    page.set_default_timeout(20000)
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8000', wait_until='domcontentloaded')
    page.evaluate('(token)=>{sessionStorage.setItem("commerce-token",token);sessionStorage.setItem("commerce-shop","");sessionStorage.setItem("commerce-workspace","today")}', token)
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('.sidebar')).to_be_visible()
    expect(page.get_by_label('店铺范围')).to_be_enabled()
    return context, page


def run(client, shops, end, token):
    checks, retired_requests = [], []
    errors.clear()
    shots.mkdir(parents=True, exist_ok=True)
    response = client.post('/api/ops/demo/import', json={}); response.raise_for_status()
    response = client.get('/api/ops/overview', params={'end': end}); response.raise_for_status()
    expected = check_financials(response.json(), shops)
    response = client.post('/api/ops/tasks/generate', json={'date': end}); response.raise_for_status()
    task_count = len(response.json()['items'])
    assert task_count > 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context, page = login(browser, token)
        page.on('request', lambda r: retired_requests.append(r.url) if re.search(r'/api/ops/(issues|effects)(?:[/?]|$)|/publication(?:[/?]|$)', r.url) else None)
        for label in ['问题跟进', '效果复盘']:
            expect(page.get_by_role('button', name=label, exact=True)).to_have_count(0)
        page.get_by_label('工作日期', exact=True).fill(end)
        page.get_by_label('工作日期', exact=True).dispatch_event('change')
        checks.append('retired_module_navigation_removed')
        expect(page.locator('.ops-task').first).to_be_visible()
        expect(page.get_by_label('店铺范围')).to_be_enabled()
        expect(page.locator('.ops-task')).to_have_count(task_count)
        page.get_by_role('button', name='生成今日SOP', exact=True).click()
        expect(page.get_by_role('status')).to_contain_text('新增 0 项')
        expect(page.locator('.ops-task')).to_have_count(task_count)
        page.get_by_role('button', name='跟进任务', exact=True).first.click()
        expect(page.get_by_label('完成证据', exact=True)).to_be_visible()
        page.get_by_label('关闭任务', exact=True).click()
        page.screenshot(path=str(shots / 'ops-today.png'), full_page=True)
        checks.extend(['today_tasks_and_idempotent_sop', 'task_evidence_form_and_schedules'])

        page.get_by_role('button', name='全店分析', exact=True).click()
        expect(page.locator('.metric-grid')).to_contain_text(f'{expected["total"]["paid_fen"] / 100:,.2f}')
        expect(page.locator('.ops-workspace table tbody tr')).to_have_count(len(shops))
        page.screenshot(path=str(shots / 'ops-analysis.png'), full_page=True)
        expect(page.get_by_label('店铺范围')).to_be_enabled()
        selected_shop = shops[-1]
        page.get_by_label('店铺范围').select_option(selected_shop)
        expect(page.locator('.metric-grid')).to_contain_text(f'{expected["by_shop"][selected_shop]["paid_fen"] / 100:,.2f}')
        expect(page.locator('.ops-workspace table tbody tr')).to_have_count(1)
        page.reload(wait_until='domcontentloaded')
        expect(page.get_by_label('店铺范围')).to_have_value(selected_shop)
        expect(page.locator('.chat-panel')).to_have_attribute('data-chat-workspace', 'ops-analysis')
        page.get_by_role('button', name='今日工作', exact=True).click()
        page.get_by_label('工作日期', exact=True).fill(end)
        page.get_by_label('工作日期', exact=True).dispatch_event('change')
        expect(page.locator('.ops-task').first).to_be_visible()
        expect(page.get_by_label('店铺范围')).to_be_enabled()
        page.get_by_label('店铺范围').select_option('')
        expect(page.get_by_label('店铺范围')).to_be_enabled()
        checks.extend(['imported_financial_ground_truth_in_ui', 'shop_scope_and_reload'])

        page.get_by_role('button', name='数据导入', exact=True).click()
        expect(page.get_by_role('combobox', name='报表类型', exact=True)).to_be_visible()
        expect(page.get_by_label('报表文件', exact=True)).to_be_enabled()
        page.get_by_label('报表文件', exact=True).set_input_files(str(ROOT / 'data/douyin-ops-v1/daily_reports.csv'))
        page.get_by_role('button', name='校验报表', exact=True).click()
        expect(page.locator('.ops-workspace')).to_contain_text(f'有效 {len(fixture_rows("daily_reports"))} 行')
        page.get_by_role('button', name='确认导入', exact=True).click()
        expect(page.get_by_role('status')).to_contain_text(re.compile('已写入|没有重复计数'))
        row = json.loads((ROOT / 'data/douyin-ops-v1/daily_reports.jsonl').read_text().splitlines()[0])
        row['paid_fen'] = -1
        expect(page.get_by_label('报表文件', exact=True)).to_be_enabled()
        page.get_by_label('报表文件', exact=True).set_input_files({'name':'invalid.json', 'mimeType':'application/json', 'buffer':json.dumps([row]).encode()})
        page.get_by_role('button', name='校验报表', exact=True).click()
        expect(page.locator('.ops-workspace')).to_contain_text('paid_fen 不能为负数')
        expect(page.get_by_role('button', name='确认导入', exact=True)).to_be_disabled()
        checks.extend(['report_upload_preview_commit', 'invalid_rows_prevent_commit'])

        page.get_by_role('button', name='短视频运营', exact=True).click()
        video_count = sum(row['shop_id'] in shops for row in fixture_rows('videos'))
        expect(page.locator('.ops-workspace table tbody tr')).to_have_count(video_count)
        assert '暂无数据' in page.locator('.ops-workspace table').inner_text()
        page.get_by_role('button', name='编辑脚本', exact=True).first.click()
        expect(page.locator('.modal fieldset')).to_have_count(3)
        expect(page.get_by_label('字幕', exact=True).first).to_be_visible()
        page.get_by_label('关闭脚本', exact=True).click()
        page.screenshot(path=str(shots / 'ops-videos.png'), full_page=True)
        page.get_by_role('button', name='预览成片', exact=True).first.click()
        video = page.locator('.ops-video-modal video')
        expect(video).to_be_visible()
        video.evaluate('(v) => v.play()')
        page.wait_for_function('document.querySelector(".ops-video-modal video").readyState >= 2')
        assert video.evaluate('(v) => v.duration') > 10
        video.evaluate('(v) => {v.pause();v.currentTime=1}')
        page.screenshot(path=str(shots / 'ops-render.png'), full_page=False)
        page.get_by_label('关闭视频', exact=True).click()
        checks.extend(['video_metrics_and_missing_values', 'visual_script_editor', 'authenticated_render_plays_in_browser'])

        page.get_by_role('button', name='店铺订单', exact=True).click()
        expect(page.locator('.order-card').first).to_contain_text('TEST-')
        checks.append('orders_use_imported_data')
        context.close()
        context, page = login(browser, token, mobile=True)
        page.get_by_label('工作日期', exact=True).fill(end)
        page.get_by_label('工作日期', exact=True).dispatch_event('change')
        close = page.get_by_role('button', name='关闭助手', exact=True)
        if close.is_visible(): close.click()
        expect(page.locator('.ops-task').first).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(shots / 'ops-mobile.png'), full_page=True)
        checks.append('mobile_no_horizontal_overflow')
        context.close()
        browser.close()
    assert not errors, errors
    assert not retired_requests, retired_requests
    checks.append('no_retired_module_requests')
    result = dict(status='passed', checks=checks, browser_errors=errors, real_model_requests=0)
    result['fixture_shops'] = list(shops)
    write_report('operations-browser-report.json', result)
    print('Operations browser checks passed:', len(checks), '; JS errors:', len(errors))


def main():
    with fixture_client() as args:
        run(*args)

if __name__ == '__main__':
    main()
