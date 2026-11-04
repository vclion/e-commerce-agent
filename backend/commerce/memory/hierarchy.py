"""Tiered conversation context. Originals remain authoritative and recoverable.

Token accounting uses UTF-8 bytes as a conservative upper bound for byte-based
tokenizers. This is deliberately not advertised as an exact model token count.
"""
import json
import re
from sqlalchemy import select
from ..config import settings
from ..models import ChatRun
from .models import ContextSegment


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def token_bound(value):
    return len(value.encode('utf-8'))


def context_budget(query):
    return max(0, min(settings.memory_context_tokens,
        settings.memory_model_context_tokens - settings.memory_reserved_tokens - token_bound(query)))


def original(record):
    return {'run_id': record.id, 'turn': record.turn_seq, 'status': record.status,
        'user': record.query, 'assistant': record.answer, 'actual_effects': record.effects}


def light(record):
    """Only remove identical paragraphs and whitespace; retain numbers and wording."""
    value = original(record)
    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', record.answer) if p.strip()]
    value['assistant'] = '\n\n'.join(dict.fromkeys(paragraphs))
    value['compression'] = '仅去空白与完全重复段落；操作结果原样保留'
    return value


def key_points(records):
    """Extract original user clauses, never promote assistant suggestions to facts."""
    points = []
    for record in records:
        for clause in re.split(r'(?<=[。！？；\n])', record.query):
            quote = clause.strip()
            if quote and re.search(r'\d|必须|不要|不能|禁止|只允许|预算|约定|决定|确认|改为|取消|记住|默认|以后|待办|未完成|还没', quote):
                # Credentials are neither pinned nor summarized as durable facts.
                if re.search(r'(?i)(password|api[_ -]?key|access[_ -]?token|密码|密钥)\s*[:：=]', quote):
                    continue
                kind = 'constraint' if re.search(r'必须|不要|不能|禁止|只允许', quote) else (
                    'decision' if re.search(r'约定|决定|确认|改为|取消|记住|默认|以后|待办|未完成|还没', quote) else 'number')
                points.append({'run_id': record.id, 'turn': record.turn_seq, 'kind': kind, 'quote': quote})
    # Identical recurring clauses must not crowd an older, unique budget/constraint out.
    return list({point['quote']: point for point in points}.values())


def relevance(query, value):
    # Dependency-free matching, including Chinese phrases and exact identifiers.
    terms = re.findall(r'[a-zA-Z0-9_-]+|[\u4e00-\u9fff]', query.casefold())
    pairs = [query[i:i+2] for i in range(len(query)-1) if '\u4e00' <= query[i] <= '\u9fff']
    text = value.casefold()
    return sum(term in text for term in set(terms + pairs))


def records_for(db, session, before_seq):
    return list(db.scalars(select(ChatRun).where(ChatRun.session_id == session.id,
        ChatRun.user_id == session.user_id, ChatRun.shop_id == session.shop_id,
        ChatRun.workspace == session.workspace, ChatRun.scope_fingerprint == session.scope_fingerprint,
        ChatRun.turn_seq < before_seq, ChatRun.memory_excluded.is_(False),
        ChatRun.status != 'running').order_by(ChatRun.turn_seq)))


def segments_for(db, session, records):
    visible = {r.id for r in records}
    return [s for s in db.scalars(select(ContextSegment).where(
        ContextSegment.session_id == session.id, ContextSegment.epoch == session.epoch)
        .order_by(ContextSegment.start_seq)) if s.source_ids and set(s.source_ids).issubset(visible)]


def compression_source(db, session, before_seq):
    records = records_for(db, session, before_seq + 1)
    segments = segments_for(db, session, records)
    covered = {rid for segment in segments for rid in segment.source_ids}
    keep = settings.memory_recent_turns
    older = records[:-keep] if keep else records
    pending = [r for r in older if r.id not in covered]
    if len(pending) < settings.memory_segment_turns:
        return None
    # Bound maintenance inputs without cutting any original message in half.
    selected, used = [], 0
    for record in pending:
        size = token_bound(dump(original(record)))
        if size > 80000:
            continue  # A single giant message must not block compaction of later turns.
        if used + size > 80000:
            break
        selected.append(record); used += size
        if len(selected) == settings.memory_segment_turns:
            break
    if not selected:
        return None  # Oversized originals stay available through history_read.
    return {'session_id': session.id, 'session_epoch': session.epoch,
        'source_ids': [r.id for r in selected], 'start_seq': selected[0].turn_seq,
        'last_seq': selected[-1].turn_seq, 'prior_summary': '', 'reflection': False,
        'turns': [{'source_id': r.id, 'turn': r.turn_seq, 'user': r.query,
            'assistant': r.answer, 'status': r.status, 'effects': r.effects} for r in selected],
        'key_points': key_points(selected)}


def assemble(db, session, run, entries, temporary=False):
    from .indexes import assemble as indexed_assemble
    return indexed_assemble(db, session, run, entries, temporary)
