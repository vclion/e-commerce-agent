from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def initialize_schema():
    """Add apparel snapshots without altering legacy history or accounts."""
    from .memory import models as memory_models  # register the memory schema before create_all
    migrate_memberships = not inspect(engine).has_table('memberships')
    Base.metadata.create_all(engine)
    token_columns = {c['name'] for c in inspect(engine).get_columns('access_tokens')}
    chat_columns = {c['name'] for c in inspect(engine).get_columns('chat_runs')}
    with engine.begin() as conn:
        for table_name in ['access_tokens', 'chat_runs', 'conversations']:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS workspace VARCHAR(30) NOT NULL DEFAULT 'general'"))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_chat_runs_workspace ON chat_runs (workspace)'))
        conn.execute(text("ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS effects JSON NOT NULL DEFAULT '[]'"))
        conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ'))
        conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS client_request_id VARCHAR(64)'))
        conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS retry_of VARCHAR(40)'))
        conn.execute(text('''CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_runs_client_request
            ON chat_runs (user_id, client_request_id) WHERE client_request_id IS NOT NULL'''))
        for table_name in ['access_tokens', 'chat_runs']:
            conn.execute(text(f'ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS scope_fingerprint VARCHAR(64)'))
        conn.execute(text('ALTER TABLE drafts ALTER COLUMN shop_id DROP NOT NULL'))
        conn.execute(text('ALTER TABLE drafts ADD COLUMN IF NOT EXISTS source_scope JSON'))
        # Single-shop history has a provable source. Never infer aggregate history
        # from today's memberships, which may already have been revoked.
        conn.execute(text('''UPDATE drafts d SET source_scope = json_build_object(d.shop_id, s.mode)
            FROM platform_shops s WHERE d.shop_id = s.id AND d.source_scope IS NULL'''))
        from .catalog import SHOPS
        conn.execute(text('''UPDATE drafts SET source_scope = json_build_object(shop_id, 'demo')
            WHERE shop_id = ANY(:shops) AND source_scope IS NULL'''), {'shops': list(SHOPS)})
        if 'shop_id' not in token_columns:
            conn.execute(text('ALTER TABLE access_tokens ADD COLUMN shop_id VARCHAR(20)'))
            conn.execute(text('''UPDATE access_tokens t SET shop_id = u.shop_id
                FROM users u WHERE t.user_id = u.id AND t.kind = 'tool' '''))
        if 'shop_id' not in chat_columns:
            conn.execute(text('ALTER TABLE chat_runs ADD COLUMN shop_id VARCHAR(20)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_chat_runs_shop_id ON chat_runs (shop_id)'))
            conn.execute(text('''UPDATE chat_runs r SET shop_id = u.shop_id
                FROM users u WHERE r.user_id = u.id'''))
        if migrate_memberships:
            conn.execute(text('''INSERT INTO memberships (user_id, shop_id)
                SELECT id, shop_id FROM users WHERE role = 'operator'
                ON CONFLICT DO NOTHING'''))
    columns = {c['name'] for c in inspect(engine).get_columns('orders')}
    with engine.begin() as conn:
        if 'dataset_version' not in columns:
            conn.execute(text("ALTER TABLE orders ADD COLUMN dataset_version VARCHAR(30) NOT NULL DEFAULT 'legacy-v1'"))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_orders_dataset_version ON orders (dataset_version)'))
        if 'specs_snapshot' not in columns:
            conn.execute(text("ALTER TABLE orders ADD COLUMN specs_snapshot JSON NOT NULL DEFAULT '{}'"))
        if 'refund_reason' not in columns:
            conn.execute(text('ALTER TABLE orders ADD COLUMN refund_reason VARCHAR(80)'))
    chat_columns = {c['name'] for c in inspect(engine).get_columns('chat_runs')}
    with engine.begin() as conn:
        if 'conversation_id' not in chat_columns:
            conn.execute(text('ALTER TABLE chat_runs ADD COLUMN conversation_id VARCHAR(100)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_chat_runs_conversation_id ON chat_runs (conversation_id)'))
            # Old tool contexts retain the last run ID; recover only verified ownership matches.
            conn.execute(text('''UPDATE chat_runs r SET conversation_id = c.id
                FROM conversations c JOIN access_tokens t ON t.digest = encode(sha256(c.context_ref::bytea), 'hex')
                WHERE t.request_id = r.id AND c.user_id = r.user_id AND t.user_id = r.user_id'''))
        if 'sources' not in chat_columns:
            conn.execute(text("ALTER TABLE chat_runs ADD COLUMN sources JSON NOT NULL DEFAULT '[]'"))
    from .memory.migrations import migrate
    migrate(engine)


def get_db():
    with SessionLocal() as session:
        yield session
