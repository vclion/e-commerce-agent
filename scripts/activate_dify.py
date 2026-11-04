"""Bind local Chatflow keys after DeepSeek has been configured in Dify."""
import json
import subprocess
from .dify_console import DifyConsole, ROOT


def main():
    console = DifyConsole()
    models = console.request('GET', '/workspaces/current/model-providers/langgenius/deepseek/deepseek/models')['data']
    flash = next((x for x in models if x['model'] == 'deepseek-flash'), None)
    if not flash or flash['status'] != 'active':
        raise SystemExit('DeepSeek 尚未配置可用凭据。请在 http://localhost:8088 的设置 → 模型供应商 → DeepSeek 中保存 API Key，然后重跑本命令。')
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    keys = json.loads((ROOT / '.runtime/dify-app-keys.json').read_text())
    values = {f'DIFY_{role.upper()}_KEY': keys[apps[role]['app_id']] for role in ['operator']}
    env = ROOT / '.env'
    lines = [line for line in env.read_text().splitlines() if line.split('=', 1)[0] not in {*values, 'DIFY_CUSTOMER_KEY', 'DIFY_MERCHANT_KEY'}]
    env.write_text('\n'.join(lines + [k + '=' + v for k, v in values.items()]) + '\n')
    env.chmod(0o600)
    subprocess.run([str(ROOT / '.venv/bin/python'), str(ROOT / 'scripts/start.py'), '--restart'], cwd=ROOT, check=True)
    print('DeepSeek configured; operations Chatflow key bound locally. Open http://localhost:8000 .')


if __name__ == '__main__':
    main()
