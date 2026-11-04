"""Recover transport failures without replaying possibly committed business writes.

Existing business handlers retain their transactions. A journal marks every handler
commit as uncertain until its result is durably recorded. A crash in that gap is
reported for reconciliation rather than guessed to be safe to retry.
"""
import asyncio
import hashlib
import json
import logging
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.dialects.postgresql import insert

from .config import settings
from .db import SessionLocal
from .models import AccessToken, Audit, ChatRun, ToolExecution, now, uid
from .tool_runtime import execution_effects

logger = logging.getLogger('commerce.recovery')


def write_operations():
    from . import ops_tools
    from .chat_workspaces import WRITE_OPERATIONS
    from .memory import tools
    return ops_tools.WRITE_OPERATIONS | WRITE_OPERATIONS | tools.WRITE_OPERATIONS


def fingerprint(arguments):
    # Keep a digest, not a second copy of user input or credentials.
    value = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()


def tool_record(db, run_id, user_id, operation, arguments):
    return db.scalar(select(ToolExecution).where(ToolExecution.run_id == run_id,
        ToolExecution.user_id == user_id, ToolExecution.operation == operation,
        ToolExecution.arguments_hash == fingerprint(arguments)))


def cached_result(record):
    if record.state == 'succeeded':
        return record.result
    if record.state == 'failed':
        raise HTTPException(409, '该操作此前失败，请核对参数后重新提问；本次未重复执行')
    raise HTTPException(409, '该操作正在执行或提交结果待核对，请查看运行记录；本次未重复执行')


def reserve_tool(db, run_id, user_id, operation, arguments):
    # Older independent workflow contexts have no local ChatRun; retain their contract.
    if not db.get(ChatRun, run_id):
        return None, False
    values = dict(id=uid(), run_id=run_id, user_id=user_id, operation=operation,
        arguments_hash=fingerprint(arguments), state='running', created_at=now(), updated_at=now())
    inserted = db.scalar(insert(ToolExecution).values(**values).on_conflict_do_nothing(
        index_elements=['run_id', 'operation', 'arguments_hash']).returning(ToolExecution.id))
    db.commit()
    return tool_record(db, run_id, user_id, operation, arguments), bool(inserted)


def track_commits(db, record):
    if record is None:
        return lambda: None

    def before_commit(session):
        # This marker and any business mutations commit in the same transaction.
        # A metadata-only commit is conservatively treated the same way.
        record.state, record.updated_at = 'uncertain', now()

    event.listen(db, 'before_commit', before_commit)

    def release():
        if event.contains(db, 'before_commit', before_commit):
            event.remove(db, 'before_commit', before_commit)
    return release


def fail_tool(record_id):
    if not record_id:
        return
    with SessionLocal() as db:
        row = db.scalar(select(ToolExecution).where(ToolExecution.id == record_id).with_for_update())
        if row and row.state == 'running':
            row.state, row.updated_at = 'failed', now()
        # An uncertain commit remains uncertain, including across process restarts.
        db.commit()


def error_kind(run):
    """Classify safe stored messages; never expose upstream exception payloads."""
    value = (run.error or '').casefold()
    if not value:
        return None
    for kind, words in [
        ('authorization', ['认证', '授权', '权限', '401', '403']),
        ('validation', ['参数', '422']),
        ('rate_limit', ['繁忙', '限流', '429']),
        ('context_limit', ['上下文超限', '上下文预算', 'context length']),
        ('timeout', ['超时', 'timeout']),
        ('interrupted', ['中断', '断开']),
        ('unavailable', ['不可达', '未配置', '未绑定', '尚未发布', '503']),
    ]:
        if any(word in value for word in words):
            return kind
    return 'unknown'


def describe(db, run):
    rows = list(db.scalars(select(ToolExecution).where(ToolExecution.run_id == run.id,
        ToolExecution.user_id == run.user_id).order_by(ToolExecution.created_at, ToolExecution.id)))
    attempts = [a.details for a in db.scalars(select(Audit).where(
        Audit.request_id == run.id, Audit.user_id == run.user_id, Audit.operation == 'tool.attempt'))]
    effects = execution_effects(db, run.id, run.user_id)
    accounted = {(row.operation, row.arguments_hash) for row in rows}
    legacy_successes = {effect['operation'] for effect in effects}
    writes = write_operations()
    legacy_write = any(attempt.get('operation') in writes and
        (attempt.get('operation'), attempt.get('arguments_hash')) not in accounted and
        not (not attempt.get('arguments_hash') and attempt.get('operation') in legacy_successes)
        for attempt in attempts)
    unresolved = any(row.state in {'running', 'uncertain'} for row in rows) or legacy_write
    succeeded = any(row.state == 'succeeded' for row in rows) or bool(effects)
    can_retry = run.status in {'failed', 'interrupted'} and not unresolved and not succeeded
    if run.status == 'running':
        message = '本次请求仍在执行，请稍后核对结果，不必重复提交。'
    elif unresolved:
        message = '存在提交结果待核对的操作。请查看对应业务记录；本次不会重复执行。'
    elif succeeded and run.status != 'succeeded':
        message = '已有操作完成，但回复未完成。请查看执行回执与对应业务结果；本次不会重复执行。'
    elif can_retry:
        message = '本轮未发现已提交或结果不确定的写操作，可以安全重试这条消息。'
    else:
        message = '已恢复本次请求的已有结果，没有重复执行。'
    return {'run_id': run.id, 'can_retry': can_retry, 'error_kind': error_kind(run),
        'business_status': 'unknown' if unresolved else 'partial' if succeeded and any(r.state == 'failed' for r in rows) else 'completed' if succeeded else
                           'failed' if rows else 'not_started',
        'message': message, 'operations': [{'operation': r.operation, 'state': r.state,
            'resource_id': (r.result or {}).get('data', {}).get('id')
                if isinstance((r.result or {}).get('data'), dict) else None} for r in rows]}


def checkpoint(run_id, answer, sources):
    with SessionLocal() as db:
        run = db.scalar(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
        if not run or run.status != 'running':
            raise HTTPException(409, '本轮已结束，请核对运行结果')
        run.answer, run.sources, run.updated_at = answer, sources, now()
        db.commit()


def recover_stale(db, user_id=None):
    cutoff = now() - timedelta(seconds=settings.chat_recovery_seconds)
    query = select(ChatRun).where(ChatRun.status == 'running',
        func.coalesce(ChatRun.updated_at, ChatRun.created_at) < cutoff)
    if user_id:
        query = query.where(ChatRun.user_id == user_id)
    rows = list(db.scalars(query.with_for_update(skip_locked=True)))
    for row in rows:
        row.status, row.updated_at = 'interrupted', now()
        row.error = '本轮执行超时或服务曾中断，已结束等待；请核对已有结果后恢复。'
        row.effects = execution_effects(db, row.id, row.user_id)
        for token in db.scalars(select(AccessToken).where(AccessToken.request_id == row.id,
                                                         AccessToken.kind == 'tool')):
            token.expires_at = now()
    return len(rows)


async def run():
    while True:
        try:
            def tick():
                with SessionLocal() as db:
                    count = recover_stale(db)
                    db.commit()
                    return count
            await asyncio.to_thread(tick)
        except Exception:
            logger.exception('Chat recovery sweep failed')
        await asyncio.sleep(15)
