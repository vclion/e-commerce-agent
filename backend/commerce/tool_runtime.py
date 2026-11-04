"""Keep chat writes and their receipts consistent with the run lifecycle."""
from fastapi import HTTPException
from sqlalchemy import event, select

from .models import Audit, ChatRun


def write_guard(db, request_id, user_id):
    def check(session, lock=True):
        query = select(ChatRun).where(ChatRun.id == request_id).execution_options(populate_existing=True)
        run = session.scalar(query.with_for_update() if lock else query)
        if run and (run.user_id != user_id or run.status != 'running'):
            raise HTTPException(409, '本轮对话已结束，未提交新的操作，请重新提问')
        return run

    if check(db, lock=False) is None:
        return lambda: None  # independent, non-chat workflow context
    # Some handlers commit more than once (for example around a model call).
    # Recheck each transaction so a later commit cannot outlive a cancelled run.
    event.listen(db, 'before_commit', check)

    def release():
        if event.contains(db, 'before_commit', check):
            event.remove(db, 'before_commit', check)
    return release


def execution_effects(db, request_id, user_id):
    return [{'operation': row.operation.removeprefix('tool.'),
             'label': row.details['effect'], 'resource_id': row.details.get('resource_id')}
            for row in db.scalars(select(Audit).where(Audit.request_id == request_id,
                Audit.user_id == user_id).order_by(Audit.created_at, Audit.id))
            if row.details.get('effect')]
