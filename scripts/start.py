"""Start the local workbench without exposing private configuration."""
from pathlib import Path
import json
import os
import subprocess
import sys
import time
import urllib.request
import signal

# Optional project proxy is configured only on hosts requiring Windows routing.
if (root_config := Path(__file__).resolve().parents[1] / '.runtime/windows-proxy.json').exists() and '--stop' not in sys.argv:
    subprocess.run([sys.executable, '-m', 'scripts.windows_proxy'], cwd=root_config.parent.parent, check=True)

root = Path(__file__).resolve().parents[1]
runtime = root / '.runtime'
runtime.mkdir(exist_ok=True)
from commerce.config import settings


def stop_helper(name, marker):
    pid_file=runtime/(name+'.pid')
    if not pid_file.exists():return
    helper_pid=int(pid_file.read_text())
    helper_proc=Path(f'/proc/{helper_pid}')
    if helper_proc.exists():
        if marker not in (helper_proc/'cmdline').read_text() or (helper_proc/'cwd').resolve()!=root:
            raise RuntimeError('Refused to stop an unrecognized helper.')
        os.kill(helper_pid,signal.SIGTERM)
    pid_file.unlink(missing_ok=True)


def start_helper(name, command, marker):
    pid_file=runtime/(name+'.pid')
    if pid_file.exists():
        proc=Path('/proc/'+pid_file.read_text().strip())
        if proc.exists() and marker in (proc/'cmdline').read_text() and (proc/'cwd').resolve()==root:return
    with (runtime/(name+'.log')).open('a') as helper_log:
        child=subprocess.Popen(command,cwd=root,stdout=helper_log,stderr=subprocess.STDOUT,start_new_session=True)
    pid_file.write_text(str(child.pid))


if any(flag in sys.argv for flag in ['--restart','--stop']):
    stop_helper('memory-worker','commerce.memory.worker')
    stop_helper('memory-embedding','memory_embedding_server.py')
if any(flag in sys.argv for flag in ['--restart', '--stop']) and (runtime / 'api.pid').exists():
    pid = int((runtime / 'api.pid').read_text())
    proc = Path(f'/proc/{pid}')
    if proc.exists():
        cmdline = (proc / 'cmdline').read_text()
        if 'commerce.main:app' not in cmdline or (proc / 'cwd').resolve() != root:
            sys.exit('Refused to stop an unrecognized process.')
        os.kill(pid, signal.SIGTERM)
        for _ in range(30):
            if not proc.exists():
                break
            time.sleep(.1)
if '--stop' in sys.argv:
    (runtime / 'api.pid').unlink(missing_ok=True)
    print('Workbench stopped.')
    sys.exit(0)
if settings.memory_enabled and settings.memory_worker_enabled:
    from commerce.db import initialize_schema
    initialize_schema()
    start_helper('memory-worker',[str(root/'.venv/bin/python'),'-m','commerce.memory.worker'],'commerce.memory.worker')
url = 'http://127.0.0.1:8000/api/health'
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        assert json.load(response).get('status') == 'ok'
    print('Workbench already responding at http://localhost:8000')
    sys.exit(0)
except Exception:
    pass
log = open(runtime / 'api.log', 'a')
process = subprocess.Popen([str(root / '.venv/bin/python'), '-m', 'uvicorn', 'commerce.main:app',
                            '--host', '0.0.0.0', '--port', '8000', '--no-access-log'],
                           cwd=root, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
(runtime / 'api.pid').write_text(str(process.pid))
for attempt in range(30):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            if json.load(response).get('status') == 'ok':
                print('Workbench ready at http://localhost:8000')
                sys.exit(0)
    except Exception:
        if process.poll() is not None:
            sys.exit('API failed to start; inspect .runtime/api.log')
    time.sleep(1)
sys.exit('API startup timed out; inspect .runtime/api.log')
