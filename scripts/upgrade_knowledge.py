"""Plan or apply an in-place Dify hybrid-index upgrade. Never downloads plugins.

Default is read-only. --apply requires an already installed/configured embedding
provider, backs up settings and keeps document IDs/citations unchanged. The local
query manifest is switched only after both datasets have usable vector indexes.
"""
import argparse
import json
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import httpx

from commerce.config import ROOT, settings
from commerce.knowledge_retrieval import retrieval_model
from .dify_console import DifyConsole

PROVIDER = 'langgenius/tongyi/tongyi'
MODEL = 'text-embedding-v3'


def upgrade_plan(console, lock, manifest):
    providers = console.request('GET', '/workspaces/current/model-providers')['data']
    available = any(row['provider'] == PROVIDER for row in providers)
    plan = {'provider': PROVIDER, 'model': MODEL, 'provider_installed': available,
            'downloads': False, 'datasets': {}}
    for scope in ('public', 'internal'):
        current = console.request('GET', f'/datasets/{lock[scope]["id"]}')
        assert current['id'] == lock[scope]['id'], 'Dataset identifier mismatch'
        plan['datasets'][scope] = {'id': current['id'], 'before': current.get('indexing_technique'),
                                   'after': 'high_quality',
                                   'documents': sum(doc['scope'] == scope and doc['dataset_id'] == current['id'] for doc in manifest)}
        assert plan['datasets'][scope]['documents'], 'Manifest has no documents for this dataset'
    return plan


def wait_for_vectors(console, record, documents, timeout=180):
    """Document status alone can precede the worker starting: also probe vectors."""
    expected = {doc['document_id'] for doc in documents}
    semantic = {**retrieval_model(record), 'search_method': 'semantic_search',
                'reranking_enable': False, 'score_threshold_enabled': False}
    with httpx.Client(base_url=settings.dify_base_url.rstrip('/'),
                      headers={'Authorization': 'Bearer ' + settings.dify_knowledge_key}, timeout=20) as client:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows, page = [], 1
            while True:
                data = console.request('GET', f'/datasets/{record["id"]}/documents', params={'page': page, 'limit': 100})
                rows.extend(data['data'])
                if not data.get('has_more'):
                    break
                page += 1
            selected = {row['id']: row for row in rows if row['id'] in expected}
            errors = [row['id'] for row in selected.values() if row.get('indexing_status') == 'error']
            if errors:
                raise RuntimeError('Embedding indexing failed for documents: ' + ','.join(errors))
            if set(selected) == expected and all(row.get('indexing_status') == 'completed' for row in selected.values()):
                ready = True
                for probe in documents:
                    response = client.post(f'/datasets/{record["id"]}/retrieve',
                                           json={'query': probe['title'], 'retrieval_model': semantic})
                    response.raise_for_status()
                    if not any(item.get('segment', {}).get('document_id') == probe['document_id']
                               for item in response.json().get('records', [])):
                        ready = False
                        break
                if ready:
                    return
            time.sleep(1)
    raise TimeoutError('Vector index not ready; query manifest has not been switched')


def reindex_documents(record, documents):
    """Explicitly rebuild unchanged texts after the dataset transaction commits.

    The pinned Dify dataset-change task can finish before the settings transaction
    is visible to its worker. Native document updates avoid trusting that task's
    success flag and retain each document ID and its original processing rules.
    """
    with httpx.Client(base_url=settings.dify_base_url.rstrip('/'),
                      headers={'Authorization': 'Bearer ' + settings.dify_knowledge_key}, timeout=30) as client:
        for doc in documents:
            response = client.post(f'/datasets/{record["id"]}/documents/{doc["document_id"]}/update-by-text',
                                   json={'name': doc['name'], 'text': (ROOT / doc['file']).read_text(),
                                         'indexing_technique': record['indexing_technique'],
                                         'embedding_model': record['embedding_model'],
                                         'embedding_model_provider': record['embedding_model_provider']})
            response.raise_for_status()
            if response.json().get('document', {}).get('id') != doc['document_id']:
                raise RuntimeError('Reindexing unexpectedly changed a document ID')


def apply_upgrade(console, lock, manifest, lock_path, backup_dir, wait_ready=wait_for_vectors,
                  reindex=reindex_documents):
    models = console.request('GET', f'/workspaces/current/model-providers/{PROVIDER}/models',
                             params={'model_type': 'text-embedding'})['data']
    if not any(row['model'] == MODEL and row.get('status') == 'active' for row in models):
        raise RuntimeError('Configure the installed Tongyi embedding provider before applying the upgrade')
    before = {scope: console.request('GET', f'/datasets/{row["id"]}') for scope, row in lock.items()}
    backup_dir.mkdir(parents=True, exist_ok=False)
    backup = backup_dir / 'index-settings.json'
    backup.write_text(json.dumps({'lock': lock, 'datasets': before}, ensure_ascii=False, indent=2))
    backup.chmod(0o600)
    updated = deepcopy(lock)
    changed = []
    try:
        for scope in ('public', 'internal'):
            record = updated[scope]
            record.update(indexing_technique='high_quality', embedding_model_provider=PROVIDER, embedding_model=MODEL)
            payload = {key: record[key] for key in ('indexing_technique', 'embedding_model_provider', 'embedding_model')}
            payload['retrieval_model'] = retrieval_model(record)
            # Record before the request, since a network timeout can follow a successful mutation.
            changed.append(scope)
            console.request('PATCH', f'/datasets/{record["id"]}', json=payload)
            docs = [doc for doc in manifest if doc['scope'] == scope and doc['dataset_id'] == record['id']]
            reindex(record, docs)
            wait_ready(console, record, docs)
        pending = lock_path.with_suffix('.pending.json')
        pending.write_text(json.dumps(updated, ensure_ascii=False, indent=2))
        pending.replace(lock_path)
    except Exception as error:
        rollback_errors = []
        for scope in reversed(changed):
            previous = before[scope]
            payload = {key: previous.get(key) for key in ('indexing_technique', 'embedding_model_provider', 'embedding_model', 'retrieval_model')}
            try:
                console.request('PATCH', f'/datasets/{previous["id"]}', json=payload)
            except Exception:
                rollback_errors.append(scope)
        if rollback_errors:
            raise RuntimeError('Index upgrade failed; manual settings restore required for ' + ','.join(rollback_errors)
                               + '; backup: ' + str(backup)) from error
        raise
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    lock_path = ROOT / 'dify/knowledge.lock.json'
    lock = json.loads(lock_path.read_text())
    manifest = json.loads((ROOT / 'knowledge/manifest.json').read_text())
    console = DifyConsole()
    plan = upgrade_plan(console, lock, manifest)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.apply:
        return
    if not plan['provider_installed']:
        raise SystemExit('Tongyi plugin is not installed. Obtain download approval before installing it.')
    backup_dir = ROOT / '.runtime/backups' / ('knowledge-hybrid-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    apply_upgrade(console, lock, manifest, lock_path, backup_dir)
    print('Hybrid index ready; original document IDs and dataset bindings preserved. Backup:', backup_dir)


if __name__ == '__main__':
    main()
