"""Dify retrieval configuration shared by queries and index maintenance."""
import json
from .config import ROOT


def retrieval_model(record):
    if record.get('indexing_technique') != 'high_quality':
        return {'search_method': 'keyword_search', 'reranking_enable': False,
                'top_k': 5, 'score_threshold_enabled': False}
    provider, model = record.get('embedding_model_provider'), record.get('embedding_model')
    if not provider or not model:
        raise ValueError('High-quality knowledge index requires an embedding provider/model')
    return {'search_method': 'hybrid_search', 'reranking_enable': True,
            'reranking_mode': 'weighted_score', 'top_k': 5,
            'score_threshold_enabled': True, 'score_threshold': record.get('score_threshold', 0.45),
            'weights': {'vector_setting': {'vector_weight': 0.7,
                        'embedding_provider_name': provider, 'embedding_model_name': model},
                        'keyword_setting': {'keyword_weight': 0.3}}}


def active_index(scope, dataset_id):
    path = ROOT / 'dify/knowledge.lock.json'
    record = json.loads(path.read_text()).get(scope, {}) if path.exists() else {}
    if record.get('id') != dataset_id:
        raise ValueError('Knowledge dataset binding differs from its index manifest')
    return record
