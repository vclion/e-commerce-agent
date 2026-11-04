import json
import httpx
from fastapi import HTTPException
from .auth import require_role
from .config import ROOT, settings
from .knowledge_retrieval import active_index, retrieval_model


def document_record(user, document_id):
    manifest = json.loads((ROOT / 'knowledge/manifest.json').read_text())
    record = next((d for d in manifest if d['document_id'] == document_id), None)
    if not record:
        raise HTTPException(404, '资料不存在或不可访问')
    if record['scope'] == 'internal':
        require_role(user, 'operator')
    return record


def search(user, query, scope='public'):
    if scope not in ['public', 'internal'] or not isinstance(query, str) or not 1 <= len(query) <= 500:
        raise HTTPException(422, '知识查询需要1～500字的问题，范围为public或internal')
    if scope == 'internal':
        require_role(user, 'operator')
    dataset_id = settings.dify_internal_dataset if scope == 'internal' else settings.dify_public_dataset
    if not dataset_id or not settings.dify_knowledge_key:
        raise HTTPException(503, '知识库尚未配置')
    try:
        index = active_index(scope, dataset_id)
        retrieval = retrieval_model(index)
    except (ValueError, OSError) as exc:
        raise HTTPException(503, '知识索引配置不一致，请联系管理员检查') from exc
    try:
        response = httpx.post(settings.dify_base_url.rstrip('/') + f'/datasets/{dataset_id}/retrieve',
            headers={'Authorization': 'Bearer ' + settings.dify_knowledge_key}, timeout=httpx.Timeout(10, connect=3),
            json={'query': query, 'retrieval_model': retrieval})
        response.raise_for_status()
        records = response.json().get('records', [])
        if not isinstance(records, list):
            raise ValueError('Invalid retrieval records')
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        raise HTTPException(502, 'Dify 知识检索暂不可用，请稍后重试') from exc
    # Apply the requested document scope before returning content to the Agent.
    # A provider returning a foreign document must not widen the requested scope.
    manifest = json.loads((ROOT / 'knowledge/manifest.json').read_text())
    allowed = {doc['document_id']: doc for doc in manifest
               if doc['scope'] == scope and doc['dataset_id'] == dataset_id}
    result = []
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get('segment'), dict):
            continue
        segment = item['segment']
        doc = allowed.get(segment.get('document_id'))
        if not doc or not isinstance(segment.get('content'), str) or not segment.get('id'):
            continue
        result.append({'text': segment['content'][:3000], 'segment_id': segment['id'],
                       'document_id': doc['document_id'], 'title': doc['title'], 'version': doc['version'],
                       'source': f'/api/knowledge/documents/{doc["document_id"]}', 'score': item.get('score')})
        if len(result) == 5:
            break
    label = 'Dify hybrid semantic and keyword search' if retrieval['search_method'] == 'hybrid_search' else 'Dify economy keyword search'
    return {'items': result, 'retrieval': label, 'is_demo': True}
