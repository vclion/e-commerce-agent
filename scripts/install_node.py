"""Install a verified official Node LTS archive into project-local runtime."""
import hashlib
import json
import tarfile
import urllib.request
from pathlib import Path

project = Path(__file__).resolve().parents[1]
root = project / '.runtime'
root.mkdir(exist_ok=True)
version = json.loads((project / 'runtime.lock.json').read_text())['node']
if (root / 'node/bin/node').exists():
    import subprocess
    if subprocess.check_output([root / 'node/bin/node', '--version'], text=True).strip() == version:
        print('Node', version, 'already installed.')
        raise SystemExit(0)
name = f'node-{version}-linux-x64.tar.xz'
base = f'https://nodejs.org/dist/{version}/'
checks = urllib.request.urlopen(base + 'SHASUMS256.txt', timeout=30).read().decode()
expected = next(line.split()[0] for line in checks.splitlines() if line.split()[-1] == name)
archive = root / name
urllib.request.urlretrieve(base + name, archive)
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected, 'Archive checksum mismatch'
with tarfile.open(archive) as handle:
    handle.extractall(root, filter='data')
link = root / 'node'
if not link.exists():
    link.symlink_to(root / name.removesuffix('.tar.xz'), target_is_directory=True)
(root / 'node-version.txt').write_text(version + '\n')
print('Installed and verified Node', version)
