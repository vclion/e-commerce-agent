"""Deployment-admin CLI: explicit shared-space grants, never model-controlled."""
import argparse,json
from commerce.db import SessionLocal,initialize_schema
from commerce.models import User
from commerce.auth import scoped_actor,digest
from commerce.memory.models import MemorySpace,MemoryGrant
from commerce.memory.policy import environments
from sqlalchemy import select


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--owner',required=True);p.add_argument('--user',required=True);p.add_argument('--shop',required=True)
    p.add_argument('--publish',action='store_true');p.add_argument('--manage',action='store_true');p.add_argument('--revoke',action='store_true')
    a=p.parse_args();initialize_schema()
    with SessionLocal() as db:
        owner=scoped_actor(db,db.get(User,a.owner),a.shop)
        scoped_actor(db,db.get(User,a.user),a.shop)
        shops=environments(owner,[a.shop]);key=digest(json.dumps(['shop_shared',shops],sort_keys=True))
        space=db.scalar(select(MemorySpace).where(MemorySpace.key==key))
        if not space:
            if a.revoke:return
            space=MemorySpace(key=key,owner_id=a.owner,kind='shop_shared',shop_id=a.shop,required_shops=shops)
            db.add(space);db.flush()
        if space.owner_id!=a.owner:raise SystemExit('Owner mismatch')
        grant=db.get(MemoryGrant,(space.id,a.user))
        if a.revoke:
            if grant:db.delete(grant)
        else:db.merge(MemoryGrant(space_id=space.id,user_id=a.user,can_publish=a.publish,can_manage=a.manage))
        space.epoch+=1
        from commerce.memory.service import invalidate
        invalidate(db,a.user);db.commit()
    print('Shared memory grant updated.')


if __name__=='__main__':main()
