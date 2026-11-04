import asyncio
import json
import logging
import shutil
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user, scope_shop_ids
from .business import fail, audit
from .db import get_db
from .models import OpsImport, OpsRecord, OpsAsset, OpsScript, OpsJob, OpsSchedule, now, uid
from . import ops_data as data, ops_work as work, ops_media as media
from .ops_schemas import ImportFile, ImportTemplate, Day, TaskUpdate, ScriptSave, ScriptGenerate, ScriptVersion, RenderCreate, PeerCreate, Schedule
from . import video_parser as parsing
from . import ops_imports as importing

router = APIRouter(prefix='/api/ops', tags=['运营工作闭环'])
logger = logging.getLogger('commerce.video_parser')


@router.get('/overview')
def overview(days: int = Query(7, ge=1, le=90), end: date | None = None, user=Depends(current_user), db: Session = Depends(get_db)):
    return data.overview(db, user, days, end)


@router.get('/imports')
def imports(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [dict(id=r.id, kind=r.kind, filename=r.filename, status=r.status,
                          rows=len(r.rows), errors=len(r.errors), created_at=r.created_at)
                      for r in db.scalars(select(OpsImport).where(OpsImport.user_id == user.id)
                                          .order_by(OpsImport.created_at.desc()).limit(50))
                      if r.kind in data.KEYS and set(r.shop_scope).issubset(set(scope_shop_ids(user)))],
            'kinds': list(data.KEYS), 'format': 'CSV、JSON、JSONL或Excel (.xlsx)，支持字段映射'}


@router.post('/imports/preview')
def preview(body: ImportFile, user=Depends(current_user), db: Session = Depends(get_db)):
    return data.preview(db, user, body.kind, body.filename, body.text, prepared=importing.prepare(body, user))


@router.post('/imports/inspect')
def inspect_import(body: ImportFile, user=Depends(current_user)):
    return importing.inspect(body)


@router.get('/import-mappings')
def import_mappings(user=Depends(current_user), db: Session = Depends(get_db)):
    return importing.templates(db, user)


@router.post('/import-mappings')
def save_import_mapping(body: ImportTemplate, user=Depends(current_user), db: Session = Depends(get_db)):
    return importing.save_template(db, user, body)


@router.post('/imports/{batch_id}/commit')
def commit(batch_id: str, user=Depends(current_user), db: Session = Depends(get_db)):
    return data.commit(db, user, batch_id)


@router.get('/templates/{kind}')
def template(kind: str, user=Depends(current_user)):
    if kind not in data.KEYS:
        fail(404, '模板不存在')
    # Return a small, real sample with headers; no arbitrary file paths are accepted.
    path = data.FIXTURES / (kind + '.csv')
    lines = path.read_text(encoding='utf-8-sig').splitlines()
    return {'filename': kind + '.csv', 'text': '\ufeff' + '\n'.join(lines[:4]) + '\n'}


