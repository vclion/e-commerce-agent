"""Sidebar isolation, in-flight navigation, and real-model tools with a temporary user."""
import json
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import delete, select

from commerce.auth import password_hash, scope_fingerprint, scoped_actor
from commerce.chat_workspaces import WORKSPACES
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import User, Membership, ChatRun, Conversation, AccessToken, Audit, Draft


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    actor = 'workspaceqa-' + uuid4().hex[:14]
    checks, errors, live_runs = [], [], []
    markers = {'today': '今日工作独立标记-' + actor, 'ops-videos': '短视频独立标记-' + actor,
               'general': '旧版历史独立标记-' + actor}
    with SessionLocal() as db:
        row = User(id=actor, username=actor, role='operator', shop_id='a', display_name='栏目会话验收',
                   password_hash=password_hash(settings.demo_password))
        db.add(row); db.flush()
        db.add_all([Membership(user_id=actor, shop_id=sid) for sid in ['a', 'b']]); db.commit()
        fingerprint = scope_fingerprint(scoped_actor(db, row, 'a'))
        for workspace, marker in markers.items():
            db.add(ChatRun(user_id=actor, shop_id='a', scope_fingerprint=fingerprint,
                           workspace=workspace, query=marker, answer=marker + '已恢复', status='succeeded'))
        db.add(ChatRun(user_id=actor, shop_id='b', scope_fingerprint=scope_fingerprint(scoped_actor(db, row, 'b')),
                       workspace='today', query='店铺B专属记录', answer='店铺B专属答复', status='succeeded'))
        db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=150) as client:
            auth = client.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password}).json()
            headers = {'Authorization': 'Bearer ' + auth['access_token'], 'X-Shop-ID': 'a'}
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(viewport={'width': 1600, 'height': 1000})
                page = context.new_page(); page.set_default_timeout(20000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://127.0.0.1:8000')
                page.evaluate('(token)=>{sessionStorage.setItem("commerce-token",token);sessionStorage.setItem("commerce-shop","a");sessionStorage.setItem("commerce-workspace","today")}', auth['access_token'])
                page.reload(wait_until='domcontentloaded')
                panel = page.locator('.chat-panel')

                def ready():
                    expect(page.locator('.sidebar nav button').first).to_be_enabled()
                    expect(page.get_by_label('给助手发送消息')).to_be_enabled()

                def go(workspace):
                    expect(page.get_by_role('button', name=WORKSPACES[workspace], exact=True)).to_be_enabled()
                    page.get_by_role('button', name=WORKSPACES[workspace], exact=True).click()
                    expect(panel).to_have_attribute('data-chat-workspace', workspace)
                    ready()

                ready(); expect(panel).to_contain_text(markers['today'])
                assert markers['ops-videos'] not in panel.inner_text()
                checks.append('initial_history_is_bound_to_module_and_shop')
                page.get_by_label('给助手发送消息').fill('今日工作的未发送草稿')
                go('ops-videos'); expect(panel).to_contain_text(markers['ops-videos'])
                assert markers['today'] not in panel.inner_text()
                page.get_by_label('给助手发送消息').fill('短视频的未发送草稿')
                go('today'); expect(page.get_by_label('给助手发送消息')).to_have_value('今日工作的未发送草稿')
                checks.append('module_history_and_unsent_input_restore_independently')
                for workspace in WORKSPACES:
                    if workspace != 'general': go(workspace)
                checks.append('all_ten_sidebar_modules_load_without_js_errors')
                go('ops-videos'); page.reload(wait_until='domcontentloaded'); ready()
                expect(panel).to_have_attribute('data-chat-workspace', 'ops-videos')
                expect(panel).to_contain_text(markers['ops-videos'])
                checks.append('reload_restores_active_module_and_its_history')
                go('today')
                page.get_by_label('店铺范围').select_option('b'); ready()
                expect(panel).to_contain_text('店铺B专属记录')
                assert markers['today'] not in panel.inner_text()
                page.get_by_label('店铺范围').select_option('a'); ready()
                expect(panel).to_contain_text(markers['today'])
                checks.append('shop_switch_restores_correct_module_history')
                page.get_by_role('button', name='旧版总对话', exact=True).click()
                expect(panel).to_contain_text(markers['general'])
                expect(page.get_by_label('给助手发送消息')).to_have_count(0)
                page.get_by_role('button', name='返回栏目对话', exact=True).click(); ready()
                expect(panel).to_contain_text(markers['today'])
                checks.append('legacy_history_is_preserved_separately_as_read_only')

                # Browser-only delayed stream tests the navigation race without changing real data.
                page.evaluate('''() => {
                  const realFetch = window.fetch;
                  window.fetch = async (url, options) => {
                    if (url === '/api/chat' && JSON.parse(options.body).query === '__workspace_stream_race__') {
                      const enc = new TextEncoder();
                      return new Response(new ReadableStream({start(controller) {
                        const send = (event, data) => controller.enqueue(enc.encode(`event: ${event}\\ndata: ${JSON.stringify(data)}\\n\\n`));
                        send('start', {run_id:'simulated-race'});
                        setTimeout(()=>send('delta', {text:'只属于今日工作的流式答复'}), 2200);
                        setTimeout(()=>{send('done', {status:'succeeded'});controller.close()}, 2600);
                      }}), {headers:{'Content-Type':'text/event-stream'}});
                    }
                    return realFetch(url, options);
                  };
                }''')
                page.get_by_label('给助手发送消息').fill('__workspace_stream_race__')
                page.get_by_label('发送消息', exact=True).click()
                go('ops-videos')
                expect(page.get_by_label('发送消息', exact=True)).to_be_disabled()
                expect(page.locator('.workspace-chat-running')).to_be_visible()
                expect(page.locator('.workspace-chat-running')).to_have_count(0, timeout=15000)
                assert '只属于今日工作的流式答复' not in panel.inner_text()
                go('today'); expect(panel).to_contain_text('只属于今日工作的流式答复')
                checks.append('inflight_reply_returns_to_originating_module_after_navigation')
                page.reload(wait_until='domcontentloaded'); ready()

                def send(query, workspace):
                    go(workspace)
                    before = {r['id'] for r in client.get('/api/chat/runs', headers=headers, params={'workspace':workspace}).json()['items']}
                    page.get_by_label('给助手发送消息').fill(query)
                    page.get_by_label('发送消息', exact=True).click()
                    expect(page.get_by_label('给助手发送消息')).to_be_enabled(timeout=150000)
                    rows = client.get('/api/chat/runs', headers=headers, params={'workspace':workspace}).json()['items']
                    run = next(r for r in rows if r['id'] not in before)
                    assert run['status'] == 'succeeded', {k:run[k] for k in ['id','status','error']}
                    with SessionLocal() as db:
                        ops = list(db.scalars(select(Audit.operation).where(Audit.request_id==run['id'])))
                    live_runs.append({'id':run['id'], 'workspace':workspace, 'status':run['status'], 'operations':ops})
                    print('Real model check:',workspace,run['status'],','.join(ops),flush=True)
                    return run, ops

                title = '栏目聊天真实工具验收-' + actor
                run, operations = send(f'请将以下文字保存为经营报告草稿。标题：{title}。内容：本报告仅用于栏目会话工具验收，不含真实经营结论。请调用content_save实际保存，完成后简短告诉我草稿ID。', 'drafts')
                assert 'tool.content_save' in operations, {'operations': operations, 'answer': run['answer']}
                assert any(effect['operation']=='content_save' for effect in run['effects'])
                drafts = client.get('/api/drafts', headers=headers).json()['items']
                assert any(row['payload']['title']==title for row in drafts)
                checks.append('real_model_selects_content_save_and_page_shows_saved_draft')
                expect(page.locator('.draft-card')).to_contain_text(title)
                expect(page.locator('.message-effects')).to_contain_text('已保存内容草稿')
                first_cid = run['conversation_id']
                followup, _ = send('继续上一条，请原样告诉我刚保存的报告标题，不要再次保存。', 'drafts')
                assert followup['conversation_id'] == first_cid and title in followup['answer']
                checks.append('real_followup_keeps_only_current_module_conversation')
                page.reload(wait_until='domcontentloaded'); ready()
                expect(page.locator('.message-effects')).to_contain_text('已保存内容草稿')
                checks.append('execution_receipts_persist_after_reload')
                _, operations = send('请先查询当前栏目和可用操作，再调用ops_assets查询已有素材。只简短报告当前店铺素材数量，不要生成脚本或剪辑。', 'ops-videos')
                assert 'tool.workspace_context' in operations and 'tool.ops_assets' in operations, operations
                checks.append('real_model_selects_new_assets_tool_in_video_module')

                screenshots = ROOT / 'tests/artifacts/screenshots'; screenshots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshots / 'workspace-chat-desktop.png'), full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                page.get_by_role('button',name='关闭助手',exact=True).click()
                page.get_by_role('button',name='切换智能助手',exact=True).click()
                expect(panel).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.screenshot(path=str(screenshots / 'workspace-chat-mobile.png'), full_page=True)
                checks.append('mobile_chat_keeps_module_identity_without_horizontal_overflow')
                context.close(); browser.close()
        assert not errors, errors
        report = {'status':'passed','checks':checks,'browser_errors':errors,'real_model_runs':live_runs,
                  'data':'temporary user, synthetic existing shop records; no live platform writes',
                  'delayed_stream_test':'browser fetch simulation', 'temporary_user_removed':True}
        (ROOT / 'tests/artifacts/reports/workspace-chat-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print('Workspace chat browser checks passed:',len(checks),'real model requests:',len(live_runs),'JS errors:',len(errors),flush=True)
    finally:
        with SessionLocal() as db:
            for model in [Draft, Audit, ChatRun, Conversation, AccessToken, Membership]:
                db.execute(delete(model).where(model.user_id==actor))
            db.execute(delete(User).where(User.id==actor)); db.commit()


if __name__ == '__main__':
    main()
