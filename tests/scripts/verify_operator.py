"""Verify the deployed operations-only scope and one real marketing workflow."""
import json
import re
import httpx
from sqlalchemy import select

from commerce.auth import issue_token, scoped_actor
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import Audit, User, uid
from scripts.dify_console import DifyConsole, ROOT


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    report = {'scope': 'operator_only', 'retired_app_checks': []}
    console = DifyConsole()
    retired = json.loads((ROOT / 'tests/artifacts/reports/operator-retired-apps.json').read_text())
    for app in retired:
        actual = console.request('GET', f'/apps/{app["app_id"]}')
        assert actual['enable_api'] is False and actual['enable_site'] is False, app['name']
        report['retired_app_checks'].append({'name': app['name'], 'api_disabled': True, 'site_disabled': True})
    with httpx.Client(base_url='http://127.0.0.1:8000') as client:
        for username in ['customer-a', 'merchant-a']:
            response = client.post('/api/auth/login', json={'username': username, 'password': settings.demo_password})
            assert response.status_code in [401, 403], username
        health = client.get('/api/health').json()
        assert set(health['dify']) == {'operator'}
        report['old_logins_disabled'] = True
        report['health'] = health['status']
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    keys = json.loads((ROOT / '.runtime/dify-app-keys.json').read_text())
    run_id = uid()
    with SessionLocal() as db:
        context = issue_token(db, scoped_actor(db, db.get(User, 'operator-a'), 'a'), 'tool', run_id)
    app = apps['marketing-draft']
    response = httpx.post('http://127.0.0.1:8088/v1/workflows/run',
                         headers={'Authorization': 'Bearer ' + keys[app['app_id']]},
                         json={'inputs': {'context_ref': context, 'query': '写两版简短的通勤衬衫短视频口播草稿，引用面料事实，不提采购成本，不编造优惠。',
                                          'arguments': json.dumps({'product_id': 'MEN-001-01-M'})},
                               'user': 'operator-a', 'response_mode': 'blocking'}, timeout=120)
    response.raise_for_status()
    result = response.json()['data']
    assert result['status'] == 'succeeded', result.get('error')
    answer = result['outputs']['result']
    with SessionLocal() as db:
        tools = list(db.scalars(select(Audit.operation).where(Audit.request_id == run_id, Audit.operation != 'tool.attempt')))
    assert 'tool.product_detail' in tools
    assert re.search(r'100%\s*棉', answer), 'Expected the verified cotton composition'
    report['marketing_workflow'] = dict(status=result['status'], tools=tools, answer=answer,
                                        verified_material_fact=True, saved_or_published=False)
    (ROOT / 'tests/artifacts/reports/operator-consolidation-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Verified: retired app API/site disabled, legacy logins disabled, operator marketing workflow succeeded.')


if __name__ == '__main__':
    main()
