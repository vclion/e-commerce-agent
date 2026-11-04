"""Operator-facing setup wizard; no platform password is collected."""
import hashlib
import secrets
from datetime import timedelta
from typing import Literal
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import select

from .auth import current_user, scoped_actor
from .business import fail, audit
from .db import get_db
from .models import PlatformConnection, PlatformShop, PlatformSync, OpsRecord, User, now
from .config import settings
from .schemas import Strict
from . import platform_service as service
from .platform_vault import decrypt

router = APIRouter(prefix='/api/platform', tags=['官方平台接入'])


class Configuration(Strict):
    name: str = Field(min_length=1, max_length=80)
    provider: Literal['doudian', 'douyin'] = 'doudian'
    mode: Literal['live', 'simulation'] = 'live'
    shop_id: str | None = Field(default=None, max_length=20)
    external_id: str = Field(default='', max_length=120)
    app_key: str | None = Field(default=None, max_length=120)
    app_secret: SecretStr | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None
    token_expires_in: int = Field(default=3600, ge=60, le=31536000)
    redirect_uri: str = Field(default='', max_length=500)
    scopes: str = Field(default='video.list,data.external.item', max_length=500)
    interval_minutes: int = Field(default=60, ge=15, le=1440)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator('redirect_uri')
    @classmethod
    def valid_redirect(cls, value):
        if value:
            url = urlparse(value)
            if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError('授权回调需为无查询参数的HTTPS地址')
            if url.path != '/api/platform/oauth/callback':
                raise ValueError('回调路径需为/api/platform/oauth/callback')
        return value


class SyncRequest(Strict):
    days: int = Field(default=7, ge=1, le=90)
    resources: list[str] | None = Field(default=None, max_length=5)


class ScheduleRequest(Strict):
    enabled: bool
    interval_minutes: int = Field(default=60, ge=15, le=1440)


class ProductFacts(Strict):
    cost_fen: int | None = Field(default=None, ge=0, le=100000000)
    size: str | None = Field(default=None, max_length=30)
    color: str | None = Field(default=None, max_length=60)
    material: str | None = Field(default=None, max_length=500)
    fit: str | None = Field(default=None, max_length=60)
    season: str | None = Field(default=None, max_length=60)
    occasion: str | None = Field(default=None, max_length=100)
    evidence: str = Field(min_length=3, max_length=2000)


@router.get('/connections')
def connections(user=Depends(current_user), db=Depends(get_db)):
    ids = (user.shop_id,) if user.shop_id else user.allowed_shop_ids
    rows = list(db.scalars(select(PlatformConnection).where(PlatformConnection.user_id == user.id,
        PlatformConnection.shop_id.in_(ids)).order_by(PlatformConnection.created_at.desc())))
    return {'items': [{**service.view(row), 'shop_name': db.get(PlatformShop, row.shop_id).name} for row in rows],
        'worker_enabled': settings.platform_worker_enabled,
        'note': '商家自研：先在抖店开放平台完成店铺授权，再填写App Key、Secret与店铺ID。抖音视频数据需另外授权。',
        'docs': {'doudian': 'https://op.jinritemai.com/docs', 'douyin': 'https://developer.open-douyin.com/'}}


@router.post('/connections')
def create(body: Configuration, user=Depends(current_user), db=Depends(get_db)):
    validate_identity(body)
    return service.save(db, user, body)


def validate_identity(body):
    if body.mode == 'live' and body.provider == 'doudian' and body.external_id and not (body.external_id.isascii() and body.external_id.isdigit()):
        fail(422, '正式抖店ID需为平台提供的数字ID')


@router.patch('/connections/{identifier}')
def update(identifier: str, body: Configuration, user=Depends(current_user), db=Depends(get_db)):
    validate_identity(body)
    return service.save(db, user, body, identifier)