@router.post('/demo/import')
def demo(user=Depends(current_user), db: Session = Depends(get_db)):
    if any(getattr(user, 'shop_infos', {}).get(sid, {}).get('mode') == 'live' for sid in scope_shop_ids(user)):
        fail(409, '正式店铺不能加载测试数据，请切换到演示或演练店铺')
    manifest = json.loads((data.FIXTURES / 'manifest.json').read_text())
    counts = {}
    kinds = ['shops', 'staff', 'products', 'videos', 'orders', 'refunds', 'daily_reports', 'inventory',
             'video_daily', 'sop_templates', 'tasks', 'peer_references']
    for kind in kinds:
        rows = [json.loads(line) for line in (data.FIXTURES / f'{kind}.jsonl').read_text().splitlines()]
        if kind == 'sop_templates':
            rows = [{**r, 'shop_id': sid} for r in rows for sid in scope_shop_ids(user)]
        else:
            rows = [r for r in rows if r['shop_id'] in scope_shop_ids(user)]
        normalized = [data.normalize(kind, r, user) for r in rows]
        counts[kind] = data.apply_rows(db, user, kind, normalized, 'demo-' + manifest['dataset_version'])
    for sid in scope_shop_ids(user):
        clock = db.scalar(select(OpsRecord).where(OpsRecord.kind == 'clock', OpsRecord.shop_id == sid))
        if not clock:
            db.add(OpsRecord(shop_id=sid, kind='clock', external_id='reference', payload={'as_of': manifest['as_of'], 'is_demo': True}))
        schedule = db.get(OpsSchedule, sid)
        if not schedule:
            db.add(OpsSchedule(shop_id=sid, user_id=user.id, enabled=True, hour=9))
    for row in [json.loads(line) for line in (data.FIXTURES / 'assets.jsonl').read_text().splitlines()]:
        if row['shop_id'] not in scope_shop_ids(user) or db.get(OpsAsset, row['asset_id']):
            continue
        relative = 'assets/' + row['asset_id'] + '.mp4'
        path = media.local_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        source = (data.FIXTURES / row['media_path']).resolve()
        if not source.is_relative_to(data.FIXTURES.resolve()) or not source.is_file():
            fail(409, '缺少测试素材，请先运行generate_ops_data --media')
        shutil.copyfile(source, path)
        db.add(OpsAsset(id=row['asset_id'], shop_id=row['shop_id'], group_id=row['group_id'],
                       title=row['asset_id'], relative_path=relative, metadata_json={**media.probe(path), 'is_demo': True,
                       'visual': row['visual']}))
    db.flush()
    for sid in scope_shop_ids(user):
        peer_asset = 'TEST-PEER-ASSET-' + sid
        if not db.get(OpsAsset, peer_asset):
            relative = 'assets/' + peer_asset + '.mp4'
            path = media.local_path(relative)
            shutil.copyfile(data.FIXTURES / 'media/peer_reference.mp4', path)
            db.add(OpsAsset(id=peer_asset, shop_id=sid, group_id='', title='虚构同行占位参考',
                relative_path=relative, metadata_json={**media.probe(path), 'is_demo': True, 'visual': '占位动画，非实拍'}))
        for row in db.scalars(select(OpsRecord).where(OpsRecord.kind == 'peer_references', OpsRecord.shop_id == sid)):
            row.payload = {**row.payload, 'asset_id': peer_asset}
    db.flush()
    for row in json.loads((data.FIXTURES / 'video_scripts.json').read_text()):
        if row['shop_id'] not in scope_shop_ids(user) or db.get(OpsScript, row['script_id']):
            continue
        product_id = manifest['heroes'][row['shop_id']]
        payload = dict(product_id=product_id, reference_id=row['source_reference_id'], duration_seconds=12,
                       segments=[dict(asset_id=s['asset_id'], in_seconds=s['source_in_seconds'],
                                      out_seconds=s['source_out_seconds'], caption=s['caption']) for s in row['segments']],
                       generation='导入的合成测试脚本')
        db.add(OpsScript(id=row['script_id'], shop_id=row['shop_id'], user_id=user.id, title=row['title'], payload=payload))
    audit(db, user, 'ops.demo.import', {'counts': counts, 'dataset': manifest['dataset_version']})
    db.commit()
    return {'counts': counts, 'as_of': manifest['as_of'], 'is_demo': True, 'message': '测试数据已加载；已回填的任务和脚本不会重置。'}


@router.get('/tasks')
def tasks(day: date | None = None, user=Depends(current_user), db: Session = Depends(get_db)):
    return work.tasks(db, user, day)


@router.post('/tasks/generate')
def generate(body: Day, user=Depends(current_user), db: Session = Depends(get_db)):
    return work.generate(db, user, body.date)


@router.patch('/tasks/{task_id}')
def task_update(task_id: str, body: TaskUpdate, user=Depends(current_user), db: Session = Depends(get_db)):
    return work.change_task(db, user, task_id, body.model_dump(exclude_none=True))


