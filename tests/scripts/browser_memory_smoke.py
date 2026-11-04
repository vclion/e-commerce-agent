"""Isolated browser CRUD/responsive acceptance. No external store writes."""
import json
from uuid import uuid4
import httpx
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import delete
from commerce.config import ROOT,settings
from commerce.auth import password_hash
from commerce.db import SessionLocal
from commerce.models import User,Membership,AccessToken,Audit,ChatRun,Conversation


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    (ROOT/'tests/artifacts/screenshots').mkdir(parents=True, exist_ok=True)
    actor='memoryqa-'+uuid4().hex[:14]
    report={'checks':[],'browser_errors':[],'screenshots':[]}
    with SessionLocal() as db:
        db.add(User(id=actor,username=actor,role='operator',shop_id='a',display_name='记忆验收',password_hash=password_hash(settings.demo_password)))
        db.flush();db.add_all([Membership(user_id=actor,shop_id=s) for s in ['a','b']]);db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',timeout=30) as api:
            login=api.post('/api/auth/login',json={'username':actor,'password':settings.demo_password}).json()
            headers={'Authorization':'Bearer '+login['access_token'],'X-Shop-ID':'a'}
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda e:report['browser_errors'].append(str(e)))
                page.goto('http://127.0.0.1:8000')
                page.evaluate('(token)=>{sessionStorage.setItem("commerce-token",token);sessionStorage.setItem("commerce-shop","a");sessionStorage.setItem("commerce-workspace","today")}',login['access_token'])
                page.reload(wait_until='domcontentloaded')
                page.get_by_role('button',name='记忆管理',exact=True).click()
                dialog=page.get_by_role('dialog',name='记忆管理')
                expect(dialog).to_be_visible()
                dialog.get_by_label('主题',exact=True).fill('报告排序-'+actor)
                dialog.get_by_label('标题',exact=True).fill('先看异常的报告偏好')
                dialog.get_by_label('内容',exact=True).fill('报告先列退款和履约异常，再给出行动建议。')
                dialog.get_by_role('button',name='保存记忆',exact=True).click()
                expect(dialog.locator('.memory-card')).to_have_count(1)
                report['checks'].append('create_persists_and_lists')
                dialog.get_by_role('button',name='查看来源 / 纠正',exact=True).click()
                expect(dialog.get_by_label('内容',exact=True)).to_have_value('报告先列退款和履约异常，再给出行动建议。')
                dialog.get_by_label('内容',exact=True).fill('先列库存异常，再给出行动建议。')
                dialog.get_by_role('button',name='保存记忆',exact=True).click()
                expect(dialog.locator('.memory-card')).to_contain_text('先列库存异常')
                dialog.get_by_role('button',name='查看来源 / 纠正',exact=True).click()
                expect(dialog.get_by_text('版本 2 · 用户纠正')).to_be_visible()
                expect(dialog.get_by_text('版本 1 · 明确保存')).to_be_visible()
                report['checks'].append('edit_keeps_revision_history_and_sources')
                dialog.get_by_label('长期记忆自动写入').uncheck()
                dialog.get_by_role('button',name='保存设置',exact=True).click()
                expect(dialog.locator('.memory-notice')).to_contain_text('设置已保存')
                assert not api.get('/api/memory/settings',headers=headers).json()['auto_write']
                report['checks'].append('settings_commit')
                for width in [1440,1024,390]:
                    page.set_viewport_size({'width':width,'height':1000 if width>400 else 844})
                    page.wait_for_timeout(150)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),width
                    assert dialog.evaluate('(e)=>e.scrollWidth<=e.clientWidth+1'),width
                    path=ROOT/f'tests/artifacts/screenshots/memory-ui-{width}.png'
                    page.screenshot(path=str(path),full_page=True)
                    report['screenshots'].append(str(path.relative_to(ROOT)))
                report['checks'].append('responsive_1440_1024_390_no_horizontal_overflow')
                dialog.get_by_role('button',name='忘记',exact=True).click()
                expect(dialog.locator('.memory-card')).to_have_count(0)
                expect(dialog.locator('.memory-notice')).to_contain_text('已停止使用')
                assert not api.get('/api/memory/items',headers=headers).json()['items']
                report['checks'].append('forget_removes_and_explains_retained_history')
                dialog.get_by_role('button',name='关闭记忆管理',exact=True).click()
                browser.close()
        assert not report['browser_errors'],report['browser_errors']
    finally:
        with SessionLocal() as db:
            for model in [Audit,AccessToken,ChatRun,Conversation,Membership]:
                db.execute(delete(model).where(model.user_id==actor))
            db.execute(delete(User).where(User.id==actor));db.commit()
        report['test_account_removed']=True
        (ROOT/'tests/artifacts/reports/memory-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
