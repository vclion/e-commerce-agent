"""Local administrator client for the pinned Dify 1.17.1 installation.

Private cookies and credentials are never written to project source or printed.
"""
import base64
import json
import secrets
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[1]


class DifyConsole:
    def __init__(self):
        self.http = httpx.Client(base_url='http://127.0.0.1:8088/console/api', timeout=90)
        path = ROOT / '.runtime/dify-access.json'
        setup = self.request('GET', '/setup')
        if not path.exists():
            if setup['step'] != 'not_started':
                raise RuntimeError('An existing Dify administrator must sign in manually.')
            config = {'email': 'admin@commerce.local', 'password': 'Cc' + secrets.token_urlsafe(18) + '9',
                      'name': 'Commerce Developer'}
            path.write_text(json.dumps(config))
            path.chmod(0o600)
        config = json.loads(path.read_text())
        if setup['step'] == 'not_started':
            self.request('POST', '/setup', json={**config, 'language': 'zh-Hans'})
        self.request('POST', '/login', json={'email': config['email'],
            'password': base64.b64encode(config['password'].encode()).decode(), 'remember_me': True})
        csrf = next((v for k, v in self.http.cookies.items() if k.endswith('csrf_token')), '')
        self.http.headers['X-CSRF-Token'] = csrf

    def request(self, method, path, **kwargs):
        response = self.http.request(method, path, **kwargs)
        if response.status_code not in [200, 201, 202, 204]:
            try:
                data = response.json()
                message = str(data.get('message', data.get('code', 'Request failed')))[:250]
            except ValueError:
                message = 'Non-JSON response'
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {message}')
        return response.json() if response.content else {}


if __name__ == '__main__':
    client = DifyConsole()
    print('Local Dify administrator initialized and authenticated.')
