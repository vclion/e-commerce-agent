"""Authorized candidates first. Vector similarity is only one ranking signal."""
import json
import re
import time
import jieba
import httpx
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from ..config import settings
from ..models import now
from .models import MemoryChunk, MemoryItem
from .policy import allowed_spaces, source_visible
from .service import effective_version
from . import embeddings

jieba.setLogLevel(40)


def tokenize(value):
    return ' '.join(t.casefold() for t in jieba.cut(value) if t.strip())


embedding = embeddings.embed


def search(db, actor, query, workspace='today', limit=8, semantic=True):
    began = time.perf_counter()
    spaces = [s.id for s in allowed_spaces(db, actor, workspace)]
    rows = list(db.scalars(select(MemoryItem).where(MemoryItem.space_id.in_(spaces),
        MemoryItem.status == 'active', (MemoryItem.expires_at.is_(None) | (MemoryItem.expires_at > now())))
        .order_by(MemoryItem.updated_at.desc()).limit(2000)))
    candidates = {}
    for row in rows:
        if not source_visible(db, actor, row, workspace):
            continue
        version = effective_version(db, row)
        if version:
            chunk = db.scalar(select(MemoryChunk).where(MemoryChunk.version_id == version.id))
            if chunk:
                candidates[chunk.id] = (row, version, chunk)
    if not candidates:
        return {'items': [], 'status': 'empty', 'timings': {'total_ms': round((time.perf_counter()-began)*1000, 2)}}
    scores, rankings = {}, []
    if not query:
        rankings.append(list(candidates)[:limit])
    else:
        terms = tokenize(query)
        lexical = db.execute(text("""SELECT id, ts_rank_cd(to_tsvector('simple', tokens), plainto_tsquery('simple', :q)) score
            FROM memory_chunks WHERE id=ANY(:ids) AND to_tsvector('simple',tokens) @@ plainto_tsquery('simple',:q)
            ORDER BY score DESC LIMIT 20"""), {'q': terms, 'ids': list(candidates)}).all()
        # OR token matches complement strict full-text AND queries and retain exact SKU identifiers.
        qs = set(terms.split())
        loose = sorted(candidates, key=lambda k: len(qs.intersection(candidates[k][2].tokens.split())), reverse=True)
        loose = [k for k in loose if qs.intersection(candidates[k][2].tokens.split())][:20]
        rankings.extend([[r.id for r in lexical], loose])
    status = 'lexical'
    embed_ms = 0.0
    if query and semantic:
        start = time.perf_counter()
        try:
            vector = embedding(query)
            if vector is not None:
                # Restrict to the already authorized, effective versions before exact distance ranking.
                with db.begin_nested():
                    hits = db.execute(text('''SELECT chunk_id FROM (SELECT * FROM memory_vectors
                        WHERE chunk_id=ANY(:ids) AND model=:model AND public.vector_dims(embedding)=:dimensions OFFSET 0) authorized
                        ORDER BY embedding OPERATOR(public.<=>) CAST(:vector AS public.vector) LIMIT 20'''),
                        {'ids': list(candidates), 'model': embeddings.identity(), 'dimensions':len(vector), 'vector': json.dumps(vector)}).scalars().all()
                rankings.append(hits); status = 'hybrid' if hits else 'semantic_pending'
            else:
                status = 'semantic_unconfigured' if settings.memory_embedding_enabled else 'semantic_disabled'
        except (httpx.HTTPError, ValueError, KeyError, SQLAlchemyError):
            status = 'semantic_unavailable'
        embed_ms = (time.perf_counter()-start)*1000
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking):
            scores[chunk_id] = scores.get(chunk_id, 0) + 1/(60+rank+1)
    found = []
    for chunk_id in sorted(scores, key=scores.get, reverse=True)[:limit]:
        row, version, chunk = candidates[chunk_id]
        found.append({'id': row.id, 'version_id': version.id, 'revision': version.revision, 'kind': row.kind,
            'title': row.title, 'content': version.content, 'valid_from': version.valid_from.isoformat(),
            'score': round(scores[chunk_id], 6), 'source_level': row.source_level})
    return {'items': found, 'status': status, 'timings': {'embedding_ms': round(embed_ms, 2),
        'total_ms': round((time.perf_counter()-began)*1000, 2)}}
