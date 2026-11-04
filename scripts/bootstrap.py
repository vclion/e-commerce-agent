"""Generate private local configuration. Never print credentials."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
env = root / '.env'
if not env.exists():
    password = secrets.token_hex(20)
    env.write_text('\n'.join([
        f'DATABASE_URL=postgresql+psycopg://commerce:{password}@127.0.0.1:55432/commerce',
        f'POSTGRES_PASSWORD={password}',
        f'DEMO_PASSWORD={secrets.token_urlsafe(16)}',
        f'TOOL_SERVICE_KEY={secrets.token_urlsafe(32)}',
        'DIFY_BASE_URL=http://127.0.0.1:8088/v1',
        'DIFY_OPERATOR_KEY=', '',
    ]), encoding='utf-8')
    env.chmod(0o600)
    print('Created private .env; demo password is available there.')
else:
    print('Preserved existing .env.')
