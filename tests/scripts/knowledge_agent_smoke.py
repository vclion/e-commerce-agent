"""One real Agent retrieval request and authenticated citation checks."""
import json
import time

from sqlalchemy import select
from commerce.config import ROOT
from commerce.db import SessionLocal
from commerce.models import Audit
from .acceptance import fixture_client, write_report


def main():
    with fixture_client() as (client, shops, end, token):
        query = ('请调用knowledge工具，scope为internal，检索“把退款扣掉后剩下的钱就是利润吗”。'
                 '请依据检索到的资料简短解释期间净收款与利润的区别，不查询其他业务数据，不保存草稿。')
        started = time.perf_counter()
        response = client.post('/api/chat', json={'query': query, 'workspace': 'ops-analysis'})
        response.raise_for_status()
        event, answer, status, run_id, sources = '', '', '', '', []
        for line in response.text.splitlines():
            if line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                value = json.loads(line[5:].strip())
                if event == 'start': run_id = value['run_id']
                elif event == 'delta': answer += value['text']
                elif event == 'replace': answer = value['text']
                elif event == 'sources': sources = value
                elif event == 'done': status = value['status']
        with SessionLocal() as db:
            tools = list(db.scalars(select(Audit.operation).where(Audit.request_id == run_id,
                                      Audit.operation != 'tool.attempt')))
        assert status == 'succeeded' and 'tool.knowledge' in tools, {'status': status, 'tools': tools, 'answer': answer}
        assert '净收款' in answer and '利润' in answer, answer
        manifest = json.loads((ROOT / 'knowledge/manifest.json').read_text())
        document = next(doc for doc in manifest if doc['title'] == '退款与净收款口径')
        citation = client.get('/api/knowledge/documents/' + document['document_id'])
        citation.raise_for_status()
        assert citation.json()['version'] == document['version']
        assert '期间净收款' in citation.json()['text']
        result = {'status': status, 'run_id': run_id, 'answer': answer, 'tools': tools, 'sources': sources,
                  'elapsed_seconds': round(time.perf_counter() - started, 2),
                  'authenticated_citation_document_id': document['document_id'],
                  'scope': 'synthetic knowledge; real Agent/model and embedding API; no platform writes'}
    result['temporary_user_removed'] = True
    write_report('knowledge-agent-report.json', result)
    print('Real Agent knowledge retrieval passed:', status, tools, flush=True)


if __name__ == '__main__':
    main()
