import hashlib
import hmac
import secrets
from datetime import timedelta
from dataclasses import dataclass, field
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import AccessToken, Membership, User, PlatformShop, now
from .catalog import SHOPS

bearer = HTTPBearer(auto_error=False)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f'{salt}:{value}'


def password_matches(password: str, stored: str) -> bool:
    return hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored)


def issue_token(db: Session, user: User, kind='session', request_id=None, workspace='general'):
    token = secrets.token_urlsafe(32)
    lifetime = timedelta(hours=8) if kind == 'session' else timedelta(minutes=10)
    db.add(AccessToken(digest=digest(token), user_id=user.id, kind=kind,
                       request_id=request_id, workspace=workspace, shop_id=user.shop_id if kind == 'tool' else None,
                       scope_fingerprint=scope_fingerprint(user) if kind == 'tool' and hasattr(user, 'allowed_shop_ids') else None,
                       expires_at=now() + lifetime))
    db.commit()
    return token


def resolve_token(db: Session, token: str, kind: str) -> tuple[User, AccessToken]:
    record = db.get(AccessToken, digest(token))
    if not record or record.kind != kind or record.expires_at <= now():
        raise HTTPException(401, '登录或请求上下文已失效')
    user = db.get(User, record.user_id)
    if not user:
        raise HTTPException(401, '登录已失效')
    require_role(user, 'operator')
    return user, record


@dataclass(frozen=True)
class Actor:
    id: str
    username: str
    role: str
    display_name: str
    shop_id: str | None
    allowed_shop_ids: tuple[str, ...]
    shop_infos: dict = field(default_factory=dict)


def scoped_actor(db, user, shop_id=None):
    require_role(user, 'operator')
    allowed = tuple(db.scalars(
        select(Membership.shop_id).where(Membership.user_id == user.id).order_by(Membership.shop_id)))
    if shop_id is not None and shop_id not in allowed:
        raise HTTPException(403, '无权访问此店铺')
    name = user.display_name.split(' · ')[0]
    infos = {row.id: {'name': row.name, 'tagline': '平台接入店铺', 'positioning': '真实店铺' if row.mode == 'live' else '接口演练',
                     'sizes': [], 'mode': row.mode} for row in db.scalars(select(PlatformShop).where(PlatformShop.id.in_(allowed)))}
    return Actor(user.id, user.username, user.role, name, shop_id, allowed, infos)


def scope_shop_ids(user):
    # ORM users are accepted by internal scripts; requests always use an Actor.
    if user.shop_id:
        return (user.shop_id,)
    live = tuple(sid for sid, info in getattr(user, 'shop_infos', {}).items() if info.get('mode') == 'live')
    return live or user.allowed_shop_ids


def shop_info(user, sid):
    return getattr(user, 'shop_infos', {}).get(sid) or SHOPS.get(sid) or {
        'name': sid, 'tagline': '授权店铺', 'positioning': '运营店铺', 'sizes': []}


def scope_fingerprint(user):
    return digest('|'.join(sorted(sid + ':' + shop_info(user, sid).get('mode', 'demo') for sid in scope_shop_ids(user))))


def scope_environments(user):
    return {sid: shop_info(user, sid).get('mode', 'demo') for sid in scope_shop_ids(user)}


def shop_view(user):
    if user.shop_id:
        return shop_info(user, user.shop_id)
    return {'name': '全部正式店铺' if any(i.get('mode') == 'live' for i in getattr(user, 'shop_infos', {}).values()) else '全部授权店铺',
            'tagline': '统一管理每一家授权店铺。', 'positioning': '男装与女装 · 多店运营',
            'sizes': list(dict.fromkeys(s for sid in scope_shop_ids(user) for s in shop_info(user, sid)['sizes']))}


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
                 x_shop_id: str | None = Header(None), db: Session = Depends(get_db)) -> Actor:
    if not credentials:
        raise HTTPException(401, '请先登录')
    user = resolve_token(db, credentials.credentials, 'session')[0]
    return scoped_actor(db, user, x_shop_id)


def require_role(user: User, *roles):
    if user.role not in roles:
        raise HTTPException(403, '当前角色无权执行此操作')
