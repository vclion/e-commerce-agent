import json
import httpx
from pydantic import BaseModel, ConfigDict, Field
from ..config import ROOT, settings


class Extracted(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_id: str
    quote: str = Field(min_length=1, max_length=500)
    kind: str = 'semantic'
    subject: str = Field(max_length=180)
    predicate: str = Field(max_length=100)
    title: str = Field(max_length=180)
    content: str = Field(max_length=2000)


class MaintenanceResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    memories: list[Extracted] = Field(default_factory=list, max_length=12)
    summary: str = Field(default='', max_length=3000)
    lessons: list[str] = Field(default_factory=list, max_length=3)
    title: str = Field(default='', max_length=180)
    overview: str = Field(default='', max_length=600)


def maintain(action, payload):
    apps = json.loads((ROOT / 'dify/apps.lock.json').read_text())
    keys = json.loads((ROOT / '.runtime/dify-app-keys.json').read_text())
    app_id = apps['memory-maintenance']['app_id']
    response = httpx.post(settings.dify_base_url.rstrip('/')+'/workflows/run',
        headers={'Authorization': 'Bearer '+keys[app_id]},
        json={'inputs': {'context_ref': '', 'query': action,
              'arguments': json.dumps(payload, ensure_ascii=False)}, 'user': 'memory-worker', 'response_mode': 'blocking'},
        timeout=httpx.Timeout(settings.memory_reactive_timeout if action == 'reactive_context' else 60, connect=5))
    response.raise_for_status()
    data = response.json()['data']
    if data['status'] != 'succeeded':
        raise ValueError('记忆整理工作流失败')
    raw = data['outputs']['result'].strip()
    if raw.startswith('```'):
        raw = raw.split('\n',1)[1].rsplit('```',1)[0]
    return MaintenanceResult.model_validate_json(raw)
