import asyncio
import json
import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import business as b
from . import knowledge
from .tool_runtime import write_guard, execution_effects
from .auth import bearer, current_user, digest, issue_token, password_matches, require_role, resolve_token, scoped_actor, scope_shop_ids, shop_view, shop_info, scope_fingerprint, scope_environments
from .config import ROOT, settings
from .db import Base, SessionLocal, engine, get_db, initialize_schema
from .catalog import DATASET, SHOPS
from .models import AccessToken, Audit, ChatRun, Competitor, Conversation, Draft, Manifest, User, now, uid
from .schemas import Chat, Compare, Content, Login, Search, Snapshot, ToolCall
from .ops_api import router as ops_router
from . import ops_worker, ops_tools
from .platform_api import router as platform_router
from . import platform_service
from . import workspace_tools
from . import recovery
from .memory.api import router as memory_router
from .memory import context as memory_context, service as memory_service, tools as memory_tools
from .memory.reactive import ContextLimitError, is_context_limit
from .memory.models import ChatSession, MemoryRunContext
from .memory.policy import preferences as memory_preferences
from .chat_workspaces import Workspace, WORKSPACES, READ_OPERATIONS, WRITE_OPERATIONS, EFFECT_LABELS, allowed_writes

logger = logging.getLogger('commerce')


@asynccontextmanager
async def lifespan(app):
    initialize_schema()
    worker = asyncio.create_task(ops_worker.run()) if settings.ops_worker_enabled else None
    platform_worker = asyncio.create_task(platform_service.run()) if settings.platform_worker_enabled else None
    recovery_worker = asyncio.create_task(recovery.run())
    try:
        yield
    finally:
        recovery_worker.cancel()
        try:
            await recovery_worker
        except asyncio.CancelledError:
            pass
        if platform_worker:
            platform_worker.cancel()
            try:
                await platform_worker
            except asyncio.CancelledError:
                pass
        if worker:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass


app = FastAPI(title='衣序运营 Agent', version='0.2.0', lifespan=lifespan)
app.include_router(ops_router)
app.include_router(platform_router)
app.include_router(memory_router)


@app.middleware('http')
async def request_id(request: Request, call_next):
    request.state.request_id = uid()
    response = await call_next(request)
    response.headers['X-Request-ID'] = request.state.request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(IntegrityError)
async def integrity_error(request, exc):
    return JSONResponse(status_code=409, content={'detail': '数据状态发生冲突，请刷新后重试'})


@app.get('/api/health')
def health(db: Session = Depends(get_db)):
    db.execute(text('SELECT 1'))
    return {'status': 'ok', 'is_demo': True, 'dify': {
        'operator': bool(settings.dify_operator_key)},
        'dataset': db.get(Manifest, DATASET).data if db.get(Manifest, DATASET) else None}


@app.post('/api/auth/login')
def login(body: Login, db: Session = Depends(get_db)):
    username = 'operator-a' if body.username == 'operator' else body.username
    user = db.scalar(select(User).where(User.username == username))
    if not user or not password_matches(body.password, user.password_hash):
        raise HTTPException(401, '账号或密码不正确')
    require_role(user, 'operator')
    return {'access_token': issue_token(db, user), 'user': user_view(scoped_actor(db, user))}


def user_view(user):
    return {**{k: getattr(user, k) for k in ['id', 'username', 'role', 'shop_id', 'display_name']},
            'shop': shop_view(user), 'shops': [{'id': sid, **shop_info(user, sid)} for sid in user.allowed_shop_ids]}


@app.get('/api/auth/me')
def me(user: User = Depends(current_user)):
    return user_view(user)


