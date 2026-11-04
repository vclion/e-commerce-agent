"""Update only local context apps using installed plugins; no downloads."""
import json
import subprocess
import sys
from datetime import datetime, timezone
import yaml
from commerce.config import ROOT, settings
from .dify_console import DifyConsole
from .generate_dify import generate
from .setup_integrations import api_tools


def main():
    console = DifyConsole()
    manifest = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    backup = ROOT / '.runtime/backups' / ('indexed-dify-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    backup.mkdir(parents=True, exist_ok=True)
    for slug in ('operator', 'memory-maintenance'):
        graph = console.request('GET', f'/apps/{manifest[slug]["app_id"]}/workflows/publish')
        path = backup / (slug + '.json')
        path.write_text(json.dumps(graph, ensure_ascii=False)); path.chmod(0o600)
    api_tools(console, actions=False)
    generate(only={'operator', 'memory-maintenance'})
    for slug in ('memory-maintenance', 'operator'):
        entry = manifest[slug]
        data = yaml.safe_load((ROOT / entry['source']).read_text())
        for variable in data['workflow']['environment_variables']:
            if variable['name'] == 'TOOL_SERVICE_KEY':
                variable['value'] = settings.tool_service_key
        result = console.request('POST', '/apps/imports', json={'mode':'yaml-content',
            'app_id':entry['app_id'], 'yaml_content':yaml.safe_dump(data, allow_unicode=True, sort_keys=False)})
        if result.get('status') == 'pending':
            result = console.request('POST', f'/apps/imports/{result["id"]}/confirm', json={})
        if result.get('app_id') != entry['app_id']:
            raise RuntimeError('Refused unexpected application replacement')
        console.request('POST', f'/apps/{entry["app_id"]}/workflows/publish', json={'marked_name':'indexed-reactive-v3'})
        published = console.request('GET', f'/apps/{entry["app_id"]}/workflows/publish')
        nodes = published['graph']['nodes']
        if slug == 'operator':
            agent = next(n['data'] for n in nodes if n['id'] == 'agent')
            assert 'memory' not in agent and 'history_read' in agent['agent_parameters']['instruction']['value']
        else:
            prompt = next(n['data'] for n in nodes if n['id'] == 'maintenance')['prompt_template'][0]['text']
            assert 'compress_segment' in prompt
            assert 'reactive_context' in prompt
        print('Published local app:', slug)
    subprocess.run([sys.executable, str(ROOT / 'scripts/start.py'), '--restart'], cwd=ROOT, check=True)
    print('Indexed context enabled in the local workbench.')


if __name__ == '__main__':
    main()
