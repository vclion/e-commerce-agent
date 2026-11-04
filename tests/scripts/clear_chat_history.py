"""One-off scoped workbench history cleanup with a private, verified backup."""
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

from sqlalchemy import delete, select, text, func
from commerce.config import ROOT
from commerce.db import SessionLocal
from commerce.models import User, ChatRun, Conversation, AccessToken
from commerce.memory.models import ChatSession, ContextSegment, MemoryRunContext, MemoryJob


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', default='operator-a')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SET LOCAL statement_timeout = '30s'"))
        if args.apply:
            db.execute(text('LOCK TABLE chat_runs, conversations, memory_sessions, memory_run_contexts, '
                            'memory_context_segments, memory_jobs, access_tokens IN SHARE ROW EXCLUSIVE MODE'))
        actor = db.scalar(select(User).where(User.username == args.username))
        if not actor or actor.role != 'operator':
            raise RuntimeError('Expected an existing operator account')
        run_ids = list(db.scalars(select(ChatRun.id).where(ChatRun.user_id == actor.id)))
        session_ids = list(db.scalars(select(ChatSession.id).where(ChatSession.user_id == actor.id)))
        if db.scalar(select(ChatRun.id).where(ChatRun.user_id == actor.id, ChatRun.status == 'running').limit(1)):
            raise RuntimeError('A chat is still running; no history was removed')
        selections = [
            (MemoryRunContext, MemoryRunContext.user_id == actor.id),
            (ContextSegment, ContextSegment.session_id.in_(session_ids)),
            (MemoryJob, (MemoryJob.user_id == actor.id) & MemoryJob.source_id.in_(run_ids)
                        & MemoryJob.kind.in_(('extract', 'compress'))),
            (Conversation, Conversation.user_id == actor.id),
            (ChatRun, ChatRun.user_id == actor.id),
            (ChatSession, ChatSession.user_id == actor.id),
            (AccessToken, (AccessToken.user_id == actor.id) & (AccessToken.kind == 'tool')),
        ]
        snapshots = {model.__tablename__: [dict(row) for row in db.execute(
                         select(model.__table__).where(condition)).mappings()]
                     for model, condition in selections}
        counts = {table: len(rows) for table, rows in snapshots.items()}
        protected_chats = list(db.execute(select(ChatRun.id, ChatRun.user_id).where(ChatRun.user_id != actor.id)))
        backup_data = {'created_at': datetime.now(timezone.utc).isoformat(), 'username': actor.username,
                       'user_id': actor.id, 'tables': snapshots,
                       'preserved': ['business data', 'audit events', 'long-term memories', 'login sessions', 'other accounts']}
        print(json.dumps({'username': actor.username, 'apply': args.apply, 'counts': counts}, ensure_ascii=False), flush=True)
        if not args.apply:
            return
        backup_dir = ROOT / '.runtime/backups' / ('chat-clear-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        backup_path = backup_dir / 'history.json'
        payload = json.dumps(backup_data, ensure_ascii=False, indent=2, default=str).encode()
        with os.fdopen(os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        checksum = hashlib.sha256(payload).hexdigest()
        assert hashlib.sha256(backup_path.read_bytes()).hexdigest() == checksum
        assert {table: len(rows) for table, rows in json.loads(backup_path.read_text())['tables'].items()} == counts
        for model, condition in selections:
            db.execute(delete(model).where(condition))
        after = {model.__tablename__: db.scalar(select(func.count()).select_from(model).where(condition))
                 for model, condition in selections}
        assert all(value == 0 for value in after.values()), after
        assert list(db.execute(select(ChatRun.id, ChatRun.user_id).where(ChatRun.user_id != actor.id))) == protected_chats
        result = {'username': actor.username, 'removed': counts, 'remaining': after,
                  'backup': str(backup_path), 'backup_sha256': checksum}
    print(json.dumps({'status': 'completed', **result}, ensure_ascii=False, indent=2))
    folder = ROOT / 'tests/artifacts/chat-clear'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
