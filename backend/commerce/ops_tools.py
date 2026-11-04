from datetime import date
from sqlalchemy import select
from . import ops_data as data, ops_work as work, ops_media as media
from .auth import scope_shop_ids
from .models import OpsScript, OpsJob
from .ops_schemas import Day, TaskUpdate, ScriptSave, RenderCreate

READ_OPERATIONS = {'ops_overview', 'ops_videos', 'ops_peers', 'ops_script_context', 'ops_peer_context',
                   'ops_tasks', 'ops_report', 'ops_scripts', 'ops_jobs'}
WRITE_OPERATIONS = {'ops_sop_generate', 'ops_task_update', 'ops_script_save', 'ops_render'}


def execute(db, user, operation, args):
    if operation == 'ops_overview':
        result = data.overview(db, user, int(args.get('days', 7)))
        result['金额展示'] = {k: f'{v / 100:,.2f} 元' if v is not None else '暂无数据'
                            for k, v in result['total'].items() if k.endswith('_fen')}
        return result
    if operation == 'ops_videos':
        return data.videos(db, user)
    if operation == 'ops_peers':
        return {'items': data.records(db, user, 'peer_references')}
    if operation == 'ops_script_context':
        return media.context(db, user, args.get('product_id'), args.get('reference_id'))
    if operation == 'ops_peer_context':
        return data.record(db, user, 'peer_references', args.get('reference_id')).payload
    if operation == 'ops_tasks':
        return work.tasks(db, user, date.fromisoformat(args['date']) if args.get('date') else None)
    if operation == 'ops_report':
        return work.daily_report(db, user, date.fromisoformat(args['date']) if args.get('date') else None)
    if operation == 'ops_scripts':
        return {'items': [media.script_view(s) for s in db.scalars(select(OpsScript).where(OpsScript.shop_id.in_(scope_shop_ids(user))).order_by(OpsScript.created_at.desc(), OpsScript.id.desc()).limit(30))]}
    if operation == 'ops_jobs':
        return {'items': [media.job_view(j) for j in db.scalars(select(OpsJob).where(OpsJob.shop_id.in_(scope_shop_ids(user))).order_by(OpsJob.created_at.desc(), OpsJob.id.desc()).limit(30))]}
    if operation == 'ops_sop_generate':
        return work.generate(db, user, Day.model_validate(args).date)
    if operation == 'ops_task_update':
        task_id = args.get('task_id')
        payload = TaskUpdate.model_validate({k: v for k, v in args.items() if k != 'task_id'})
        return work.change_task(db, user, task_id, payload.model_dump(exclude_none=True))
    if operation == 'ops_script_save':
        script_id = args.get('script_id')
        payload = ScriptSave.model_validate({k: v for k, v in args.items() if k != 'script_id'})
        return media.save_script(db, user, payload.model_dump(), script_id)
    if operation == 'ops_render':
        payload = RenderCreate.model_validate({k: v for k, v in args.items() if k != 'script_id'})
        return media.enqueue(db, user, args.get('script_id'), payload.expected_version)