@router.get('/daily-report')
def report(day: date | None = None, user=Depends(current_user), db: Session = Depends(get_db)):
    return work.daily_report(db, user, day)


@router.get('/schedules')
def schedules(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [dict(shop_id=r.shop_id, enabled=r.enabled, hour=r.hour, last_date=r.last_date, error=r.error)
                      for r in db.scalars(select(OpsSchedule).where(OpsSchedule.shop_id.in_(scope_shop_ids(user))))]}


@router.put('/schedules/{shop_id}')
def schedule(shop_id: str, body: Schedule, user=Depends(current_user), db: Session = Depends(get_db)):
    data.allowed(user, shop_id)
    row = db.get(OpsSchedule, shop_id)
    if not row:
        row = OpsSchedule(shop_id=shop_id, user_id=user.id)
        db.add(row)
    row.enabled, row.hour, row.user_id, row.last_date, row.error = body.enabled, body.hour, user.id, None, None
    db.commit()
    return {'ok': True}


@router.get('/videos')
def videos(user=Depends(current_user), db: Session = Depends(get_db)):
    return data.videos(db, user)


@router.post('/videos/parse', response_model=parsing.VideoParseResponse,
             summary='通过预留工具解析视频网址、字幕与文案',
             description='仅解析并返回结构化内容，不下载媒体、不创建同行记录、不调用分析模型。工具尚未接入时返回501。',
             responses={status: {'model': parsing.VideoParseError, 'description': description}
                        for status, description in [(501, '解析工具尚未接入'), (400, '网址不受工具支持'),
                                                    (502, '解析失败或工具返回无效数据'), (504, '解析工具超时')]})
async def parse_video(body: parsing.VideoParseRequest, user=Depends(current_user),
                      parser: parsing.VideoParser = Depends(parsing.get_video_parser)):
    data.allowed(user, body.shop_id)
    try:
        result = await asyncio.wait_for(parser.parse(str(body.url), shop_id=body.shop_id),
                                        timeout=parsing.PARSER_TIMEOUT_SECONDS)
        # Validate even if a future adapter accidentally returns a raw tool dict.
        content = parsing.VideoParseContent.model_validate(result).model_dump()
        return parsing.VideoParseResponse(**content, shop_id=body.shop_id, source_url=body.url, parser=parser.name)
    except parsing.ParserNotConfigured:
        status, code, detail = 501, 'video_parser_not_configured', '视频解析工具尚未接入，当前不能解析视频或提取字幕；接口已预留。'
    except parsing.UnsupportedVideoURL:
        status, code, detail = 400, 'video_url_unsupported', '解析工具暂不支持此视频网址。'
    except TimeoutError:
        status, code, detail = 504, 'video_parse_timeout', '视频解析超时，请稍后重试。'
    except Exception:
        logger.exception('Video parser failed')
        status, code, detail = 502, 'video_parse_failed', '视频解析失败或工具返回内容无效，请检查解析工具后重试。'
    return JSONResponse(status_code=status, content={'code': code, 'detail': detail})


