"""Real Agent tool selection and local operations writes, with audit evidence."""
import json
from sqlalchemy import select
from commerce.db import SessionLocal
from commerce.models import Audit
from .acceptance import fixture_client, check_financials, write_report


def main():
    cases = [
        ('analysis', '用ops_overview查询最近7天当前全部授权店铺合计的支付成交额与期间退款额。请按“支付成交额：数字元；期间退款额：数字元”报告，保留两位小数，只使用已导入报表。', 'tool.ops_overview'),
        ('sop', '请调用ops_sop_generate，确保今天的每日SOP已生成，已有任务不要重复创建。简短告诉我执行结果。', 'tool.ops_sop_generate'),
        ('render', '先用ops_scripts读取TEST-SCRIPT-a的当前版本，再调用ops_render创建这个脚本的剪辑任务。不修改脚本，不向外部发布，报告实际任务ID和状态。', 'tool.ops_render')
    ]
    results = []
    with fixture_client() as (client, shops, end, token):
        headers = {'Authorization': 'Bearer ' + token}
        loaded = client.post('/api/ops/demo/import', json={}); loaded.raise_for_status()
        overview = client.get('/api/ops/overview'); overview.raise_for_status()
        expected = check_financials(overview.json(), shops)
        for label, query, required in cases:
            response = client.post('/api/chat', headers=headers, json={'query': query})
            response.raise_for_status()
            current, answer, status, run_id = '', '', '', ''
            for line in response.text.splitlines():
                if line.startswith('event:'):
                    current = line[6:].strip()
                elif line.startswith('data:'):
                    value = json.loads(line[5:].strip())
                    if current == 'start': run_id = value['run_id']
                    if current == 'delta': answer += value['text']
                    if current == 'replace': answer = value['text']
                    if current == 'done': status = value['status']
            with SessionLocal() as db:
                tools = list(db.scalars(select(Audit.operation).where(Audit.request_id == run_id, Audit.operation != 'tool.attempt')))
            item = dict(case=label, run_id=run_id, status=status, answer=answer, tools=tools, required_tool=required)
            results.append(item)
            write_report('operations-agent-report.json', {'cases': results, 'fixture_shops': list(shops)})
            print(label, status, tools, flush=True)
            assert status == 'succeeded' and required in tools, item
            if label == 'analysis':
                compact = answer.replace(',', '').replace(' ', '')
                for field in ('paid_fen', 'refund_fen'):
                    assert f'{expected["total"][field] / 100:.2f}' in compact, (field, answer)
    write_report('operations-agent-report.json', {'status': 'passed', 'cases': results,
                 'fixture_shops': list(shops), 'temporary_user_removed': True})
    print('Operations Agent: 3 real read/write cases passed.')


if __name__ == '__main__':
    main()
