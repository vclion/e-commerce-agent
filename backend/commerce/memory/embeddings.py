"""API-only embeddings. Never loads a model or installs/downloads dependencies."""
import hashlib
import json
import math
from urllib.parse import urlsplit
import httpx
from ..config import settings


def configured():
    return bool(settings.memory_embedding_enabled and settings.memory_embedding_url.strip() and settings.memory_embedding_model.strip()
                and settings.memory_embedding_dimensions)


def endpoint():
    url = settings.memory_embedding_url.strip().rstrip('/')
    parts = urlsplit(url)
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('Invalid embedding API URL; use the separate API key setting')
    return url if parts.path.endswith('/embeddings') else url + '/embeddings'


def identity():
    # Keys may rotate without invalidating vectors; endpoint/model/revision/dimension may not.
    if not configured():
        return 'unconfigured'
    spec = [endpoint(), settings.memory_embedding_model, settings.memory_embedding_revision,
            settings.memory_embedding_dimensions]
    return 'api:' + hashlib.sha256(json.dumps(spec).encode()).hexdigest()


def embed(value, timeout=None):
    if not configured():
        return None
    headers = {}
    key = settings.memory_embedding_api_key.get_secret_value()
    if key:
        headers['Authorization'] = 'Bearer ' + key
    # No automatic retries or redirects: do not forward credentials to another endpoint.
    response = httpx.post(endpoint(), headers=headers, json={
        'model': settings.memory_embedding_model, 'input': value, 'encoding_format': 'float'},
        timeout=timeout or settings.memory_embedding_query_timeout, follow_redirects=False)
    response.raise_for_status()
    try:
        vector = response.json()['data'][0]['embedding']
        valid = (isinstance(vector, list) and len(vector) == settings.memory_embedding_dimensions
                 and all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                 and any(v != 0 for v in vector))
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        valid = False
    if not valid:
        raise ValueError('Embedding response must contain a finite, nonzero vector of the configured dimension')
    return vector
