"""Optional WSL-to-Windows local proxy pipe for Docker under Windows TUN routing.

The TCP stream stays opaque: HTTPS CONNECT is terminated by the user's existing
Windows proxy, and TLS/model credentials remain between Dify and the provider.
Only binds the local Docker bridge gateway; does not change system networking.
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.runtime/windows-proxy.json'
PID = ROOT / '.runtime/windows-proxy.pid'
WINDOWS_SHELL = '/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
PIPE = '''
$ErrorActionPreference = 'Stop'
$client = [System.Net.Sockets.TcpClient]::new()
try {{
    $client.Connect('127.0.0.1', {port})
    $network = $client.GetStream()
    $up = [Console]::OpenStandardInput().CopyToAsync($network)
    $down = $network.CopyToAsync([Console]::OpenStandardOutput())
    [System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]@($up, $down)) | Out-Null
}} finally {{
    $client.Dispose()
}}
'''


def configuration():
    if not CONFIG.exists():
        return None
    config = json.loads(CONFIG.read_text())
    if not config.get('enabled'):
        return None
    for key, default in [('listen_port', 18790), ('windows_proxy_port', 7890)]:
        value = config.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 65535:
            raise ValueError('Local proxy ports must be integers from 1024 to 65535')
        config[key] = value
    bridge = json.loads(subprocess.check_output(['docker', 'network', 'inspect', 'bridge']))[0]
    config['listen_host'] = bridge['IPAM']['Config'][0]['Gateway']
    return config


def ensure_started():
    config = configuration()
    if config is None:
        return
    if not Path(WINDOWS_SHELL).is_file():
        raise RuntimeError('Configured Windows proxy pipe requires WSL Windows interop')
    if PID.exists():
        try:
            existing = int(PID.read_text())
            proc = Path(f'/proc/{existing}')
            if proc.exists() and b'scripts.windows_proxy' in (proc / 'cmdline').read_bytes() and (proc / 'cwd').resolve() == ROOT:
                return
        except (ValueError, FileNotFoundError, ProcessLookupError):
            pass
    log = (ROOT / '.runtime/windows-proxy.log').open('ab')
    try:
        process = subprocess.Popen([str(ROOT / '.venv/bin/python'), '-m', 'scripts.windows_proxy', '--serve'],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    finally:
        log.close()
    PID.write_text(str(process.pid))
    print('Project Windows proxy pipe starting on Docker bridge port', config['listen_port'])


async def serve(config):
    limit = asyncio.Semaphore(16)

    async def connect(reader, writer):
        process, tasks = None, []
        try:
            # Do not create a Windows process for an empty readiness probe.
            first = await asyncio.wait_for(reader.read(65536), timeout=10)
            if not first:
                return
            async with limit:
                process = await asyncio.create_subprocess_exec(WINDOWS_SHELL, '-NoProfile', '-NonInteractive',
                    '-Command', PIPE.format(port=config['windows_proxy_port']), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                process.stdin.write(first)
                await process.stdin.drain()

                async def copy(source, target):
                    while chunk := await source.read(65536):
                        target.write(chunk)
                        await target.drain()

                tasks = [asyncio.create_task(copy(reader, process.stdin)),
                         asyncio.create_task(copy(process.stdout, writer))]
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, TimeoutError, ConnectionError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            if process:
                if process.stdin:
                    process.stdin.close()
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                await process.wait()

    server = await asyncio.start_server(connect, config['listen_host'], config['listen_port'])
    print('Windows proxy pipe ready on local Docker bridge', flush=True)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    if '--serve' in sys.argv:
        current = configuration()
        if current:
            asyncio.run(serve(current))
    else:
        ensure_started()
