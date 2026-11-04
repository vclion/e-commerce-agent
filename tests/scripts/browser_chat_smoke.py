"""Exercise real chat persistence with a temporary, isolated operator."""
import json
from pathlib import Path
from uuid import uuid4
import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete
from commerce.auth import password_hash
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import AccessToken, Audit, ChatRun, Conversation, Membership, User

ROOT = Path(__file__).resolve().parents[2]
(ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
actor = 'chatqa-' + uuid4().hex[:16]
errors, checks = [], []
with SessionLocal() as db:
    db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='对话记录验收',
                password_hash=password_hash(settings.demo_password)))
    db.flush()
    db.add(Membership(user_id=actor, shop_id='a'))
    db.commit()

try:
    with httpx.Client(base_url='http://127.0.0.1:8000') as client:
        response = client.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password})
        response.raise_for_status()
        token = response.json()['access_token']
        headers = {'Authorization': 'Bearer ' + token}
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto('http://127.0.0.1:8000')
            page.evaluate('(token) => sessionStorage.setItem("commerce-token", token)', token)
            print('Reload', flush=True)
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('.sidebar')).to_be_visible()
            expect(page.locator('.chat-welcome')).to_be_visible()
            query = '请查询MEN-001-01-M的商品事实，简短给出运营写文案时可以引用的面料和规格。'
            page.get_by_label('给助手发送消息').fill(query)
            with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST', timeout=130000) as pending:
                page.get_by_label('发送消息', exact=True).click()
            expect(page.get_by_label('给助手发送消息')).to_be_enabled(timeout=130000)
            runs = client.get('/api/chat/runs', headers=headers, params={'workspace':'today'}).json()['items']
            assert len(runs) == 1 and runs[0]['status'] == 'succeeded', runs
            cid = runs[0]['conversation_id']
            assert cid and runs[0]['answer']
            print('First reply saved',flush=True)
            checks.append('real_reply_persisted')

            print('Reload', flush=True)
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('.chat-message.user')).to_contain_text(query)
            expect(page.locator('.chat-message.assistant')).to_contain_text(runs[0]['answer'])
            checks.append('automatic_restore_after_reload')
            print('Restored history', flush=True)
            with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST', timeout=130000) as pending:
                page.get_by_label('给助手发送消息').fill('请根据上一条回复，用一句话概括需要提供的信息。')
                page.get_by_label('发送消息', exact=True).click()
            assert pending.value.request.post_data_json['conversation_id'] == cid
            expect(page.get_by_label('给助手发送消息')).to_be_enabled(timeout=130000)
            runs = client.get('/api/chat/runs', headers=headers, params={'conversation_id': cid, 'workspace':'today'}).json()['items']
            assert len(runs) == 2 and all(r['status'] == 'succeeded' for r in runs)
            checks.append('followup_uses_original_conversation')

            expect(page.get_by_role('button', name='新对话', exact=True)).to_have_count(0)
            expect(page.get_by_label('历史对话', exact=True)).to_have_count(0)
            page.reload(wait_until='domcontentloaded')
            expect(page.locator('.chat-message.user')).to_have_count(2)
            checks.append('continuous_history_without_switching')

            page.get_by_label('退出登录', exact=True).click()
            expect(page.locator('.login-page')).to_be_visible()
            page.get_by_label('运营账号', exact=True).fill('operator')
            page.locator('input[type=password]').fill(settings.demo_password)
            page.get_by_role('button', name='进入工作台').click()
            expect(page.get_by_label('店铺范围')).to_have_value('')
            expect(page.get_by_label('给助手发送消息')).to_be_enabled()
            assert query not in page.locator('.chat-panel').inner_text()
            checks.append('switching_user_does_not_leak_history')
            context.close(); browser.close()
    assert not errors, errors
    report = {'status': 'passed', 'checks': checks, 'browser_errors': errors,
              'real_model_requests': 2, 'temporary_user_removed': True}
    (ROOT / 'tests/artifacts/reports/chat-history-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Chat history checks passed:', len(checks), '; JS errors:', len(errors))
finally:
    with SessionLocal() as db:
        for cls in [Audit, ChatRun, Conversation, AccessToken, Membership]:
            db.execute(delete(cls).where(cls.user_id == actor))
        db.execute(delete(User).where(User.id == actor))
        db.commit()
