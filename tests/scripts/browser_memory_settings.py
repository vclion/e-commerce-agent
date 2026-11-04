"""Live settings round-trip and responsive UI check using an isolated local account."""
import json
from uuid import uuid4
import httpx
from playwright.sync_api import sync_playwright, expect
from sqlalchemy import delete
from commerce.config import ROOT, settings
from commerce.auth import password_hash
from commerce.db import SessionLocal
from commerce.models import User, Membership, AccessToken, Audit


def main():
    output=ROOT/'tests/artifacts/memory-settings'
    output.mkdir(parents=True,exist_ok=True)
    actor='settingsqa-'+uuid4().hex[:12]
    report={'checks':[], 'browser_errors':[], 'screenshots':[]}
    with SessionLocal() as db:
        db.add(User(id=actor,username=actor,role='operator',shop_id='a',
            display_name='设置验收',password_hash=password_hash(settings.demo_password)))
        db.flush();db.add(Membership(user_id=actor,shop_id='a'));db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000',timeout=30) as api:
            login=api.post('/api/auth/login',json={'username':actor,'password':settings.demo_password}).json()
            headers={'Authorization':'Bearer '+login['access_token'],'X-Shop-ID':'a'}
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda error:report['browser_errors'].append(str(error)))
                page.goto('http://127.0.0.1:8000')
                page.evaluate('(token)=>{sessionStorage.setItem("commerce-token",token);sessionStorage.setItem("commerce-shop","a");sessionStorage.setItem("commerce-workspace","today")}',login['access_token'])
                page.reload(wait_until='domcontentloaded')
                page.get_by_role('button',name='记忆管理',exact=True).click()
                dialog=page.get_by_role('dialog',name='记忆管理')
                auto=dialog.get_by_label('长期记忆自动写入',exact=True)
                indexing=dialog.get_by_label('上下文索引整理',exact=True)
                expect(auto).to_be_checked();expect(indexing).to_be_checked()
                for memory_on,index_on in [(False,True),(True,False),(False,False),(True,True)]:
                    auto.set_checked(memory_on);indexing.set_checked(index_on)
                    dialog.get_by_role('button',name='保存设置',exact=True).click()
                    expect(dialog.locator('.memory-notice')).to_have_text('设置已保存')
                    prefs=api.get('/api/memory/settings',headers=headers).json()
                    assert prefs['auto_write'] is memory_on and prefs['context_index_enabled'] is index_on
                    expect(dialog.get_by_label('整理经验候选',exact=True)).to_be_enabled(enabled=memory_on)
                    dialog.get_by_role('button',name='关闭记忆管理',exact=True).click()
                    page.get_by_role('button',name='记忆管理',exact=True).click()
                    expect(auto).to_be_checked(checked=memory_on)
                    expect(indexing).to_be_checked(checked=index_on)
                report['checks'].append('four_independent_combinations_persist_after_reopen')
                report['checks'].append('reflection_requires_automatic_memory_write')
                auto.uncheck()
                dialog.get_by_role('button',name='保存设置',exact=True).click()
                expect(dialog.locator('.memory-notice')).to_have_text('设置已保存')
                # Explicit user saves remain available with automatic writing switched off.
                dialog.get_by_label('主题',exact=True).fill('设置测试偏好')
                dialog.get_by_label('标题',exact=True).fill('手动保存的简洁回答偏好')
                dialog.get_by_label('内容',exact=True).fill('回答先给结论，再简要解释。')
                dialog.get_by_role('button',name='保存记忆',exact=True).click()
                expect(dialog.locator('.memory-card')).to_have_count(1)
                assert len(api.get('/api/memory/items',headers=headers).json()['items'])==1
                report['checks'].append('manual_memory_save_remains_available')
                for width in [1440,1024,390]:
                    page.set_viewport_size({'width':width,'height':1000 if width>400 else 844})
                    page.wait_for_timeout(150)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert dialog.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
                    expect(auto).to_be_visible();expect(indexing).to_be_visible()
                    image=output/f'settings-{width}.png'
                    page.screenshot(path=str(image),full_page=True)
                    report['screenshots'].append(str(image.relative_to(ROOT)))
                report['checks'].append('responsive_1440_1024_390_without_horizontal_overflow')
                browser.close()
        assert not report['browser_errors'],report['browser_errors']
    finally:
        with SessionLocal() as db:
            for model in [Audit,AccessToken,Membership]:
                db.execute(delete(model).where(model.user_id==actor))
            db.execute(delete(User).where(User.id==actor));db.commit()
        report['test_account_removed']=True
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()