@app.post('/api/auth/logout')
def logout(credentials=Depends(bearer), user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.execute(delete(AccessToken).where(AccessToken.digest == digest(credentials.credentials)))
    db.commit()
    return {'ok': True}


@app.post('/api/products/search')
def products(body: Search, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.search_products(db, user, body)


@app.post('/api/products/compare')
def compare(body: Compare, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return {'items': [b.product_view(b.scoped_product(db, user, p), user) for p in body.product_ids]}


@app.get('/api/orders')
def orders(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.get_orders(db, user)


@app.get('/api/after-sales')
def after_sales(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.get_after_sales(db, user)


@app.get('/api/drafts')
def drafts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Draft).where(Draft.user_id == user.id, Draft.kind.in_(['marketing', 'report']),
        Draft.shop_id.in_(scope_shop_ids(user)) | Draft.shop_id.is_(None),
        cast(Draft.source_scope, JSONB).contained_by(scope_environments(user)))
    if user.shop_id:
        stmt = stmt.where(Draft.shop_id == user.shop_id)
    return {'items': [draft_view(d) for d in db.scalars(stmt.order_by(Draft.created_at.desc()).limit(30))]}


def draft_view(d):
    return {k: getattr(d, k) for k in ['id', 'shop_id', 'kind', 'payload', 'version', 'status', 'created_at']}


@app.post('/api/content')
def content(body: Content, user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_role(user, 'operator')
    d = Draft(user_id=user.id, shop_id=user.shop_id, source_scope=scope_environments(user),
              kind=body.kind, payload=body.model_dump())
    db.add(d)
    db.commit()
    return draft_view(d)


@app.get('/api/analytics')
def analytics(days: int = Query(7, ge=1, le=90), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.analytics(db, user, days)


@app.get('/api/selection')
def selection(category: str = '', min_margin: float = Query(.2, ge=0, lt=1),
              max_price_fen: int = Query(10000000, gt=0), user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.selection(db, user, category, min_margin, max_price_fen)


@app.get('/api/competitors')
def competitors(product_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return b.competitors(db, user, product_id)


@app.post('/api/competitors/import')
def import_competitors(body: list[Snapshot], user: User = Depends(current_user), db: Session = Depends(get_db)):
    require_role(user, 'operator')
    if not 1 <= len(body) <= 100:
        b.fail(422, '单次导入 1～100 条')
    for item in body:
        p = b.scoped_product(db, user, item.product_id)
        if item.captured_at.tzinfo is None or item.captured_at > now():
            b.fail(422, '采集时间必须带时区且不能在未来')
        db.add(Competitor(shop_id=p.shop_id, category=p.category, **item.model_dump(), is_demo=False))
    db.commit()
    return {'imported': len(body), 'notice': '仅保存提供的快照；未访问来源 URL 或核验真实性'}


@app.get('/api/audit')
def audit_events(user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Audit).where(Audit.user_id == user.id)
    if user.shop_id:
        stmt = stmt.where(Audit.details['shop_id'].as_string() == user.shop_id)
    return {'items': [{'operation': a.operation, 'details': a.details, 'request_id': a.request_id,
                       'created_at': a.created_at} for a in db.scalars(stmt
                                                                 .order_by(Audit.created_at.desc()).limit(30))]}


@app.get('/api/knowledge/documents/{document_id}')
def knowledge_document(document_id: str, user: User = Depends(current_user)):
    doc = knowledge.document_record(user, document_id)
    return {'id': doc['document_id'], 'title': doc['title'], 'version': doc['version'],
            'text': (ROOT / doc['file']).read_text()}


@app.post('/tools/read')
@app.post('/tools/execute')
def tool_execute(body: ToolCall, request: Request, authorization: str = Header(''),
                 x_context_ref: str = Header(''), db: Session = Depends(get_db)):
    if not secrets.compare_digest(authorization, f'Bearer {settings.tool_service_key}'):
        b.fail(401, '工具服务认证失败')
    user, context = resolve_token(db, x_context_ref, 'tool')
    user = scoped_actor(db, user, context.shop_id)
    memory_context.validate_token(db, context)
    if (context.scope_fingerprint and context.scope_fingerprint != scope_fingerprint(user)) or (
        not context.scope_fingerprint and any(sid in user.shop_infos for sid in scope_shop_ids(user))):
        b.fail(409, '数据来源范围已变化，请开启新对话')
    args = body.arguments
    op = body.operation
    if op in (ops_tools.WRITE_OPERATIONS | WRITE_OPERATIONS) and op not in allowed_writes(context.workspace):
        b.fail(403, '当前栏目不提供此写入操作，请切换到对应栏目执行')
    if request.url.path == '/tools/read' and op not in {
        'products', 'compare', 'orders', 'after_sales', 'analytics', 'selection',
        'competitors', 'product_detail', 'knowledge', 'clarify', *ops_tools.READ_OPERATIONS, *READ_OPERATIONS, *memory_tools.READ_OPERATIONS
    }:
        b.fail(403, '研究工具只允许读取资料')
    is_write = op in (ops_tools.WRITE_OPERATIONS | WRITE_OPERATIONS | memory_tools.WRITE_OPERATIONS)
    if is_write:
        previous = recovery.tool_record(db, context.request_id, user.id, op, args)
        if previous:
            return recovery.cached_result(previous)
    # Serialize and enforce the per-run call budget outside the model prompt.
    db.scalar(select(AccessToken).where(AccessToken.digest == context.digest).with_for_update())
    db.refresh(context)
    memory_context.validate_token(db, context)
    attempts = db.scalar(select(func.count()).select_from(Audit).where(
        Audit.request_id == context.request_id, Audit.operation == 'tool.attempt'))
    if attempts >= 8:
        b.fail(429, '本轮工具调用已达到8次上限，请根据现有资料返回结果')
    attempt = {'operation': op}
    if is_write:
        attempt['arguments_hash'] = recovery.fingerprint(args)
    b.audit(db, user, 'tool.attempt', attempt, context.request_id)
    db.commit()
    release_guard = write_guard(db, context.request_id, user.id) if is_write else lambda: None
    release_tracker, record_id, record = lambda: None, None, None
    try:
        if is_write:
            record, created = recovery.reserve_tool(db, context.request_id, user.id, op, args)
            if record and not created:
                return recovery.cached_result(record)
            record_id = record.id if record else None
            release_tracker = recovery.track_commits(db, record)
        if op in memory_tools.READ_OPERATIONS | memory_tools.WRITE_OPERATIONS:
            result = memory_tools.execute(db, user, context, op, dict(args))
        elif op in READ_OPERATIONS | WRITE_OPERATIONS:
            result = workspace_tools.execute(db, user, op, args, context.workspace)
        elif op in ops_tools.READ_OPERATIONS | ops_tools.WRITE_OPERATIONS:
            result = ops_tools.execute(db, user, op, args)
        elif op == 'products':
            result = b.search_products(db, user, Search.model_validate(args))
        elif op == 'product_detail':
            result = b.product_view(b.scoped_product(db, user, str(args.get('product_id', ''))), user)
        elif op == 'compare':
            params = Compare.model_validate(args)
            result = {'items': [b.product_view(b.scoped_product(db, user, p), user) for p in params.product_ids]}
        elif op == 'orders':
            result = b.get_orders(db, user)
        elif op == 'after_sales':
            result = b.get_after_sales(db, user)
        elif op == 'analytics':
            result = b.analytics_for_model(b.analytics(db, user, int(args.get('days', 7))))
        elif op == 'selection':
            result = b.selection(db, user, str(args.get('category', '')), float(args.get('min_margin', .2)),
                                 int(args.get('max_price_fen', 10000000)))
        elif op == 'competitors':
            result = b.competitors(db, user, args.get('product_id'))
        elif op == 'knowledge':
            result = knowledge.search(user, args.get('query'), args.get('scope', 'public'))
        else:
            result = {'status': 'need_input', 'question': str(args.get('question', '请补充你想查询的商品、订单或时间范围。'))[:500]}
        if is_write and (db.new or db.dirty or db.deleted):
            db.commit()
        release_tracker()
        release_guard()
        response = {'ok': True, 'data': result, 'meta': {'request_id': context.request_id,
                    'as_of': now().isoformat(), 'workspace': context.workspace}}
        if record:
            record.state, record.result, record.updated_at = 'succeeded', jsonable_encoder(response), now()
        details = {'status': 'ok', 'workspace': context.workspace}
        if op in EFFECT_LABELS:
            details['effect'] = EFFECT_LABELS[op]
            if isinstance(result, dict) and isinstance(result.get('id'), str):
                details['resource_id'] = result['id']
        b.audit(db, user, f'tool.{op}', details, context.request_id)
        if 'effect' in details:
            # The business handler may have committed just before stream teardown.
            # Update receipts even if that run has already become interrupted.
            record = db.scalar(select(ChatRun).where(ChatRun.id == context.request_id,
                ChatRun.user_id == user.id).with_for_update())
            if record:
                record.effects = execution_effects(db, context.request_id, user.id)
        db.commit()
        return response
    except Exception as exc:
        release_tracker()
        release_guard()
        db.rollback()
        recovery.fail_tool(record_id)
        if isinstance(exc, (ValidationError, ValueError, TypeError)):
            raise HTTPException(422, '工具参数不符合接口契约，请重新提取或向用户澄清') from exc
        raise
    finally:
        release_tracker()
        release_guard()


def chat_scope(stmt, user):
    stmt = stmt.where(ChatRun.shop_id == user.shop_id)
    if any(sid in user.shop_infos for sid in scope_shop_ids(user)):
        return stmt.where(ChatRun.scope_fingerprint == scope_fingerprint(user))
    return stmt.where((ChatRun.scope_fingerprint == scope_fingerprint(user)) | ChatRun.scope_fingerprint.is_(None))


def accessible_conversation(db, user, conversation_id, workspace='general'):
    conv = db.get(Conversation, conversation_id)
    if not conv or conv.user_id != user.id:
        b.fail(404, '会话不存在或不可访问')
    token = db.get(AccessToken, digest(conv.context_ref))
    if conv.workspace != workspace or (token and token.workspace != workspace):
        b.fail(409, '会话所属栏目不同，请使用当前栏目的会话')
    if not token or token.user_id != user.id or token.shop_id != user.shop_id:
        b.fail(409, '会话店铺范围已变化，请在当前范围开启新会话')
    if (token.scope_fingerprint and token.scope_fingerprint != scope_fingerprint(user)) or (
        not token.scope_fingerprint and any(sid in user.shop_infos for sid in scope_shop_ids(user))):
        b.fail(409, '会话数据来源范围已变化，请开启新会话')
    return conv, token


@app.get('/api/chat/runs')
def chat_runs(conversation_id: str = '', run_id: str = '', offset: int = Query(0, ge=0),
              workspace: Workspace = 'general',
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    recovery.recover_stale(db, user.id)
    db.commit()
    stmt = chat_scope(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.workspace == workspace), user)
    if conversation_id:
        conv = db.get(Conversation, conversation_id)
        if not conv or conv.user_id != user.id:
            b.fail(404, '会话不存在或不可访问')
        stmt = stmt.where(ChatRun.conversation_id == conversation_id)
    elif run_id:
        stmt = stmt.where(ChatRun.id == run_id)
    rows = list(db.scalars(stmt.order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).offset(offset).limit(21)))
    return {'items': [{**{k: getattr(r, k) for k in ['id', 'shop_id', 'workspace', 'query', 'answer', 'status', 'error', 'created_at', 'conversation_id', 'session_id', 'sources', 'effects', 'memory_excluded']},
                       'recovery': recovery.describe(db, r)}
                      for r in rows[:20]], 'has_more': len(rows) > 20}


@app.get('/api/chat/conversations')
def chat_conversations(offset: int = Query(0, ge=0), workspace: Workspace = 'general',
                       user: User = Depends(current_user), db: Session = Depends(get_db)):
    key = func.coalesce(ChatRun.conversation_id, ChatRun.id)
    ranked = chat_scope(select(ChatRun.id.label('id'),
                    func.row_number().over(partition_by=key, order_by=[ChatRun.created_at.desc(), ChatRun.id.desc()]).label('rn'))\
        .where(ChatRun.user_id == user.id, ChatRun.workspace == workspace), user).subquery()
    rows = list(db.scalars(select(ChatRun).join(ranked, ranked.c.id == ChatRun.id).where(ranked.c.rn == 1)
                           .order_by(ChatRun.created_at.desc(), ChatRun.id.desc()).offset(offset).limit(21)))
    return {'items': [{'id': r.conversation_id or r.id, 'conversation_id': r.conversation_id, 'run_id': r.id,
                       'workspace': r.workspace, 'title': r.query[:60], 'created_at': r.created_at, 'status': r.status} for r in rows[:20]],
            'has_more': len(rows) > 20}


def sse(event, data):
    return f'event: {event}\ndata: {json.dumps(jsonable_encoder(data), ensure_ascii=False)}\n\n'


def replay_chat(db, record):
    """Return an authorized local result without invoking Dify or business tools."""
    info = recovery.describe(db, record)
    effects = execution_effects(db, record.id, record.user_id)
    async def stream():
        yield sse('start', {'run_id': record.id, 'replayed': True})
        if record.session_id:
            yield sse('session', {'id': record.session_id})
        if record.conversation_id:
            yield sse('conversation', {'id': record.conversation_id})
        if record.answer:
            yield sse('delta', {'text': record.answer})
        yield sse('sources', record.sources or [])
        yield sse('effects', effects)
        yield sse('recovery', info)
        if record.status in {'failed', 'interrupted'}:
            yield sse('failure', {'message': record.error or info['message']})
        yield sse('done', {'run_id': record.id, 'status': record.status})
    return StreamingResponse(stream(), media_type='text/event-stream',
        headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


def dify_failure(detail):
    """Translate upstream failures without exposing plugin traces or credentials."""
    text = str(detail or '').lower()
    if is_context_limit(text):
        return str(ContextLimitError())
    if any(word in text for word in ['timeout', 'timed out', 'connection refused', 'connection error', 'connecterror', 'connectionerror', 'proxyerror', 'name resolution']):
        return '模型服务连接超时或暂时不可达，请稍后重试这条消息。'
    if any(word in text for word in ['429', 'rate limit', 'rate_limit', 'too many requests']):
        return '模型服务繁忙，请稍后重试这条消息。'
    if any(word in text for word in ['401', 'invalid api key', 'authentication', 'unauthorized']):
        return '模型服务认证失败，请检查 Dify 中的模型配置。'
    return '运营助手执行失败，本次回复未完成。请重试这条消息，或查看运行记录。'


@app.post('/api/chat')
def chat(body: Chat, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    recovery.recover_stale(db, user.id)
    if body.client_request_id:
        previous = db.scalar(select(ChatRun).where(ChatRun.user_id == user.id,
            ChatRun.client_request_id == body.client_request_id))
        if previous:
            visible = db.scalar(chat_scope(select(ChatRun).where(ChatRun.id == previous.id,
                ChatRun.user_id == user.id, ChatRun.workspace == body.workspace), user))
            if not visible or previous.query != body.query or previous.memory_excluded != body.temporary:
                b.fail(409, '请求标识所属内容或范围不同，请重新发送')
            db.commit()
            return replay_chat(db, previous)
    if body.retry_of:
        previous = db.scalar(chat_scope(select(ChatRun).where(ChatRun.id == body.retry_of,
            ChatRun.user_id == user.id, ChatRun.workspace == body.workspace), user))
        if not previous:
            b.fail(404, '原请求不存在或不在当前范围')
        if (previous.query != body.query or previous.memory_excluded != body.temporary or
                (body.session_id and previous.session_id != body.session_id)):
            b.fail(409, '重试内容或会话与原请求不同，请重新提问')
        # Follow earlier retries too: clicking the original failed message again
        # must not replay a write that succeeded in one of its descendants.
        while True:
            child = db.scalar(chat_scope(select(ChatRun).where(ChatRun.retry_of == previous.id,
                ChatRun.user_id == user.id, ChatRun.workspace == body.workspace), user))
            if child is None:
                break
            previous = child
        if not recovery.describe(db, previous)['can_retry']:
            db.commit()
            return replay_chat(db, previous)
        if not body.session_id and previous.session_id:
            body = body.model_copy(update={'session_id': previous.session_id})
        body = body.model_copy(update={'retry_of': previous.id})
    key = settings.dify_operator_key
    if not key:
        raise HTTPException(503, '运营 Agent 尚未绑定 Dify 应用 API Key。请完成 Dify 配置；基础业务操作仍可使用。')
    if db.scalar(select(ChatRun).where(ChatRun.user_id == user.id, ChatRun.status == 'running',
                                       ChatRun.created_at > now() - timedelta(seconds=130 + (
                                           settings.memory_reactive_timeout if settings.memory_reactive_enabled else 0)))):
        b.fail(409, '上一条消息仍在处理中')
    if body.conversation_id:
        conv, token = accessible_conversation(db, user, body.conversation_id, body.workspace)
    local_session = memory_context.session_for(db, user, body)
    run = ChatRun(user_id=user.id, shop_id=user.shop_id, scope_fingerprint=scope_fingerprint(user),
                  workspace=body.workspace, query=body.query, conversation_id=local_session.upstream_id,
                  session_id=local_session.id, turn_seq=local_session.next_seq, memory_excluded=body.temporary,
                  client_request_id=body.client_request_id or None, retry_of=body.retry_of or None, updated_at=now())
    local_session.next_seq += 1
    db.add(run)
    db.flush()
    # Persist the running marker before a possible no-tool compaction model request.
    db.commit()
    try:
        snapshot = memory_context.build(db, user, local_session, run, body.temporary)
    except Exception as exc:
        db.rollback()
        record = db.get(ChatRun, run.id)
        record.status = 'failed'
        record.error = str(exc.detail) if isinstance(exc, HTTPException) else '上下文准备失败，请重试'
        db.commit()
        raise
    snapshot_refs, snapshot_status = snapshot.references, snapshot.timings.get('memory_status')
    upstream_id, local_session_id = local_session.upstream_id or '', local_session.id
    context_ref = issue_token(db, user, 'tool', run.id, workspace=body.workspace)
    memory_context.bind_token(db, run.id, context_ref)
    run_id, user_id = run.id, user.id

    async def stream():
        answer, status, error, sources = '', 'failed', None, []
        last_checkpoint = 0.0
        seen_conversation = ''
        try:
            yield sse('start', {'run_id': run_id})
            yield sse('session', {'id': local_session_id})
            yield sse('memory', {'references': snapshot_refs, 'status': snapshot_status})
            async with asyncio.timeout(120), httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5)) as client:
                active_ref, request_conversation, retried, tool_event_seen = context_ref, upstream_id, False, False
                while True:
                    try:
                        async with client.stream('POST', settings.dify_base_url.rstrip('/') + '/chat-messages',
                                                 headers={'Authorization': f'Bearer {key}'},
                                                 json={'query': body.query, 'inputs': {'context_ref': active_ref,
                                                       'workspace': body.workspace, 'workspace_name': WORKSPACES[body.workspace]},
                                                        'user': user_id, 'conversation_id': request_conversation,
                                                       'response_mode': 'streaming'}) as response:
                            if response.status_code != 200:
                                if response.status_code in (400, 413, 422):
                                    raw_error = (await response.aread())[:16000].decode('utf-8', errors='replace')
                                    if is_context_limit(raw_error):
                                        raise ContextLimitError()
                                raise RuntimeError(f'Dify 返回 HTTP {response.status_code}，请检查应用发布状态和配置')
                            async for line in response.aiter_lines():
                                if not line.startswith('data:'):
                                    continue
                                item = json.loads(line[5:].strip())
                                with SessionLocal() as guard:
                                    memory_context.validate(guard, run_id)
                                event = item.get('event')
                                if event in ('tool_started','tool_finished','tool_message') or (
                                        event == 'agent_thought' and item.get('tool')):
                                    tool_event_seen = True
                                if item.get('conversation_id') and item['conversation_id'] != seen_conversation:
                                    conv_id = item['conversation_id']
                                    with SessionLocal() as store:
                                        existing = store.get(Conversation, conv_id)
                                        if existing and (existing.user_id != user_id or existing.workspace != body.workspace
                                                         or existing.session_id not in (None, local_session_id)):
                                            raise RuntimeError('会话归属校验失败')
                                        if not existing:
                                            store.add(Conversation(id=conv_id, user_id=user_id, context_ref=active_ref,
                                                workspace=body.workspace, session_id=local_session_id))
                                        else:
                                            existing.context_ref = active_ref
                                            existing.session_id = local_session_id
                                        store.get(ChatSession, local_session_id).upstream_id = conv_id
                                        store.get(ChatRun, run_id).conversation_id = conv_id
                                        store.commit()
                                    seen_conversation = conv_id
                                    yield sse('conversation', {'id': conv_id})
                                if event in ['message', 'agent_message']:
                                    fragment = item.get('answer', '')
                                    answer += fragment
                                    yield sse('delta', {'text': fragment})
                                elif event == 'message_replace':
                                    answer = item.get('answer', '')
                                    yield sse('replace', {'text': answer})
                                elif event == 'message_end':
                                    status = 'succeeded'
                                    resources = item.get('metadata', {}).get('retriever_resources', [])
                                    sources = [{'document_id': x.get('document_id'),
                                                          'title': x.get('document_name'), 'segment_id': x.get('segment_id')}
                                                         for x in resources]
                                    yield sse('sources', sources)
                                elif event == 'workflow_finished' and item.get('data', {}).get('status') in ['failed', 'stopped', 'partial-succeeded']:
                                    if is_context_limit(item['data'].get('error')):
                                        raise ContextLimitError()
                                    raise RuntimeError(dify_failure(item['data'].get('error')))
                                elif event == 'error':
                                    if is_context_limit(item.get('code')) or is_context_limit(item.get('message') or item.get('error')):
                                        raise ContextLimitError()
                                    raise RuntimeError(dify_failure(item.get('message') or item.get('error')))
                                if time.monotonic() - last_checkpoint >= .75:
                                    await asyncio.to_thread(recovery.checkpoint, run_id, answer, sources)
                                    last_checkpoint = time.monotonic()
                            if status != 'succeeded':
                                raise RuntimeError('回复连接中断，尚未收到完整结果，请重试这条消息。')
                            if not answer.strip():
                                raise RuntimeError('模型未返回可显示的回复，请重试这条消息。')
                        break
                    except ContextLimitError:
                        if retried or answer or status == 'succeeded' or tool_event_seen or not settings.memory_reactive_enabled:
                            raise
                        active_ref = await asyncio.to_thread(memory_context.retry_context, run_id, active_ref)
                        retried, request_conversation, seen_conversation = True, '', ''
                        yield sse('conversation', {'id': ''})
                        yield sse('memory', {'references': [], 'status': snapshot_status})
                        yield sse('context', {'action': 'reactive_retry', 'attempt': 1})
        except (asyncio.CancelledError, GeneratorExit):
            status, error = 'interrupted', '连接中断；请从对话历史和运行记录中查看状态'
            raise
        except Exception as exc:
            status = 'failed'
            error = str(exc.detail) if isinstance(exc, HTTPException) else str(exc) if isinstance(exc, RuntimeError) else dify_failure(f'{type(exc).__name__}: {exc}')
            yield sse('failure', {'message': error})
        finally:
            with SessionLocal() as store:
                record = store.scalar(select(ChatRun).where(ChatRun.id == run_id).with_for_update())
                if record.status == 'running':
                    record.answer, record.status, record.error = answer, status, error
                    record.sources, record.updated_at = sources, now()
                else:
                    status, error = record.status, record.error
                record.effects = execution_effects(store, run_id, user_id)
                effects = record.effects
                if settings.memory_enabled and not record.memory_excluded:
                    memory_service.enqueue(store, user_id, 'extract', run_id, memory_preferences(store, user_id).epoch)
                    memory_service.enqueue(store, user_id, 'compress', run_id, memory_preferences(store, user_id).epoch)
                store.commit()
                recovery_info = recovery.describe(store, record)
        yield sse('effects', effects)
        yield sse('recovery', recovery_info)
        yield sse('done', {'run_id': run_id, 'status': status})

    return StreamingResponse(stream(), media_type='text/event-stream',
                             headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache'})


dist = ROOT / 'frontend/dist'
if dist.exists():
    app.mount('/assets', StaticFiles(directory=dist / 'assets'), name='assets')


@app.get('/', include_in_schema=False)
def index():
    if (dist / 'index.html').exists():
        return FileResponse(dist / 'index.html')
    return JSONResponse({'message': '前端尚未构建，请按 README 执行 npm run build。', 'api_docs': '/docs'})
