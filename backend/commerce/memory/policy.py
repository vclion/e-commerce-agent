import json
from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from ..auth import digest, scope_shop_ids, shop_info, scope_fingerprint, scoped_actor
from ..models import User, ChatRun, Audit
from .models import MemorySpace, MemoryGrant, MemorySettings, MemoryEvidence


def preferences(db, user_id, lock=False):
    if lock:
        db.scalar(select(User).where(User.id == user_id).with_for_update())
    db.execute(insert(MemorySettings).values(user_id=user_id, read_enabled=True, auto_write=True,
        context_index_enabled=True, reflection_enabled=True, read_epoch=1, epoch=1).on_conflict_do_nothing())
    return db.get(MemorySettings, user_id, populate_existing=True)


def environments(actor, ids):
    return {sid: shop_info(actor, sid).get('mode', 'demo') for sid in ids}


def space_for(db, actor, scope, workspace, *, shared=False):
    if scope == 'personal_shop' and not actor.shop_id:
        raise HTTPException(422, '请选择一家店铺保存店铺约定；多店信息请保存到当前栏目')
    ids = [] if scope == 'personal' else list(scope_shop_ids(actor))
    shops = environments(actor, ids)
    column = workspace if scope == 'workspace' else None
    key = digest(json.dumps([actor.id, scope, shops, column], sort_keys=True))
    row = db.scalar(select(MemorySpace).where(MemorySpace.key == key))
    if not row:
        row = MemorySpace(key=key, owner_id=actor.id, kind=scope, shop_id=actor.shop_id if ids else None,
                          workspace=column, required_shops=shops)
        db.add(row); db.flush()
    return row


def allowed_spaces(db, actor, workspace=None, *, manage=False):
    grants = select(MemoryGrant.space_id).where(MemoryGrant.user_id == actor.id)
    if manage:
        grants = grants.where(MemoryGrant.can_manage.is_(True))
    stmt = select(MemorySpace).where(or_(MemorySpace.owner_id == actor.id, MemorySpace.id.in_(grants)))
    result = []
    ids = set(scope_shop_ids(actor))
    for row in db.scalars(stmt):
        if row.workspace and workspace is not None and row.workspace != workspace:
            continue
        if not set(row.required_shops).issubset(ids):
            continue
        if row.required_shops != environments(actor, row.required_shops):
            continue
        result.append(row)
    return result


def source_visible(db, actor, item, workspace=None):
    for evidence in db.scalars(select(MemoryEvidence).where(MemoryEvidence.item_id == item.id)):
        if evidence.source_type == 'audit':
            record=db.get(Audit,evidence.source_id)
            if not record or record.user_id!=actor.id:
                return False
            try:source_actor=scoped_actor(db,db.get(User,actor.id),record.details.get('shop_id'))
            except HTTPException:return False
            if record.details.get('data_scope')!=scope_fingerprint(source_actor):return False
        elif evidence.source_type == 'chat':
            run = db.get(ChatRun, evidence.source_id)
            if not run or run.user_id != actor.id or run.memory_excluded:
                return False
            # Approved semantic/decision projections can cross columns, never raw chat text.
            if item.kind in ('episodic', 'procedural') and workspace is not None and run.workspace != workspace:
                return False
            try:source_actor=scoped_actor(db,db.get(User,actor.id),run.shop_id)
            except HTTPException:return False
            if run.scope_fingerprint and run.scope_fingerprint != scope_fingerprint(source_actor):return False
    return True
