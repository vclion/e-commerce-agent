"""Use an isolated PostgreSQL schema, never mutate the demo dataset."""
import os
from pathlib import Path
from uuid import uuid4
import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

root = Path(__file__).resolve().parents[2]
source_url = os.environ.get('DATABASE_URL') or dotenv_values(root / '.env')['DATABASE_URL']
test_schema = 'cc_test_' + uuid4().hex
admin_engine = create_engine(source_url)
with admin_engine.begin() as conn:
    conn.execute(text(f'CREATE SCHEMA "{test_schema}"'))
test_url = make_url(source_url).update_query_dict({'options': f'-csearch_path={test_schema}'})
os.environ['DATABASE_URL'] = test_url.render_as_string(hide_password=False)
os.environ['OPS_WORKER_ENABLED'] = 'false'
os.environ['PLATFORM_WORKER_ENABLED'] = 'false'
os.environ['MEMORY_WORKER_ENABLED'] = 'false'
os.environ['MEMORY_EMBEDDING_URL'] = ''


@pytest.fixture(scope='session', autouse=True)
def database():
    from commerce.seed import seed
    from commerce.db import engine
    seed()
    yield
    engine.dispose()
    with admin_engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{test_schema}" CASCADE'))
    admin_engine.dispose()


@pytest.fixture()
def client(database):
    from fastapi.testclient import TestClient
    from commerce.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth(client):
    from commerce.config import settings
    def login(username='operator-a'):
        response = client.post('/api/auth/login', json={'username': username, 'password': settings.demo_password})
        assert response.status_code == 200
        return {'Authorization': 'Bearer ' + response.json()['access_token']}
    return login
