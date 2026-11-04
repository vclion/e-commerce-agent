"""Configure the pinned local Dify checkout without exposing secrets."""
from pathlib import Path
import secrets
import json
import subprocess
if __package__:
    from .windows_proxy import configuration as proxy_configuration, ensure_started as ensure_proxy
else:
    from windows_proxy import configuration as proxy_configuration, ensure_started as ensure_proxy

root = Path(__file__).resolve().parents[1]
docker = root / 'vendor/dify/docker'
target = docker / '.env'
if not target.exists():
    redis_password = secrets.token_hex(24)
    replacements = {
        'SECRET_KEY': secrets.token_hex(32), 'DB_PASSWORD': secrets.token_hex(24),
        'REDIS_PASSWORD': redis_password,
        'CELERY_BROKER_URL': f'redis://:{redis_password}@redis:6379/1',
        'SANDBOX_API_KEY': secrets.token_hex(24),
        'PLUGIN_DAEMON_KEY': secrets.token_hex(32),
        'PLUGIN_DIFY_INNER_API_KEY': secrets.token_hex(32),
        'EXPOSE_NGINX_PORT': '127.0.0.1:8088',
        'EXPOSE_NGINX_SSL_PORT': '127.0.0.1:8448',
        'EXPOSE_PLUGIN_DEBUGGING_PORT': '5003',
        'CONSOLE_WEB_URL': 'http://localhost:8088',
        'CONSOLE_API_URL': 'http://localhost:8088',
        'SERVICE_API_URL': 'http://localhost:8088',
        'APP_WEB_URL': 'http://localhost:8088',
        'APP_API_URL': 'http://localhost:8088',
        'FILES_URL': 'http://localhost:8088',
        'NEXT_PUBLIC_SOCKET_URL': 'ws://localhost:8088',
    }
    lines = []
    for line in (docker / '.env.example').read_text().splitlines():
        key = line.split('=', 1)[0]
        lines.append(f'{key}={replacements[key]}' if key in replacements else line)
    target.write_text('\n'.join(lines) + '\n')
    target.chmod(0o600)
# The port variable is also consumed by application validation; it must be numeric.
content = target.read_text().replace('EXPOSE_PLUGIN_DEBUGGING_PORT=127.0.0.1:5003', 'EXPOSE_PLUGIN_DEBUGGING_PORT=5003')
# The API client and sandbox server use two variable names for the same key.
values = dict(line.split('=', 1) for line in content.splitlines() if '=' in line and not line.startswith('#'))
content = '\n'.join('CODE_EXECUTION_API_KEY=' + values['SANDBOX_API_KEY']
                    if line.startswith('CODE_EXECUTION_API_KEY=') else line
                    for line in content.splitlines()) + '\n'
# Allow only the Docker host gateway used by this project's authenticated API.
# Keep SSRF protection enabled for all other private addresses.
bridge = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'bridge']))[0]
gateway = bridge['IPAM']['Config'][0]['Gateway']
content = '\n'.join('SSRF_PROXY_ALLOW_PRIVATE_IPS=' + gateway + '/32'
                    if line.startswith('SSRF_PROXY_ALLOW_PRIVATE_IPS=') else line
                    for line in content.splitlines()) + '\n'
target.write_text(content)
override = docker / 'docker-compose.override.yaml'
proxy = proxy_configuration()
override.write_text('services:\n' + ''.join(
    f'  {name}:\n    extra_hosts:\n      - "host.docker.internal:host-gateway"\n'
    + ('    ports: !override\n      - "127.0.0.1:5003:5003"\n' if name == 'plugin_daemon' else '')
    + (f'    environment:\n      HTTP_PROXY: "http://host.docker.internal:{proxy["listen_port"]}"\n'
       f'      HTTPS_PROXY: "http://host.docker.internal:{proxy["listen_port"]}"\n'
       '      NO_PROXY: "localhost,127.0.0.1,::1,api,worker,plugin_daemon,ssrf_proxy,host.docker.internal"\n'
       if name == 'plugin_daemon' and proxy else '')
    for name in ['api', 'worker', 'ssrf_proxy', 'plugin_daemon']
))
ensure_proxy()
print('Dify 1.17.1 local configuration ready; web port 8088.')
