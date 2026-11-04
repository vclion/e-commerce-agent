"""Retrieval configuration and permissions at the Agent input boundary."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from commerce import knowledge, knowledge_retrieval
from commerce.config import settings


@pytest.fixture
def index(tmp_path, monkeypatch):
    (tmp_path / 'dify').mkdir()
    (tmp_path / 'knowledge').mkdir()
    records = {'public': {'id': 'public-ds', 'indexing_technique': 'economy'},
               'internal': {'id': 'internal-ds', 'indexing_technique': 'economy'}}
    manifest = [{'document_id': scope + '-doc', 'dataset_id': scope + '-ds', 'scope': scope,
                 'title': scope + '-title', 'version': 'v2'} for scope in records]
    (tmp_path / 'knowledge/manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(knowledge, 'ROOT', tmp_path)
    monkeypatch.setattr(knowledge_retrieval, 'ROOT', tmp_path)
    monkeypatch.setattr(settings, 'dify_public_dataset', 'public-ds')
    monkeypatch.setattr(settings, 'dify_internal_dataset', 'internal-ds')
    monkeypatch.setattr(settings, 'dify_knowledge_key', 'fixture-key')
    def save():
        (tmp_path / 'dify/knowledge.lock.json').write_text(json.dumps(records))
    save()
    return records, save


def segment(document_id, text='有来源的资料'):
    return {'segment': {'document_id': document_id, 'id': document_id + '-segment', 'content': text}, 'score': .9}


def fake_response(monkeypatch, records, capture):
    def post(url, **kwargs):
        capture.update(url=url, **kwargs)
        return httpx.Response(200, json={'records': records}, request=httpx.Request('POST', url))
    monkeypatch.setattr(knowledge.httpx, 'post', post)


def test_hybrid_uses_embedding_weighted_search_and_preserves_citations(index, monkeypatch):
    records, save = index
    records['public'].update(indexing_technique='high_quality', embedding_model_provider='fixture/provider',
                             embedding_model='fixture-embedding')
    save()
    capture = {}
    fake_response(monkeypatch, [segment('public-doc')], capture)
    result = knowledge.search(SimpleNamespace(role='operator'), '净收款算利润吗？')
    retrieval = capture['json']['retrieval_model']
    assert retrieval['search_method'] == 'hybrid_search' and retrieval['reranking_mode'] == 'weighted_score'
    assert retrieval['weights']['vector_setting']['embedding_model_name'] == 'fixture-embedding'
    assert retrieval['score_threshold_enabled'] and retrieval['top_k'] == 5
    assert result['items'][0]['source'] == '/api/knowledge/documents/public-doc'
    assert 'hybrid' in result['retrieval']


def test_economy_index_remains_usable_until_migration_is_complete(index, monkeypatch):
    capture = {}
    fake_response(monkeypatch, [], capture)
    result = knowledge.search(SimpleNamespace(role='operator'), '尺码')
    assert capture['json']['retrieval_model']['search_method'] == 'keyword_search'
    assert result['items'] == [] and 'economy' in result['retrieval']


def test_provider_cannot_mix_internal_or_foreign_documents_into_public_result(index, monkeypatch):
    capture = {}
    fake_response(monkeypatch, [segment('internal-doc', '内部资料'), segment('unknown-doc'),
                               {}, {'segment': None}, segment('public-doc')], capture)
    result = knowledge.search(SimpleNamespace(role='operator'), '运营资料', 'public')
    assert [row['document_id'] for row in result['items']] == ['public-doc']


def test_internal_permission_checked_before_any_provider_call(index, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized query reached retrieval provider')
    monkeypatch.setattr(knowledge.httpx, 'post', forbidden)
    with pytest.raises(HTTPException) as error:
        knowledge.search(SimpleNamespace(role='customer'), '退款口径', 'internal')
    assert error.value.status_code == 403


@pytest.mark.parametrize('changes', [{'id': 'wrong-dataset'}, {'indexing_technique': 'high_quality'}])
def test_inconsistent_index_fails_explicitly_before_query(index, monkeypatch, changes):
    records, save = index
    records['public'].update(changes)
    save()
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid index binding reached retrieval provider')
    monkeypatch.setattr(knowledge.httpx, 'post', forbidden)
    with pytest.raises(HTTPException) as error:
        knowledge.search(SimpleNamespace(role='operator'), '商品价格')
    assert error.value.status_code == 503


def test_provider_failure_is_not_reported_as_empty_success(index, monkeypatch):
    def unavailable(*args, **kwargs):
        raise httpx.ConnectTimeout('fixture timeout')
    monkeypatch.setattr(knowledge.httpx, 'post', unavailable)
    with pytest.raises(HTTPException) as error:
        knowledge.search(SimpleNamespace(role='operator'), '商品价格')
    assert error.value.status_code == 502


@pytest.mark.parametrize('fail_scope', [None, 'internal'])
def test_upgrade_switches_only_after_both_indexes_ready_and_restores_on_failure(tmp_path, fail_scope):
    from scripts.upgrade_knowledge import apply_upgrade
    lock = {scope: {'id': scope + '-ds', 'indexing_technique': 'economy'} for scope in ('public', 'internal')}
    manifest = [{'scope': scope, 'dataset_id': scope + '-ds', 'document_id': scope + '-doc'} for scope in lock]
    lock_path = tmp_path / 'knowledge.lock.json'
    original = json.dumps(lock)
    lock_path.write_text(original)
    patches, ready = [], []
    class Console:
        def request(self, method, path, **kwargs):
            if path.endswith('/models'):
                return {'data': [{'model': 'text-embedding-v3', 'status': 'active'}]}
            if method == 'GET':
                return {'id': path.rsplit('/', 1)[1], 'indexing_technique': 'economy',
                        'retrieval_model': {'search_method': 'keyword_search'}}
            patches.append((path, kwargs['json']))
            return {}
    def wait(console, record, docs):
        assert lock_path.read_text() == original, 'Query manifest switched before index readiness'
        assert len(docs) == 1
        ready.append(record['id'])
        if fail_scope and record['id'] == fail_scope + '-ds':
            raise TimeoutError('fixture indexing error')
    backup = tmp_path / 'backup'
    if fail_scope:
        with pytest.raises(TimeoutError):
            apply_upgrade(Console(), lock, manifest, lock_path, backup, wait, lambda record, docs: None)
        assert lock_path.read_text() == original
        assert [(path, payload['indexing_technique']) for path, payload in patches[-2:]] == [
            ('/datasets/internal-ds', 'economy'), ('/datasets/public-ds', 'economy')]
    else:
        result = apply_upgrade(Console(), lock, manifest, lock_path, backup, wait, lambda record, docs: None)
        assert ready == ['public-ds', 'internal-ds']
        assert all(record['indexing_technique'] == 'high_quality' for record in result.values())
        assert json.loads(lock_path.read_text()) == result
    assert json.loads((backup / 'index-settings.json').read_text())['lock'] == lock


def test_upgrade_requires_active_embedding_model_before_any_mutation(tmp_path):
    from scripts.upgrade_knowledge import apply_upgrade
    class Console:
        def request(self, method, path, **kwargs):
            assert method == 'GET'
            return {'data': []}
    with pytest.raises(RuntimeError, match='Configure'):
        apply_upgrade(Console(), {}, [], tmp_path / 'lock.json', tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_vector_readiness_requires_every_document_not_only_first(monkeypatch):
    from scripts import upgrade_knowledge as upgrade
    probes = []
    class Console:
        def request(self, *args, **kwargs):
            return {'data': [{'id': 'first', 'indexing_status': 'completed'},
                             {'id': 'missing-vector', 'indexing_status': 'completed'}]}
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json):
            probes.append(json['query'])
            records = [{'segment': {'document_id': 'first'}}] if json['query'] == 'first-title' else []
            return httpx.Response(200, json={'records': records}, request=httpx.Request('POST', 'http://fixture'))
    ticks = iter([0, 0, 2])
    monkeypatch.setattr(upgrade.httpx, 'Client', Client)
    monkeypatch.setattr(upgrade.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(upgrade.time, 'sleep', lambda seconds: None)
    record = {'id': 'ds', 'indexing_technique': 'high_quality',
              'embedding_model': 'fixture-model', 'embedding_model_provider': 'fixture-provider'}
    docs = [{'document_id': 'first', 'title': 'first-title'},
            {'document_id': 'missing-vector', 'title': 'missing-title'}]
    with pytest.raises(TimeoutError):
        upgrade.wait_for_vectors(Console(), record, docs, timeout=1)
    assert probes == ['first-title', 'missing-title']


def test_explicit_reindex_preserves_text_processing_rule_and_document_id(tmp_path, monkeypatch):
    from scripts import upgrade_knowledge as upgrade
    source = tmp_path / 'source.md'
    source.write_text('原有资料内容\n\n引用保持不变。')
    monkeypatch.setattr(upgrade, 'ROOT', tmp_path)
    requests = []
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, url, json):
            requests.append((url, json))
            return httpx.Response(200, json={'document': {'id': 'original-doc'}},
                                  request=httpx.Request('POST', 'http://fixture'))
    monkeypatch.setattr(upgrade.httpx, 'Client', Client)
    record = {'id': 'ds', 'indexing_technique': 'high_quality',
              'embedding_model': 'fixture-model', 'embedding_model_provider': 'fixture-provider'}
    upgrade.reindex_documents(record, [{'document_id': 'original-doc', 'name': 'original-name', 'file': 'source.md'}])
    url, payload = requests[0]
    assert '/documents/original-doc/update-by-text' in url
    assert payload['text'] == source.read_text() and payload['name'] == 'original-name'
    assert 'process_rule' not in payload, 'Existing Dify processing rules must be retained'
