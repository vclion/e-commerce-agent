"""Offline setup acceptance in a real browser. Temporary shops/user are removed afterward."""
import json
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete, select

from commerce.auth import password_hash
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import (User, Membership, PlatformConnection, PlatformShop, PlatformSync,
    OpsRecord, OpsTask, AccessToken, Audit)


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    actor = 'platform-browser-' + uuid4().hex[:10]
    checks, errors = [], []
    shots = ROOT / 'tests/artifacts/screenshots'
    shots.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='临时平台接入验收',
                    password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add(Membership(user_id=actor, shop_id='a'))
        db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=30) as api:
            result = api.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password})
            result.raise_for_status()
            api.headers['Authorization'] = 'Bearer ' + result.json()['access_token']
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
                page.get_by_role('button', name='平台接入', exact=True).click()
                expect(page.get_by_role('button', name='新增平台接入', exact=True)).to_be_enabled()
                page.get_by_role('button', name='新增平台接入', exact=True).click()
                page.get_by_label('店铺显示名称', exact=True).fill('正式店铺待授权')
                page.get_by_role('button', name='保存配置', exact=True).click()
                expect(page.locator('.connection')).to_contain_text('待填写配置')
                expect(page.locator('.connection')).to_contain_text('Secret待填写')
                checks.append('live_configuration_can_be_saved_without_credentials')
                page.screenshot(path=str(shots / 'platform-setup.png'), full_page=True)
                page.get_by_role('button', name='全店分析', exact=True).click()
                expect(page.locator('.ops-workspace')).to_contain_text('暂无数据')
                assert '443,315.00' not in page.locator('.ops-workspace').inner_text()
                assert '118,246.00' not in page.locator('.ops-workspace').inner_text()
                checks.append('empty_live_shop_does_not_fall_back_to_demo_financials')
                page.get_by_role('button', name='平台接入', exact=True).click()
                expect(page.get_by_role('button', name='无凭证演练', exact=True)).to_be_enabled()
                page.get_by_role('button', name='无凭证演练', exact=True).click()
                expect(page.locator('.sync-job').first).to_contain_text('同步完成', timeout=45000)
                checks.append('simulated_native_api_sync_runs_in_durable_worker')
                connections = api.get('/api/platform/connections').json()['items']
                simulated = next(r for r in connections if r['mode'] == 'simulation')
                live = next(r for r in connections if r['mode'] == 'live')
                assert live['missing'] and live['status'] == 'unconfigured'
                assert simulated['capabilities']['orders']['status'] == 'available'
                expect(page.get_by_label('店铺范围')).to_be_enabled()
                page.get_by_label('店铺范围').select_option(simulated['shop_id'])
                page.get_by_role('button', name='全店分析', exact=True).click()
                expect(page.locator('.metric-grid')).to_contain_text('1,393.00')
                expect(page.locator('.demo-badge')).to_contain_text('演练')
                checks.append('simulated_shop_scope_metrics_and_source_badge')
                page.get_by_role('button', name='商品资料', exact=True).click()
                expect(page.locator('.product-card')).to_have_count(1)
                checks.append('synchronized_products_reach_existing_catalog')
                page.get_by_role('button', name='平台接入', exact=True).click()
                expect(page.get_by_role('button', name='补充商品成本与规格', exact=True)).to_be_enabled()
                page.get_by_role('button', name='补充商品成本与规格', exact=True).click()
                page.get_by_label('采购成本（元，可空）', exact=True).fill('89.50')
                page.get_by_label('尺码', exact=True).fill('M')
                page.get_by_label('面料成分', exact=True).fill('100%棉')
                page.get_by_label('资料依据', exact=True).fill('合成供货单，仅用于接口演练验收')
                page.get_by_role('button', name='保存补充资料', exact=True).click()
                expect(page.get_by_role('status')).to_contain_text('已保存商品补充资料')
                with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/sync')) as queued:
                    page.get_by_role('button', name='立即同步', exact=True).click()
                next_job = queued.value.json()['id']
                expect(page.locator('.sync-job').filter(has_text=next_job)).to_contain_text('同步完成', timeout=45000)
                product = api.get(f'/api/platform/connections/{simulated["id"]}/products').json()['items'][0]
                assert product['cost_fen'] == 8950 and product['size'] == 'M' and product['material'] == '100%棉'
                checks.append('supplemental_facts_survive_resynchronization')
                page.screenshot(path=str(shots / 'platform-simulation.png'), full_page=True)
                page.reload(wait_until='domcontentloaded')
                expect(page.get_by_label('店铺范围')).to_have_value(simulated['shop_id'])
                page.get_by_role('button', name='平台接入', exact=True).click()
                expect(page.locator('.connection')).to_contain_text('抖店接口演练')
                checks.append('configuration_and_selected_shop_survive_reload')
                page.set_viewport_size({'width': 390, 'height': 844})
                close = page.get_by_role('button', name='关闭助手', exact=True)
                if close.is_visible():
                    close.click()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(shots / 'platform-mobile.png'), full_page=True)
                checks.append('mobile_layout_without_horizontal_overflow')
                context.close()
                browser.close()
            assert not errors, errors
            report = dict(status='passed', checks=checks, browser_errors=errors,
                real_platform_requests=0, real_model_requests=0,
                scope='offline API transport simulation; real platform authorization remains unverified')
            (ROOT / 'tests/artifacts/reports/platform-browser-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('Platform browser checks passed:', len(checks), '; JS errors:', len(errors))
    finally:
        with SessionLocal() as db:
            rows = list(db.scalars(select(PlatformConnection).where(PlatformConnection.user_id == actor)))
            ids, shops = [r.id for r in rows], [r.shop_id for r in rows]
            db.execute(delete(PlatformSync).where(PlatformSync.connection_id.in_(ids)))
            db.execute(delete(PlatformConnection).where(PlatformConnection.id.in_(ids)))
            db.execute(delete(OpsTask).where(OpsTask.shop_id.in_(shops)))
            db.execute(delete(OpsRecord).where(OpsRecord.shop_id.in_(shops)))
            db.execute(delete(Membership).where(Membership.user_id == actor))
            db.execute(delete(PlatformShop).where(PlatformShop.id.in_(shops)))
            db.execute(delete(AccessToken).where(AccessToken.user_id == actor))
            db.execute(delete(Audit).where(Audit.user_id == actor))
            db.execute(delete(User).where(User.id == actor))
            db.commit()


if __name__ == '__main__':
    main()