@router.post('/connections/{identifier}/connect')
def connect(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    return service.connect(db, user, service.connection(db, user, identifier, True))


@router.post('/connections/{identifier}/sync')
def sync(identifier: str, body: SyncRequest, user=Depends(current_user), db=Depends(get_db)):
    return service.enqueue(db, user, service.connection(db, user, identifier, True), body.days, body.resources)


@router.put('/connections/{identifier}/schedule')
def schedule(identifier: str, body: ScheduleRequest, user=Depends(current_user), db=Depends(get_db)):
    row = service.connection(db, user, identifier, True)
    if body.enabled and row.status != 'connected':
        fail(409, '连接检查成功后才能开启自动同步')
    row.enabled, row.interval_minutes = body.enabled, body.interval_minutes
    row.next_sync_at = now() if body.enabled else None
    db.commit()
    return service.view(row)


@router.post('/connections/{identifier}/disconnect')
def disconnect(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    row = service.connection(db, user, identifier, True)
    if db.scalar(select(PlatformSync.id).where(PlatformSync.connection_id == row.id, PlatformSync.status == 'running')):
        fail(409, '同步正在运行，请结束后再断开')
    for job in db.scalars(select(PlatformSync).where(PlatformSync.connection_id == row.id,
                                                    PlatformSync.status.in_(['queued', 'retry_wait']))):
        job.status, job.finished_at = 'cancelled', now()
    from .platform_vault import encrypt
    credentials = decrypt(row.credentials)
    row.credentials = encrypt({'app_secret': credentials['app_secret']}) if credentials.get('app_secret') else ''
    row.status, row.enabled, row.expires_at, row.next_sync_at = 'disconnected', False, None, None
    row.options = {k: v for k, v in row.options.items() if not k.startswith('oauth_')}
    audit(db, user, 'platform.disconnect', {'id': row.id})
    db.commit()
    return service.view(row)


@router.get('/connections/{identifier}/syncs')
def syncs(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    service.connection(db, user, identifier)
    return {'items': [service.sync_view(row) for row in db.scalars(select(PlatformSync)
        .where(PlatformSync.connection_id == identifier).order_by(PlatformSync.created_at.desc()).limit(30))]}


@router.post('/syncs/{identifier}/retry')
def retry(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    job = db.get(PlatformSync, identifier, with_for_update=True)
    if not job:
        fail(404, '同步任务不存在')
    row = service.connection(db, user, job.connection_id, True)
    if job.status not in ['failed', 'partial']:
        fail(409, '仅失败或部分成功的任务可重试')
    resources = [r for r in job.resources if job.result.get(r, {}).get('status') != 'succeeded']
    return service.enqueue(db, user, row, job.days, resources)


@router.post('/syncs/{identifier}/cancel')
def cancel(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    job = db.get(PlatformSync, identifier, with_for_update=True)
    if not job:
        fail(404, '同步任务不存在')
    service.connection(db, user, job.connection_id, True)
    if job.status not in ['queued', 'retry_wait']:
        fail(409, '只能取消尚未执行的同步任务')
    job.status, job.finished_at = 'cancelled', now()
    db.commit()
    return service.sync_view(job)


@router.post('/connections/{identifier}/authorize')
def authorize(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    row = service.connection(db, user, identifier, True)
    if row.provider != 'douyin' or row.mode != 'live':
        fail(422, '此入口仅用于正式抖音账号OAuth授权；抖店自研请在平台授权店铺后检查连接')
    redirect = row.options.get('redirect_uri')
    if not redirect or not row.app_key or not decrypt(row.credentials).get('app_secret'):
        fail(422, '请先保存Client Key、Secret和HTTPS回调地址')
    state = secrets.token_urlsafe(32)
    row.options = {**row.options, 'oauth_state': hashlib.sha256(state.encode()).hexdigest(),
                   'oauth_expires': (now() + timedelta(minutes=10)).isoformat()}
    db.commit()
    return {'url': 'https://open.douyin.com/platform/oauth/connect/?' + urlencode({
        'client_key': row.app_key, 'response_type': 'code', 'scope': row.options['scopes'],
        'redirect_uri': redirect, 'state': row.id + '.' + state}), 'expires_in': 600,
        'note': '需要当前应用实际获批的权限。若平台要求经营关系扫码授权，可在平台授权后填写对应API凭证。'}


@router.get('/connections/{identifier}/products')
def product_facts(identifier: str, user=Depends(current_user), db=Depends(get_db)):
    row = service.connection(db, user, identifier)
    return {'items': [record.payload for record in db.scalars(select(OpsRecord).where(
        OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'products').order_by(OpsRecord.external_id).limit(1000))]}


@router.patch('/connections/{identifier}/products/{product_id}')
def save_product_facts(identifier: str, product_id: str, body: ProductFacts, user=Depends(current_user), db=Depends(get_db)):
    row = service.connection(db, user, identifier, True)
    record = db.scalar(select(OpsRecord).where(OpsRecord.shop_id == row.shop_id, OpsRecord.kind == 'products',
                                             OpsRecord.external_id == product_id).with_for_update())
    if not record:
        fail(404, '同步商品不存在')
    fields = body.model_dump(exclude={'evidence'}, exclude_unset=True)
    fields = {k: (v or None) if isinstance(v, str) else v for k, v in fields.items()}
    facts = {**record.payload.get('operator_facts', {}), **fields}
    record.payload = {**record.payload, **fields, 'operator_facts': facts, 'operator_facts_note': body.evidence,
                      'operator_facts_updated_at': now().isoformat()}
    audit(db, user, 'platform.product.facts', {'connection_id': row.id, 'sku_id': product_id, 'fields': list(fields)})
    db.commit()
    return record.payload


@router.get('/oauth/callback', response_class=HTMLResponse)
def callback(state: str = Query(max_length=180), code: str = Query(default='', max_length=1000), db=Depends(get_db)):
    identifier, _, nonce = state.partition('.')
    row = db.scalar(select(PlatformConnection).where(PlatformConnection.id == identifier).with_for_update())
    if not row or row.provider != 'douyin' or row.mode != 'live' or not nonce:
        fail(400, '授权状态无效')
    expected = row.options.get('oauth_state', '')
    from datetime import datetime
    if not expected or not secrets.compare_digest(hashlib.sha256(nonce.encode()).hexdigest(), expected) or datetime.fromisoformat(row.options['oauth_expires']) <= now():
        fail(400, '授权状态失效，请重新发起授权')
    # Consume the nonce before remote token exchange, including denied or failed authorizations.
    row.options = {k: v for k, v in row.options.items() if not k.startswith('oauth_')}
    db.commit()
    actor = scoped_actor(db, db.get(User, row.user_id), row.shop_id)
    if not code:
        fail(400, '未获得授权码，请回到接入页面重新授权')
    service.connect(db, actor, row, code)
    return '<!doctype html><meta charset="utf-8"><title>授权完成</title><p>抖音账号授权完成，可关闭此页，回到运营工作台刷新连接。</p><a href="/">返回工作台</a>'
