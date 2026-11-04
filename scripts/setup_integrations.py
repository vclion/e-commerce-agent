"""Install official plugins and register the project's API/workflow tools."""
import argparse
import json
import httpx
import yaml
from commerce.config import settings
from .dify_console import DifyConsole, ROOT
from commerce.chat_workspaces import READ_OPERATIONS, WRITE_OPERATIONS, OPERATIONS
from commerce.ops_tools import READ_OPERATIONS as OPS_READ, WRITE_OPERATIONS as OPS_WRITE
from commerce.memory.tools import READ_OPERATIONS as MEMORY_READ, WRITE_OPERATIONS as MEMORY_WRITE


def update_contracts():
    for actions in (False, True):
        path = ROOT / ('contracts/dify-action-tools.json' if actions else 'contracts/dify-read-tools.json')
        schema = json.loads(path.read_text())
        endpoint = '/tools/execute' if actions else '/tools/read'
        props = schema['paths'][endpoint]['post']['requestBody']['content']['application/json']['schema']['properties']
        extras = WRITE_OPERATIONS if actions else READ_OPERATIONS
        base = OPS_WRITE if actions else OPS_READ | {'products', 'product_detail', 'compare', 'orders',
            'after_sales', 'analytics', 'selection', 'competitors', 'knowledge', 'clarify'}
        props['operation']['enum'] = sorted(base | extras | (MEMORY_WRITE if actions else MEMORY_READ))
        props['arguments']['description'] = ('JSON参数对象；' + '\n'.join(
            op + ':' + description for op, (_, description) in OPERATIONS.items() if op in extras)
            + ('\nops_sop_generate:{date?};ops_task_update:{task_id,expected_version,status?,owner?,note?,evidence?};'
               'ops_script_save:{product_id,title,segments,reference_id?,script_id?,expected_version?};ops_render:{script_id,expected_version}'
               if actions else '\n原商品和经营读取参数保持不变；ops_overview:{days?};ops_tasks/ops_report:{date?};knowledge:{query,scope?}'))
        props['arguments']['description'] += ('\nmemory_propose:{kind,scope,subject,predicate,title,content,quote};memory_forget:{item_ids:[]}' if actions else '\nmemory_search:{query};memory_get:{id};history_read:{index_id?,run_id?,query?,field?:user或assistant或effects,offset?,length?,after_turn?,limit?:最多5}；按索引定位原文，query也搜索冷索引与未建索引历史；仅当前会话，返回分页')
        path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')


def api_tools(console, actions=False):
    update_contracts()
    schema_path = ROOT / ('contracts/dify-action-tools.json' if actions else 'contracts/dify-read-tools.json')
    schema = json.loads(schema_path.read_text())
    if actions:
        operation_path = '/tools/execute'
    else:
        operation_path = '/tools/read'
    operation = schema['paths'][operation_path]['post']['requestBody']['content']['application/json']['schema']['properties']['operation']
    if not actions and 'knowledge' not in operation['enum']:
        operation['enum'].append('knowledge')
    params = schema['paths'][operation_path]['post']['requestBody']['content']['application/json']['schema']['properties']['arguments']
    if not actions and 'knowledge:' not in params['description']:
        params['description'] += ' knowledge: query,scope(public/internal)。'
    schema_path.write_text(json.dumps(schema, ensure_ascii=False, indent=2))
    providers = console.request('GET', '/workspaces/current/tool-providers', params={'type': 'api'})
    name = 'Commerce Actions' if actions else 'Commerce Read'
    existing = next((p for p in providers if p['name'] == name), None)
    payload = {'provider': name, 'schema_type': 'openapi', 'schema': json.dumps(schema),
               'icon': {'content': '🛍️', 'background': '#E8EEE3'},
               'credentials': {'auth_type': 'api_key_header', 'api_key_header': 'Authorization',
                               'api_key_header_prefix': 'custom', 'api_key_value': 'Bearer ' + settings.tool_service_key}}
    if existing:
        payload['original_provider'] = name
    console.request('POST', '/workspaces/current/tool-provider/api/' + ('update' if existing else 'add'), json=payload)
    tools = console.request('GET', '/workspaces/current/tool-provider/api/tools', params={'provider': name})
    providers = console.request('GET', '/workspaces/current/tool-providers', params={'type': 'api'})
    provider_id = next(p['id'] for p in providers if p['name'] == name)
    (ROOT / ('.runtime/action-tool.json' if actions else '.runtime/read-tool.json')).write_text(json.dumps({'provider': provider_id, 'tools': tools}))
    print('Registered authenticated tool:', name)


def plugins(console):
    lock_path = ROOT / 'dify/plugins.lock.json'
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
    for plugin in ['deepseek', 'agent']:
        if plugin not in lock:
            cached = ROOT / f'.runtime/marketplace-{plugin}.json'
            if cached.exists():
                data = json.loads(cached.read_text())
            else:
                response = httpx.get(f'https://marketplace.dify.ai/api/v1/plugins/langgenius/{plugin}', timeout=30)
                response.raise_for_status()
                data = response.json()
                cached.write_text(json.dumps(data))
            lock[plugin] = data['data']['plugin']['latest_package_identifier']
        package = lock[plugin]
        result = console.request('POST', '/workspaces/current/plugin/install/marketplace',
                                  json={'plugin_unique_identifiers': [package]})
        print('Official plugin:', plugin, 'already installed' if result.get('all_installed') else 'installation queued')
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2))


def workflow_tools(console):
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    tools_manifest = {}
    for slug, item in apps.items():
        if item['mode'] != 'workflow' or slug == 'memory-maintenance':
            continue
        try:
            existing = console.request('GET', '/workspaces/current/tool-provider/workflow/get', params={'workflow_app_id': item['app_id']})
        except RuntimeError as error:
            if 'HTTP 400' not in str(error) and 'HTTP 404' not in str(error):
                raise
            existing = None
        if not existing:
            data = yaml.safe_load((ROOT / item['source']).read_text())
            fields = data['workflow']['graph']['nodes'][0]['data']['variables']
            console.request('POST', '/workspaces/current/tool-provider/workflow/create', json={
                'workflow_app_id': item['app_id'], 'name': 'wf_' + slug.replace('-', '_'),
                'label': data['app']['name'], 'description': data['app']['description'] or slug,
                'icon': {'content': '🛍️', 'background': '#E8EEE3'},
                'parameters': [{'name': v['variable'], 'form': 'llm', 'description': v['label']} for v in fields]})
        record = console.request('GET', '/workspaces/current/tool-provider/workflow/get', params={'workflow_app_id': item['app_id']})
        provider_id = record.get('id') or record.get('workflow_tool_id')
        if not provider_id:
            raise RuntimeError('Workflow provider did not return its ID')
        metadata = console.request('GET', '/workspaces/current/tool-provider/workflow/tools', params={'workflow_tool_id': provider_id})
        tools_manifest[slug] = {'provider_id': provider_id, 'metadata': metadata}
        print('Workflow tool ready:', slug)
    (ROOT / '.runtime/workflow-tools.json').write_text(json.dumps(tools_manifest))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--plugins', action='store_true')
    parser.add_argument('--api-tools', action='store_true')
    parser.add_argument('--workflow-tools', action='store_true')
    args = parser.parse_args()
    console = DifyConsole()
    if args.plugins:
        plugins(console)
    if args.api_tools:
        api_tools(console)
        api_tools(console, actions=True)
    if args.workflow_tools:
        workflow_tools(console)
