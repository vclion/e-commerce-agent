"""Reproduce the reported prompt, verify retry and a real product-copy response."""
import json
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete

from commerce.auth import password_hash
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import AccessToken, Audit, ChatRun, Conversation, Membership, User


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    actor = 'chatfix-' + uuid4().hex[:12]
    checks, errors = [], []
    with SessionLocal() as db:
        db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='对话修复验收',
                    password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add(Membership(user_id=actor, shop_id='a'))
        db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=130) as client:
            result = client.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password})
            result.raise_for_status()
            token = result.json()['access_token']
            headers = {'Authorization': 'Bearer ' + token}
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                context.route('https://fonts.googleapis.com/**', lambda route: route.fulfill(status=200, body=''))
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8000', wait_until='domcontentloaded')
                page.evaluate('(token)=>sessionStorage.setItem("commerce-token",token)', token)
                page.reload(wait_until='domcontentloaded')
                expect(page.get_by_label('给助手发送消息', exact=True)).to_be_enabled()
                # A cut stream must preserve the partial text, show retry, and stay unsavable.
                partial = ('event: start\r\ndata: {"run_id":"cut-stream"}\r\n\r\n'
                           'event: delta\r\ndata: {"text":"已收到的部分内容"}\r\n\r\n')
                page.route('**/api/chat', lambda route: route.fulfill(status=200, content_type='text/event-stream', body=partial), times=1)
                page.get_by_label('给助手发送消息', exact=True).fill('帮我生成文案')
                page.get_by_label('发送消息', exact=True).click()
                expect(page.locator('.chat-message.assistant.error')).to_contain_text('已收到的部分内容')
                expect(page.locator('.chat-message.assistant.error')).to_contain_text('回复连接中断')
                expect(page.get_by_role('button', name='保存回复', exact=True)).to_have_count(0)
                expect(page.get_by_role('button', name='重试这条消息', exact=True)).to_be_enabled()
                checks.append('cut_stream_keeps_partial_answer_and_offers_retry')
                with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST', timeout=130000) as pending:
                    page.get_by_role('button', name='重试这条消息', exact=True).click()
                assert pending.value.request.post_data_json['query'] == '帮我生成文案'
                expect(page.get_by_label('给助手发送消息', exact=True)).to_be_enabled(timeout=130000)
                runs = client.get('/api/chat/runs', headers=headers, params={'workspace':'today'}).json()['items']
                assert len(runs) == 1 and runs[0]['status'] == 'succeeded', [{k:r.get(k) for k in ['status','error']} for r in runs]
                assert runs[0]['answer'].strip()
                conversation = runs[0]['conversation_id']
                checks.append('reported_generic_prompt_succeeds_through_real_dify')
                page.get_by_label('给助手发送消息', exact=True).fill('给MEN-001-01-M生成一段不超过80字的抖音口播文案，先查询商品事实，不编造优惠。')
                with page.expect_response(lambda r: r.url.endswith('/api/chat') and r.request.method == 'POST', timeout=130000) as pending:
                    page.get_by_label('发送消息', exact=True).click()
                expect(page.get_by_label('给助手发送消息', exact=True)).to_be_enabled(timeout=130000)
                assert pending.value.request.post_data_json['conversation_id'] == conversation
                runs = client.get('/api/chat/runs', headers=headers, params={'workspace':'today'}).json()['items']
                assert len(runs) == 2 and all(r['status'] == 'succeeded' for r in runs), [{k:r.get(k) for k in ['status','error']} for r in runs]
                assert '棉' in runs[0]['answer']
                checks.append('followup_generates_product_copy_from_actual_tools')
                page.reload(wait_until='domcontentloaded')
                expect(page.locator('.chat-message.assistant')).to_have_count(2)
                expect(page.locator('.chat-message.assistant.error')).to_have_count(0)
                expect(page.get_by_role('button', name='保存回复', exact=True)).to_be_enabled()
                checks.append('complete_replies_restore_after_reload')
                shots = ROOT / 'tests/artifacts/screenshots'
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / 'chat-recovered.png'), full_page=False)
                context.close()
                browser.close()
        assert not errors, errors
        report = dict(status='passed', checks=checks, browser_errors=errors, real_chat_requests=2,
                      simulated_cut_streams=1, temporary_user_removed=True)
        (ROOT / 'tests/artifacts/reports/chat-recovery-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print('Chat recovery checks passed:', len(checks), '; real chats: 2')
    finally:
        with SessionLocal() as db:
            for cls in [Audit, ChatRun, Conversation, AccessToken, Membership]:
                db.execute(delete(cls).where(cls.user_id == actor))
            db.execute(delete(User).where(User.id == actor))
            db.commit()


if __name__ == '__main__':
    main()
