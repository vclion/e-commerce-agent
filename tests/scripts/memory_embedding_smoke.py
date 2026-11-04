"""Exercise the configured real embedding API and worker with disposable synthetic memories.

Uses existing dependencies only. Makes paid API requests; never fetches models or packages.
Credentials remain in the private .env and are excluded from the report and console output.
"""
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import delete, text

from commerce.auth import password_hash, scoped_actor
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import User, Membership, AccessToken, Audit, ChatRun, Conversation
from commerce.memory import embeddings, retrieval


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    if not embeddings.configured():
        raise SystemExit('Configure the embedding API before running this check.')
    actor = 'embedqa-' + uuid4().hex[:16]
    report = {
        'tested_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'synthetic_memories_real_embedding_api_no_store_platform_requests',
        'model': settings.memory_embedding_model,
        'dimensions': settings.memory_embedding_dimensions,
        'checks': [], 'searches': [], 'index_waits_ms': [],
        'cache': 'No application embedding/content cache; provider cache not controlled',
    }
    failed = False
    with SessionLocal() as db:
        db.add(User(id=actor, username=actor, role='operator', shop_id='a', display_name='向量验收',
                    password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add_all([Membership(user_id=actor, shop_id=shop) for shop in ['a', 'b']])
        db.commit()

    def require(response):
        if not response.is_success:
            raise RuntimeError(f'Local API returned HTTP {response.status_code}')
        return response.json()

    def wait_indexed(rows):
        versions = [row['versions'][0]['id'] for row in rows]
        began = time.monotonic()
        while time.monotonic() - began < 60:
            with SessionLocal() as db:
                count = db.scalar(text('''SELECT count(*) FROM memory_vectors x
                    JOIN memory_chunks c ON c.id=x.chunk_id WHERE c.version_id=ANY(:versions)
                    AND x.model=:model AND public.vector_dims(x.embedding)=:dimensions'''),
                    {'versions': versions, 'model': embeddings.identity(),
                     'dimensions': settings.memory_embedding_dimensions})
                failed_jobs = db.execute(text('''SELECT state,attempts,error FROM memory_jobs
                    WHERE user_id=:user_id AND source_id=ANY(:versions) AND error IS NOT NULL'''),
                    {'user_id': actor, 'versions': versions}).mappings().all()
                if failed_jobs:
                    report['index_errors'] = [dict(row) for row in failed_jobs]
                    raise RuntimeError('Embedding worker reported an API/index error; acceptance stopped')
            if count == len(versions):
                report['index_waits_ms'].append(round((time.monotonic()-began)*1000, 1))
                return
            time.sleep(.5)
        raise RuntimeError('Current memory versions were not indexed within 60 seconds')

    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=30) as api:
            token = require(api.post('/api/auth/login', json={
                'username': actor, 'password': settings.demo_password}))['access_token']
            headers = {'Authorization': 'Bearer '+token, 'X-Shop-ID': 'a'}
            if not require(api.get('/api/memory/settings', headers=headers))['semantic_configured']:
                raise RuntimeError('Restart the API/worker to load the embedding configuration')

            def create(title, content, shop='a'):
                return require(api.post('/api/memory/items', headers={**headers, 'X-Shop-ID': shop}, json={
                    'kind': 'episodic', 'scope': 'personal_shop', 'subject': uuid4().hex,
                    'title': title, 'content': content}))

            shipping = create('快递追踪', '物流停滞超过一天时，先联系仓库检查出库扫描，再向承运商催办。')
            refund = create('售后核对', '售后退款：核对订单凭证和买家申请，再记录处理意见，不直接执行打款。')
            private = create('仅限店乙', '店乙的报告约定只供店乙使用。', 'b')
            wait_indexed([shipping, refund, private])
            report['checks'].append('real_worker_persists_three_4096_dimension_vectors'
                                    if settings.memory_embedding_dimensions == 4096 else 'real_worker_persists_vectors')

            for query, expected in [('滞留包裹催揽收', shipping), ('无理由退货受理步骤', refund)]:
                began = time.perf_counter()
                result = require(api.post('/api/memory/search', headers=headers,
                    json={'query': query, 'workspace': 'today', 'limit': 2}))
                with SessionLocal() as db:
                    principal = scoped_actor(db, db.get(User, actor), 'a')
                    lexical = retrieval.search(db, principal, query, semantic=False)
                ids = [row['id'] for row in result['items']]
                report['searches'].append({'query': query, 'status': result['status'],
                    'total_http_and_control_ms': round((time.perf_counter()-began)*1000, 1),
                    'timings': result.get('timings'), 'ids': ids, 'expected_id': expected['id'],
                    'lexical_only_ids': [row['id'] for row in lexical['items']]})
                if result['status'] != 'hybrid' or not ids or ids[0] != expected['id']:
                    raise RuntimeError('Paraphrase retrieval did not rank the expected memory first')
                if private['id'] in ids:
                    raise RuntimeError('Another shop memory was returned')
            if not any(not row['lexical_only_ids'] for row in report['searches']):
                raise RuntimeError('The samples did not demonstrate recall beyond keyword matches')
            report['checks'].extend(['paraphrase_recall_beyond_keywords', 'cross_shop_vector_isolation'])

            changed = require(api.patch('/api/memory/items/'+shipping['id'], headers=headers, json={
                'expected_revision': shipping['revision'],
                'content': '物流异常优先核查仓库出库记录，并在当日联系承运商处理。'}))
            wait_indexed([changed])
            result = require(api.post('/api/memory/search', headers=headers,
                json={'query': '滞留包裹催揽收', 'workspace': 'today'}))
            match = next((row for row in result['items'] if row['id'] == shipping['id']), None)
            if result['status'] != 'hybrid' or not match or match['revision'] != 2:
                raise RuntimeError('Corrected memory did not use the current indexed revision')
            report['checks'].append('correction_uses_new_vector_version')

            require(api.post('/api/memory/forget', headers=headers, json={'item_ids': [shipping['id']]}))
            result = require(api.post('/api/memory/search', headers=headers,
                json={'query': '滞留包裹催揽收', 'workspace': 'today'}))
            if any(row['id'] == shipping['id'] for row in result['items']):
                raise RuntimeError('Forgotten memory was retrieved')
            with SessionLocal() as db:
                remaining = db.scalar(text('''SELECT count(*) FROM memory_vectors x
                    JOIN memory_chunks c ON c.id=x.chunk_id WHERE c.item_id=:id'''), {'id': shipping['id']})
            if remaining:
                raise RuntimeError('Forgotten memory still has vectors')
            report['checks'].append('forget_erases_vectors_and_excludes_recall')
    except Exception as exc:
        failed = True
        report['error_type'] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            report['failure_reason'] = str(exc)
        print('Embedding acceptance failed:', type(exc).__name__)
    finally:
        with SessionLocal() as db:
            for model in [Audit, AccessToken, ChatRun, Conversation, Membership]:
                db.execute(delete(model).where(model.user_id == actor))
            db.execute(delete(User).where(User.id == actor))
            db.commit()
        report['test_account_removed'] = True
        target = ROOT/'tests/artifacts/reports/memory-embedding-report.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'checks': report['checks'], 'test_account_removed': True}, ensure_ascii=False))
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
