"""Chat adapters call the same validated business paths as sidebar pages."""
from typing import Literal
from pydantic import TypeAdapter
from sqlalchemy import select

from . import ops_api, ops_imports, ops_data, platform_api, platform_service
from .auth import scope_shop_ids, scope_environments
from .business import fail
from .chat_workspaces import Workspace, WORKSPACES, OPERATIONS, allowed_writes
from .models import OpsImport, Draft, ChatRun
from .ops_schemas import (ImportFile, ImportTemplate, Schedule, PeerCreate,
                          ScriptGenerate, ScriptVersion)
from .schemas import Content, Snapshot, Strict


class Empty(Strict):
    pass


class RunFilter(Strict):
    workspace: Workspace | None = None
    status: Literal['running', 'succeeded', 'failed', 'interrupted'] | None = None


def take(args, name):
    value = args.pop(name, None)
    if not isinstance(value, str) or not value.strip() or len(value) > 180:
        fail(422, f'缺少有效的{name}')
    return value


def connection_view(value):
    # Platform credentials and app identifiers stay in the native configuration form.
    keys = {'id', 'shop_id', 'provider', 'mode', 'status', 'has_secret', 'has_token',
            'missing', 'capabilities', 'expires_at', 'last_sync_at', 'next_sync_at',
            'enabled', 'interval_minutes', 'version', 'resources', 'worker_enabled'}
    return {key: val for key, val in value.items() if key in keys}


