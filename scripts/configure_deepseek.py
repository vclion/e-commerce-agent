"""Import a DeepSeek key from hidden input or stdin directly into local Dify.

The raw provider key is never saved to source, .env, a temporary file, or stdout.
"""
import getpass
import sys
from .dify_console import DifyConsole


def main():
    secret = getpass.getpass('DeepSeek API Key: ') if sys.stdin.isatty() else sys.stdin.read().strip()
    if not secret or not secret.startswith('sk-') or any(c.isspace() for c in secret):
        raise SystemExit('API Key 格式无效，未修改 Dify 配置。')
    try:
        console = DifyConsole()
        path = '/workspaces/current/model-providers/langgenius/deepseek/deepseek/credentials'
        console.request('POST', path, json={'credentials': {'api_key': secret}, 'name': 'Commerce DeepSeek'})
        providers = console.request('GET', '/workspaces/current/model-providers')['data']
        provider = next(p for p in providers if p['provider'] == 'langgenius/deepseek/deepseek')
        custom = provider.get('custom_configuration') or {}
        credentials = custom.get('available_credentials') or []
        selected = next((c for c in credentials if c.get('credential_name', c.get('name')) == 'Commerce DeepSeek'), None)
        if selected:
            console.request('POST', path + '/switch', json={'credential_id': selected.get('credential_id', selected.get('id'))})
        models = console.request('GET', '/workspaces/current/model-providers/langgenius/deepseek/deepseek/models')['data']
        model = next(m for m in models if m['model'] == 'deepseek-flash')
        print('DeepSeek credential imported into local Dify. Model status:', model['status'])
        if model['status'] != 'active':
            raise RuntimeError('Key 已保存，但模型尚未处于 active 状态，需要检查供应商配置。')
    except Exception as error:
        raise SystemExit(str(error).replace(secret, '[redacted]')) from None
    finally:
        secret = ''


if __name__ == '__main__':
    main()
