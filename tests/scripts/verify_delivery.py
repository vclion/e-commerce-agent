"""Check local delivery state and ensure public project files contain no secrets."""
import json
from datetime import datetime, timezone
from pathlib import Path
import httpx
import yaml
import xml.etree.ElementTree as ET
from commerce.config import settings
from scripts.dify_console import DifyConsole, ROOT
from .acceptance import ARTIFACTS, write_report


def application_sources(root=ROOT):
    return {path.stem: path for folder in ('workflows', 'chatflows')
            for path in (root / 'dify' / folder).glob('*.yml')}


def read_junit(path):
    """Read only explicitly selected evidence; failed/skipped tests are never passes."""
    if not path.exists():
        return {'status': 'missing', 'path': str(path)}
    root = ET.parse(path).getroot()
    suites = [suite for suite in root.iter('testsuite') if not suite.findall('testsuite')]
    assert suites, 'JUnit contains no test suites'
    failures = sum(int(s.get('failures', 0)) + int(s.get('errors', 0)) for s in suites)
    passed = sum(int(s.get('tests', 0)) - int(s.get('skipped', 0)) - int(s.get('failures', 0)) - int(s.get('errors', 0)) for s in suites)
    return {'status': 'failed' if failures else 'passed' if passed else 'empty',
            'path': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
            'verified_at': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            'passed': passed,
            'failures': failures}


def main():
    (ROOT / 'tests/artifacts/reports').mkdir(parents=True, exist_ok=True)
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junit', type=Path, default=ARTIFACTS / 'backend-junit.xml')
    args = parser.parse_args()
    excluded = {'.git', '.venv', '.runtime', 'vendor', 'node_modules', '__pycache__', '.pytest_cache', 'dist', 'screenshots'}
    secrets = [settings.demo_password, settings.tool_service_key, settings.dify_knowledge_key,
               settings.dify_operator_key]
    # Include all generated private administrator/app keys in the scan, without printing values.
    for filename in ['dify-access.json', 'dify-app-keys.json', 'knowledge-key.json']:
        path = ROOT / '.runtime' / filename
        if path.exists():
            data = json.loads(path.read_text())
            if filename == 'dify-access.json':
                secrets.append(data['password'])
            elif filename == 'dify-app-keys.json':
                secrets.extend(data.values())
            else:
                secrets.append(data['token'])
    secrets = [s for s in secrets if s and len(s) >= 12]
    leaks = []
    def walk(folder):
        for path in folder.iterdir():
            if path.name in excluded or path.name == '.env' or path.is_symlink():
                continue
            if path.is_dir():
                yield from walk(path)
            elif path.suffix in ['.py', '.json', '.jsonl', '.md', '.yml', '.yaml', '.vue', '.ts', '.css', '.sh', '.toml', '.lock']:
                yield path
    for path in walk(ROOT):
        content = path.read_text(errors='replace')
        if any(value in content for value in secrets):
            leaks.append(str(path.relative_to(ROOT)))
    assert not leaks, 'Secret found in public project files: ' + ', '.join(leaks)
    console = DifyConsole()
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    sources = application_sources()
    assert sources and set(apps) == set(sources), {'missing': sorted(set(sources) - set(apps)), 'extra': sorted(set(apps) - set(sources))}
    for name, item in apps.items():
        assert (ROOT / item['source']).resolve() == sources[name].resolve(), (name, item['source'])
        definition = yaml.safe_load((ROOT / item['source']).read_text())
        assert definition['version'] == '0.7.0'
        published = console.request('GET', f'/apps/{item["app_id"]}/workflows/publish')
        assert published and published.get('graph'), 'Missing published graph'
        for var in definition['workflow']['environment_variables']:
            if var['value_type'] == 'secret':
                assert var['value'] == ''
    models = console.request('GET', '/workspaces/current/model-providers/langgenius/deepseek/deepseek/models')['data']
    model_status = next(x['status'] for x in models if x['model'] == 'deepseek-flash')
    live_path = ROOT / 'tests/artifacts/reports/live-model-report.json'
    live_verified = False
    if live_path.exists():
        latest = {x['role']: x for x in json.loads(live_path.read_text())}
        live_verified = all(role in latest and latest[role]['status'] == 'succeeded' and
                            latest[role].get('answer') and all(latest[role].get('grounding_check', {}).values())
                            for role in ['operator'])
    docs = json.loads((ROOT / 'knowledge/manifest.json').read_text())
    health = httpx.get('http://127.0.0.1:8000/api/health').json()
    evidence = read_junit(args.junit.resolve())
    assert evidence['status'] != 'failed', evidence
    test_count = evidence.get('passed') if evidence['status'] == 'passed' else None
    report = {'public_secret_scan': 'passed', 'published_apps': len(apps), 'knowledge_documents': len(docs),
              'workbench_health': health['status'], 'deepseek_model_status': model_status,
              'live_model_quality_evaluation': ('pending_credentials' if model_status != 'active' else
                                                'smoke_verified_not_benchmarked' if live_verified else 'pending_live_evaluation'),
              'backend_integration_tests_last_verified': test_count, 'test_evidence': evidence,
              'active_dataset': health['dataset']['version'],
              'apparel_skus': health['dataset']['skus']}
    write_report('delivery-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()



