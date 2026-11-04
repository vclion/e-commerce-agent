"""Small real-model acceptance run, using each workbench's authenticated SSE API.

Does not submit or confirm business actions. It records observed responses,
elapsed time and actual tool calls; it is not a model-quality benchmark.
"""
import json
import argparse
import time
from pathlib import Path
import httpx
from sqlalchemy import select
from commerce.config import settings
from commerce.db import SessionLocal
from commerce.models import Audit, ChatRun

ROOT = Path(__file__).resolve().parents[2]


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser()
    parser.add_argument('--role', choices=['operator'])
    parser.add_argument('--append', action='store_true')
    parser.add_argument('--shop', choices=['a', 'b'], default='a')
    args = parser.parse_args()
    cases = [('operator', '查询最近7个完整自然日的经营数据，用两句话报告成交额、订单数和期间退款额。不要保存报告。')]
    report_path = ROOT / ('tests/artifacts/reports/live-model-report.json' if args.shop == 'a' else 'tests/artifacts/reports/live-model-report-b.json')
    results = json.loads(report_path.read_text()) if args.append and report_path.exists() else []
    with httpx.Client(base_url='http://127.0.0.1:8000', timeout=150) as client:
        for role, query in cases:
            if args.role and role != args.role:
                continue
            login = client.post('/api/auth/login', json={'username': role + '-' + args.shop, 'password': settings.demo_password})
            login.raise_for_status()
            headers = {'Authorization': 'Bearer ' + login.json()['access_token'], 'X-Shop-ID': args.shop}
            started = time.monotonic()
            overview = client.get('/api/ops/overview?days=7', headers=headers).json()
            imported = any(r['latest_date'] for r in overview['by_shop'])
            result = {'role': role, 'shop': args.shop, 'dataset_version': 'douyin-ops-v1' if imported else 'apparel-v1', 'query': query, 'answer': '', 'status': 'failed', 'sources': []}
            with client.stream('POST', '/api/chat', headers=headers, json={'query': query}) as response:
                response.raise_for_status()
                event = ''
                for line in response.iter_lines():
                    if line.startswith('event:'):
                        event = line[6:].strip()
                    elif line.startswith('data:'):
                        data = json.loads(line[5:].strip())
                        if event == 'start':
                            result['run_id'] = data['run_id']
                        elif event == 'conversation':
                            result['conversation_id'] = data['id']
                        elif event == 'delta':
                            result['answer'] += data['text']
                        elif event == 'replace':
                            result['answer'] = data['text']
                        elif event == 'sources':
                            result['sources'] = data
                        elif event == 'failure':
                            result['error'] = data['message']
                        elif event == 'done':
                            result['status'] = data['status']
            result['elapsed_seconds'] = round(time.monotonic() - started, 2)
            with SessionLocal() as db:
                result['tools'] = list(db.scalars(select(Audit.operation).where(
                    Audit.request_id == result.get('run_id'), Audit.operation.like('tool.%'),
                    Audit.operation != 'tool.attempt')))
            if role == 'operator' and result['status'] == 'succeeded':
                metrics = overview['total'] if imported else client.get('/api/analytics?days=7', headers=headers).json()
                from decimal import Decimal
                anchors = [f'{Decimal(metrics[field]) / Decimal(100):,.2f}'
                           for field in ['paid_fen', 'refund_fen']]
                result['grounding_check'] = {
                    'formatted_amounts_present': all(anchor in result['answer'] for anchor in anchors),
                    'order_count_present': str(metrics['paid_parent_orders'] if imported else metrics['order_count']) in result['answer']}
            results.append(result)
            report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(role, 'status:', result['status'], 'seconds:', result['elapsed_seconds'], 'tools:', result['tools'])
            if result.get('error'):
                print('  Failure:', result['error'])
    latest = {r['role']: r for r in results}
    if any(r['status'] != 'succeeded' or not r['answer'] or
           not all(r.get('grounding_check', {}).values()) for r in latest.values()):
        raise SystemExit('Some live calls failed. See tests/artifacts/reports/live-model-report.json and Dify run logs.')
    print('Selected real-model requests completed. Inspect the report before making quality claims.')


if __name__ == '__main__':
    main()
