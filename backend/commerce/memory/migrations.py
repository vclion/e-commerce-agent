"""Numbered, additive migrations. No historical chat is imported implicitly."""
from sqlalchemy import text


def migrate(engine):
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE IF NOT EXISTS memory_schema_versions (version integer PRIMARY KEY)'))
        # A lock shared by schema initializers in this schema, including multiple API workers.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(current_schema() || '/memory-migrations'))"))
        if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=1')):
            conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS session_id VARCHAR(40)'))
            conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS turn_seq INTEGER NOT NULL DEFAULT 0'))
            conn.execute(text('ALTER TABLE chat_runs ADD COLUMN IF NOT EXISTS memory_excluded BOOLEAN NOT NULL DEFAULT false'))
            conn.execute(text('ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_id VARCHAR(40)'))
            conn.execute(text('CREATE INDEX IF NOT EXISTS ix_chat_runs_session_id ON chat_runs(session_id, turn_seq)'))
            conn.execute(text("CREATE INDEX IF NOT EXISTS memory_chunk_words ON memory_chunks USING gin(to_tsvector('simple', tokens))"))
            conn.execute(text('INSERT INTO memory_schema_versions VALUES (1)'))
        if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=2')):
            from sqlalchemy import inspect
            schema=conn.scalar(text('SELECT current_schema()'))
            inspector=inspect(conn)
            for table in inspector.get_table_names(schema=schema):
                if not table.startswith('memory_'):continue
                for fk in inspector.get_foreign_keys(table,schema=schema):
                    if fk.get('options',{}).get('ondelete')=='CASCADE':continue
                    columns=','.join('"'+c+'"' for c in fk['constrained_columns'])
                    targets=','.join('"'+c+'"' for c in fk['referred_columns'])
                    ref_schema=fk['referred_schema'] or schema
                    conn.execute(text(f'ALTER TABLE "{table}" DROP CONSTRAINT "{fk["name"]}"'))
                    conn.execute(text(f'ALTER TABLE "{table}" ADD CONSTRAINT "{fk["name"]}" FOREIGN KEY ({columns}) REFERENCES "{ref_schema}"."{fk["referred_table"]}" ({targets}) ON DELETE CASCADE'))
            conn.execute(text('INSERT INTO memory_schema_versions VALUES (2)'))
        # Extension installation belongs to provisioning; do not download packages at API startup.
        if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=4')):
            from .models import ContextSegment
            ContextSegment.__table__.create(conn, checkfirst=True)
            conn.execute(text('INSERT INTO memory_schema_versions VALUES (4)'))
        if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=5')):
            conn.execute(text("ALTER TABLE memory_context_segments ADD COLUMN IF NOT EXISTS entities JSONB NOT NULL DEFAULT '[]'"))
            conn.execute(text('ALTER TABLE memory_context_segments ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ'))
            conn.execute(text('ALTER TABLE memory_context_segments ADD COLUMN IF NOT EXISTS last_used_seq INTEGER NOT NULL DEFAULT 0'))
            conn.execute(text('ALTER TABLE memory_context_segments ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0'))
            conn.execute(text('INSERT INTO memory_schema_versions VALUES (5)'))
        if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=6')):
            conn.execute(text('ALTER TABLE memory_settings ADD COLUMN IF NOT EXISTS context_index_enabled BOOLEAN NOT NULL DEFAULT true'))
            conn.execute(text('ALTER TABLE memory_settings ADD COLUMN IF NOT EXISTS read_epoch INTEGER NOT NULL DEFAULT 1'))
            conn.execute(text('INSERT INTO memory_schema_versions VALUES (6)'))
        if conn.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='vector')")):
            conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public'))
            conn.execute(text('''CREATE TABLE IF NOT EXISTS memory_vectors (
                chunk_id VARCHAR(40) PRIMARY KEY REFERENCES memory_chunks(id) ON DELETE CASCADE,
                model VARCHAR(120) NOT NULL, embedding public.vector NOT NULL)'''))
            if not conn.scalar(text('SELECT 1 FROM memory_schema_versions WHERE version=3')):
                # Existing vectors stay intact. Each API model identity and dimension is filtered at retrieval.
                conn.execute(text('ALTER TABLE memory_vectors ALTER COLUMN embedding TYPE public.vector'))
                conn.execute(text('INSERT INTO memory_schema_versions VALUES (3)'))
