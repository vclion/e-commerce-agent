"""Bounded per-request compaction. Never overwrites source chats or long memory."""
import re
from fastapi import HTTPException
from ..config import settings
from . import hierarchy as h, providers


class ContextLimitError(RuntimeError):
    def __init__(self, message='模型上下文超限，本轮未完成；请缩短输入或调整上下文额度'):
        super().__init__(message)


def is_context_limit(detail):
    value = str(detail or '').lower()
    return any(pattern in value for pattern in (
        'context_length_exceeded', 'maximum context length', 'context window exceeded',
        'context window is too', 'context length exceeded', 'context length limit',
        'exceeds the context', 'exceeded model context', 'input tokens exceed',
        '上下文过长', '上下文长度超', '上下文超限', '超出模型上下文')) or bool(
            re.search(r'(?:input|prompt).{0,30}(?:length|tokens).{0,30}(?:exceed|too long)', value))


def protected(record):
    # Preserve exact assistant literals as source data, not as verified business facts.
    pattern = (r'(?<![A-Za-z0-9_])[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)+(?![A-Za-z0-9_])|'
        r'(?<![A-Za-z0-9_])(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]+(?![A-Za-z0-9_])|'
        r'(?<![A-Za-z0-9_])[A-Z]{2,}(?![A-Za-z0-9_])|'
        r'[-+]?\d+(?:[.,:/-]\d+)*(?:万元|元|件|天|小时|分钟|秒|%)?')
    return {'run_id':record.id, 'turn':record.turn_seq, 'status':record.status,
        'user_original':record.query, 'actual_effects':record.effects,
        'assistant_literals_unverified':list(dict.fromkeys(re.findall(pattern,record.answer)))}


def frame(recent):
    older, latest = recent[:-1], recent[-1]
    return {'notice':'本轮临时摘要；用户原话和实际操作保持原样，助手字面值不等于已验证事实；细节可history_read回查',
        'summary_sources':[r.id for r in older], 'working_summary':'',
        'protected_originals':[protected(r) for r in older], 'latest_original':h.original(latest)}


def minimum_retry_budget(recent):
    # Summary minimum plus instruction/section overhead. Never sacrifice protected content to a ratio.
    return h.token_bound(h.dump(frame(recent))) + 256 + 1024 if len(recent)>1 else 0


def compact(recent, allowance):
    if not settings.memory_reactive_enabled or len(recent) < 2:
        raise HTTPException(413, '最新一轮完整原文已超出可用预算，无法继续压缩；请调整额度，原文未截断')
    older, latest = recent[:-1], recent[-1]
    value = frame(recent)
    available = min(settings.memory_reactive_summary_tokens, allowance - h.token_bound(h.dump(value)) - 32)
    if available < 256:
        raise HTTPException(413, '最新原文及关键约束已占满预算，无法安全压缩；请调整额度，原文未截断')
    payload = {'turns':[h.original(r) for r in older], 'target_tokens_upper_bound':available,
        'protected_originals':value['protected_originals']}
    if h.token_bound(h.dump(payload)) > min(settings.memory_reactive_input_tokens,
            settings.memory_model_context_tokens - settings.memory_reserved_tokens):
        raise HTTPException(413, '待压缩原文超过整理模型的安全输入额度，请调整额度或分开提问；原文未截断')
    try:
        result = providers.maintain('reactive_context', payload)
    except Exception as exc:
        raise HTTPException(503, '本轮上下文压缩暂不可用，请稍后重试；原文已保留') from exc
    if not result.summary.strip() or h.token_bound(result.summary) > available:
        raise HTTPException(413, '压缩结果仍超过可用预算，本轮已停止；未继续反复压缩，原文已保留')
    value['working_summary'] = result.summary
    if h.token_bound(h.dump(value)) > allowance:
        raise HTTPException(413, '压缩结果仍超过可用预算，请调整额度；原文已保留')
    return value, {'action':'summarize_recent','summary_calls':1,
        'summary_source_ids':[r.id for r in older], 'retained_latest_id':latest.id,
        'summary_tokens_upper_bound':h.token_bound(result.summary),
        'source_tokens_upper_bound':h.token_bound(h.dump(payload))}