@router.get('/peers')
def peers(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': data.records(db, user, 'peer_references')}


@router.post('/peers')
def peer_create(body: PeerCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    data.allowed(user, body.shop_id)
    if body.asset_id and media.asset(db, user, body.asset_id).shop_id != body.shop_id:
        fail(422, '参考素材店铺不匹配')
    peer_id = uid()
    value = {**body.model_dump(), 'reference_id': peer_id, 'backend_completion_rate': None,
             'backend_paid_fen': None, 'is_demo': False}
    db.add(OpsRecord(shop_id=body.shop_id, kind='peer_references', external_id=peer_id, payload=value))
    db.commit()
    return value


@router.post('/peers/{reference_id}/analyze')
def analyze(reference_id: str, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.analyze_peer(db, user, reference_id)


@router.get('/assets')
def assets(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [media.asset_view(a) for a in db.scalars(select(OpsAsset).where(OpsAsset.shop_id.in_(scope_shop_ids(user))))],
            'products': data.records(db, user, 'products')}


@router.post('/assets/upload')
async def upload(request: Request, shop_id: str, filename: str, group_id: str = '', user=Depends(current_user), db: Session = Depends(get_db)):
    data.allowed(user, shop_id)
    if len(group_id) > 80 or len(filename) > 180:
        fail(422, '名称或款式ID过长')
    asset_id = uid()
    relative = f'assets/{asset_id}.mp4'
    path = media.local_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with path.open('wb') as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > 64 * 1024 * 1024:
                    fail(413, '素材最大64MB')
                stream.write(chunk)
        metadata = await asyncio.to_thread(media.probe, path)
        row = OpsAsset(id=asset_id, shop_id=shop_id, group_id=group_id,
            title=filename.replace('\\', '/').rsplit('/', 1)[-1], relative_path=relative, metadata_json=metadata)
        db.add(row)
        db.commit()
        return media.asset_view(row)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@router.get('/assets/{asset_id}/video')
def asset_video(asset_id: str, user=Depends(current_user), db: Session = Depends(get_db)):
    row = media.asset(db, user, asset_id)
    path = media.local_path(row.relative_path)
    if not path.is_file():
        fail(404, '素材文件缺失')
    return FileResponse(path, media_type='video/mp4')


@router.get('/scripts')
def scripts(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [media.script_view(r) for r in db.scalars(select(OpsScript).where(
        OpsScript.shop_id.in_(scope_shop_ids(user))).order_by(OpsScript.created_at.desc()).limit(100))]}


@router.post('/scripts')
def script_create(body: ScriptSave, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.save_script(db, user, body.model_dump())


@router.patch('/scripts/{script_id}')
def script_update(script_id: str, body: ScriptSave, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.save_script(db, user, body.model_dump(), script_id)


@router.post('/scripts/generate')
def script_generate(body: ScriptGenerate, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.generate_script(db, user, body.product_id, body.reference_id, body.brief)


@router.post('/scripts/{script_id}/render')
def render(script_id: str, body: RenderCreate, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.enqueue(db, user, script_id, body.expected_version)


@router.post('/scripts/{script_id}/review')
def script_review(script_id: str, body: ScriptVersion, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.check_script(db, user, script_id, body.expected_version)


@router.post('/scripts/{script_id}/rewrite')
def script_rewrite(script_id: str, body: ScriptVersion, user=Depends(current_user), db: Session = Depends(get_db)):
    return media.rewrite_script(db, user, script_id, body.expected_version)


@router.get('/jobs')
def jobs(user=Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [media.job_view(j) for j in db.scalars(select(OpsJob).where(OpsJob.shop_id.in_(scope_shop_ids(user)))
                                                          .order_by(OpsJob.created_at.desc()).limit(100))]}


@router.post('/jobs/{job_id}/retry')
def retry(job_id: str, user=Depends(current_user), db: Session = Depends(get_db)):
    row = media.get_job(db, user, job_id, True)
    if row.status != 'failed':
        fail(409, '仅失败任务可以重试')
    row.status, row.error, row.finished_at = 'queued', None, None
    audit(db, user, 'ops.render.retry', {'id': row.id})
    db.commit()
    return media.job_view(row)


@router.get('/jobs/{job_id}/{kind}')
def output(job_id: str, kind: str, user=Depends(current_user), db: Session = Depends(get_db)):
    row = media.get_job(db, user, job_id)
    if row.status != 'succeeded':
        fail(409, '成片尚未完成')
    files = {'video': ('output.mp4', 'video/mp4'), 'cover': ('cover.jpg', 'image/jpeg'), 'subtitles': ('captions.srt', 'application/x-subrip')}
    if kind not in files:
        fail(404, '产物不存在')
    name, mime = files[kind]
    path = media.local_path(f'jobs/{row.id}/{name}')
    if not path.is_file():
        fail(404, '产物文件缺失，请重新剪辑')
    return FileResponse(path, media_type=mime, filename=f'{row.id}-{name}')
