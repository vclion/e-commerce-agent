"""Import generated apps idempotently into the local Dify workspace.

Credentials are injected in memory and API keys stay in .runtime (mode 0600).
No model invocation is performed by this script.
"""
import json
from pathlib import Path
import yaml
from commerce.config import settings
from .dify_console import DifyConsole, ROOT


def main():
    console = DifyConsole()
    existing = console.request('GET', '/apps', params={'page': 1, 'limit': 100}).get('data', [])
    by_name = {item['name']: item['id'] for item in existing}
    # Disable retired entry points reversibly; preserve histories and workspace apps.
    retirement = []
    for name in ['CF-Customer · 消费者助手', 'CF-Merchant · 商家助手', 'WF-Service-Prepare', 'WF-Listing-Draft']:
        if name not in by_name:
            continue
        app_id = by_name[name]
        console.request('POST', f'/apps/{app_id}/api-enable', json={'enable_api': False})
        console.request('POST', f'/apps/{app_id}/site-enable', json={'enable_site': False})
        retirement.append({'name': name, 'app_id': app_id, 'enable_api': False, 'enable_site': False})
        print('Retired entry point:', name)
    if retirement:
        backup = ROOT / '.runtime/backups/operator-only'
        backup.mkdir(parents=True, exist_ok=True)
        lock_path = ROOT / 'dify/apps.lock.json'
        if lock_path.exists() and not (backup / 'apps.lock.json').exists():
            (backup / 'apps.lock.json').write_text(lock_path.read_text())
        (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
        (ROOT / 'tests/artifacts/reports/operator-retired-apps.json').write_text(json.dumps(retirement, ensure_ascii=False, indent=2))
    manifest = {}
    key_path = ROOT / '.runtime/dify-app-keys.json'
    keys = json.loads(key_path.read_text()) if key_path.exists() else {}
    for path in sorted((ROOT / 'dify/workflows').glob('*.yml')) + sorted((ROOT / 'dify/chatflows').glob('*.yml')):
        data = yaml.safe_load(path.read_text())
        name = data['app']['name']
        for var in data['workflow']['environment_variables']:
            if var['name'] == 'TOOL_SERVICE_KEY':
                var['value'] = settings.tool_service_key
        payload = {'mode': 'yaml-content', 'yaml_content': yaml.safe_dump(data, allow_unicode=True, sort_keys=False)}
        if name in by_name:
            payload['app_id'] = by_name[name]
        response = console.request('POST', '/apps/imports', json=payload)
        if response.get('status') == 'pending':
            response = console.request('POST', f'/apps/imports/{response["id"]}/confirm', json={})
        app_id = response.get('app_id')
        print(name, 'import:', response.get('status'), 'id:', app_id)
        if not app_id:
            raise RuntimeError('Import did not return app_id: ' + str(response)[:500])
        item = {'app_id': app_id, 'mode': data['app']['mode'], 'source': str(path.relative_to(ROOT)),
                'url': f'http://localhost:8088/app/{app_id}/workflow'}
        try:
            published = console.request('POST', f'/apps/{app_id}/workflows/publish', json={'marked_name': 'demo-v0.1'})
            item['published'] = True
            print('  Published version:', published.get('created_at', 'ok'))
        except RuntimeError as error:
            item['published'] = False
            item['publish_error'] = str(error)
            print('  Publish pending:', error)
        api_keys = console.request('GET', f'/apps/{app_id}/api-keys').get('data', [])
        if api_keys:
            keys[app_id] = api_keys[0]['token']
        else:
            keys[app_id] = console.request('POST', f'/apps/{app_id}/api-keys', json={})['token']
        manifest[path.stem] = item
        # Persist each successful import to make interrupted runs recoverable.
        key_path.write_text(json.dumps(keys, indent=2)); key_path.chmod(0o600)
        (ROOT / 'dify/apps.lock.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    bound = all(getattr(settings, f'dify_{role}_key') for role in ['operator'])
    print('App definitions imported; existing workbench key bindings preserved.' if bound else
          'App definitions imported. Business .env remains unbound until the DeepSeek model is configured.')


if __name__ == '__main__':
    main()