def execute(db, user, operation, arguments, workspace):
    # Lazy import avoids a router cycle; these handlers require explicit validated arguments.
    from . import main
    args = dict(arguments)
    if operation == 'chat_run_records':
        body = RunFilter.model_validate(args)
        if workspace != 'audit' and body.workspace not in {None, workspace}:
            fail(403, '请在运行记录栏目查询其他栏目的运行摘要')
        stmt = main.chat_scope(select(ChatRun).where(ChatRun.user_id == user.id), user)
        target = body.workspace or (workspace if workspace != 'audit' else None)
        if target:
            stmt = stmt.where(ChatRun.workspace == target)
        if body.status:
            stmt = stmt.where(ChatRun.status == body.status)
        return {'items': [{key: getattr(row, key) for key in ['id', 'workspace', 'shop_id', 'status', 'error', 'created_at']}
                          for row in db.scalars(stmt.order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).limit(30))]}
    if operation == 'workspace_context':
        Empty.model_validate(args)
        return {'workspace': workspace, 'name': WORKSPACES[workspace],
                'allowed_writes': sorted(allowed_writes(workspace)),
                'operations': {op: desc for op, (kind, desc) in OPERATIONS.items()
                               if kind == 'read' or op in allowed_writes(workspace)},
                'forms': {'ops-imports': '页面上传报表后，可用ops_imports找到校验批次，再读取预览并提交。',
                          'ops-videos': '素材上传使用页面表单；聊天通过ops_assets读取已上传素材。',
                          'platform': '凭据填写、账号授权和断开连接使用平台表单；不要在聊天中索取密钥。'},
                'restrictions': '不执行退款、售后申请、商品上架或外部发布。栏目隔离对话，业务数据按授权店铺共享。'}
    if operation == 'ops_schedule_set':
        sid = take(args, 'shop_id')
        return ops_api.schedule(sid, Schedule.model_validate(args), user, db)
    if operation == 'ops_peer_create':
        return ops_api.peer_create(PeerCreate.model_validate(args), user, db)
    if operation == 'ops_peer_analyze':
        target = take(args, 'reference_id'); Empty.model_validate(args)
        return ops_api.analyze(target, user, db)
    if operation == 'ops_script_generate':
        return ops_api.script_generate(ScriptGenerate.model_validate(args), user, db)
    if operation in {'ops_script_review', 'ops_script_rewrite'}:
        target = take(args, 'script_id')
        body = ScriptVersion.model_validate(args)
        handler = ops_api.script_review if operation == 'ops_script_review' else ops_api.script_rewrite
        return handler(target, body, user, db)
    if operation == 'ops_render_retry':
        target = take(args, 'job_id'); Empty.model_validate(args)
        return ops_api.retry(target, user, db)
    if operation in {'ops_import_inspect', 'ops_import_preview'}:
        body = ImportFile.model_validate(args)
        if operation == 'ops_import_inspect':
            return ops_api.inspect_import(body, user)
        return ops_api.preview(body, user, db)
    if operation == 'ops_import_mapping_save':
        return ops_api.save_import_mapping(ImportTemplate.model_validate(args), user, db)
    if operation == 'ops_import_fields':
        kind = take(args, 'kind'); Empty.model_validate(args)
        return {'fields': ops_imports.fields(kind), **ops_api.template(kind, user)}
    if operation in {'ops_import_batch', 'ops_import_commit'}:
        target = take(args, 'batch_id'); Empty.model_validate(args)
        # Do not reveal previews from a different operator or store scope.
        batch = db.scalar(select(OpsImport).where(OpsImport.id == target, OpsImport.user_id == user.id))
        if not batch or set(batch.shop_scope) != set(scope_shop_ids(user)):
            fail(404, '当前范围内无此导入批次')
        if operation == 'ops_import_commit':
            return ops_api.commit(target, user, db)
        if batch.kind not in ops_data.KEYS:
            fail(410, '该报表类型已停用')
        return dict(id=batch.id, kind=batch.kind, filename=batch.filename, status=batch.status,
                    valid_rows=len(batch.rows), error_count=len(batch.errors),
                    errors=batch.errors[:100], preview=batch.rows[:5])
    if operation == 'content_save':
        body = Content.model_validate(args)
        # A retried identical save should return the original draft.
        for row in db.scalars(select(Draft).where(Draft.user_id == user.id,
                               Draft.shop_id == user.shop_id, Draft.kind == body.kind)):
            if row.source_scope == scope_environments(user) and row.payload == body.model_dump():
                return main.draft_view(row)
        return main.content(body, user, db)
    if operation == 'competitors_import':
        items = args.pop('items', None); Empty.model_validate(args)
        return main.import_competitors(TypeAdapter(list[Snapshot]).validate_python(items), user, db)
    if operation.startswith('platform_') and operation != 'platform_connections':
        target = take(args, 'connection_id')
        # Apply the business scope before using the setup UI's broader connection-management API.
        row = platform_service.connection(db, user, target)
        if row.shop_id not in scope_shop_ids(user):
            fail(404, '接入配置不在当前业务范围内')
        if operation == 'platform_sync':
            return platform_api.sync(target, platform_api.SyncRequest.model_validate(args), user, db)
        if operation == 'platform_schedule_set':
            return connection_view(platform_api.schedule(target, platform_api.ScheduleRequest.model_validate(args), user, db))
        if operation == 'platform_product_facts_set':
            product_id = take(args, 'product_id')
            return platform_api.save_product_facts(target, product_id, platform_api.ProductFacts.model_validate(args), user, db)
        Empty.model_validate(args)
        if operation == 'platform_syncs':
            return platform_api.syncs(target, user, db)
        if operation == 'platform_products':
            return platform_api.product_facts(target, user, db)
        if operation == 'platform_disconnect':
            return connection_view(platform_api.disconnect(target, user, db))
        return connection_view(platform_api.connect(target, user, db))
    Empty.model_validate(args)
    readers = {
        'ops_schedules': ops_api.schedules, 'ops_assets': ops_api.assets,
        'ops_imports': ops_api.imports, 'ops_import_mappings': ops_api.import_mappings,
        'content_list': main.drafts, 'audit_events': main.audit_events,
        'ops_demo_import': ops_api.demo,
    }
    if operation == 'platform_connections':
        result = platform_api.connections(user, db)
        return {'items': [connection_view(item) for item in result['items']
                          if item['shop_id'] in scope_shop_ids(user)],
                'worker_enabled': result['worker_enabled']}
    return readers[operation](user, db)
