"""Run the published Product Match workflow through Dify's service API.

Exercises Dify Code + HTTP + backend authorization without requiring an LLM key.
"""
import json
from pathlib import Path
import httpx
from commerce.auth import issue_token, scoped_actor
from commerce.db import SessionLocal
from commerce.models import User, uid

ROOT = Path(__file__).resolve().parents[2]


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    keys = json.loads((ROOT / '.runtime/dify-app-keys.json').read_text())
    with SessionLocal() as db:
        context = issue_token(db, scoped_actor(db, db.get(User, 'operator-a'), 'a'), 'tool', uid())
    app = apps['product-match']
    response = httpx.post('http://127.0.0.1:8088/v1/workflows/run',
                         headers={'Authorization': 'Bearer ' + keys[app['app_id']]},
                         json={'inputs': {'context_ref': context, 'query': '', 'arguments': json.dumps({
                             'category': '衬衫', 'max_price_fen': 30000, 'size': 'L', 'material': '棉', 'limit': 3})},
                             'user': 'operator-a', 'response_mode': 'blocking'}, timeout=60)
    response.raise_for_status()
    data = response.json()['data']
    report = {'http_status': response.status_code, 'workflow_status': data['status'], 'error': data.get('error')}
    if data['status'] == 'succeeded':
        result = json.loads(data['outputs']['result'])
        report['tool_ok'] = result.get('ok')
        report['item_count'] = len(result.get('data', {}).get('items', []))
        assert result.get('ok') and report['item_count'] > 0, 'Tool did not return product results'
        for p in result['data']['items']:
            assert p['price_fen'] <= 30000 and p['specs']['尺码'] == 'L' and '棉' in p['specs']['面料'] and 'cost_fen' in p
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    (ROOT / 'tests/artifacts/reports/dify-smoke-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    assert data['status'] == 'succeeded', data.get('error')


if __name__ == '__main__':
    main()
