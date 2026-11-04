"""Single local durable worker: scheduled SOP and claimed media jobs."""
import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select

from .auth import scoped_actor
from .db import SessionLocal
from .models import OpsSchedule, OpsJob, OpsAsset, User, now
from .ops_data import ZONE
from . import ops_media, ops_work

logger = logging.getLogger('commerce.ops')


def recover():
    # The local deployment runs a single API/worker process.
    with SessionLocal() as db:
        for row in db.scalars(select(OpsJob).where(OpsJob.status == 'running')):
            row.status = 'queued'
            row.error = '服务重启，正在恢复剪辑任务'
        db.commit()


def tick():
    current = now().astimezone(ZONE)
    with SessionLocal() as db:
        schedules = list(db.scalars(select(OpsSchedule).where(OpsSchedule.enabled.is_(True)).with_for_update(skip_locked=True)))
        for row in schedules:
            if row.last_date == current.date() or current.hour < row.hour:
                continue
            shop_id = row.shop_id
            try:
                actor = scoped_actor(db, db.get(User, row.user_id), shop_id)
                row.last_date, row.error = current.date(), None
                ops_work.generate(db, actor, current.date())
            except Exception as exc:
                db.rollback()
                stored = db.get(OpsSchedule, shop_id)
                stored.error = exc.detail if isinstance(exc, HTTPException) else '每日任务生成失败，请检查数据后重新开启计划'
                stored.last_date = current.date()
                db.commit()
        db.commit()
        job = db.scalar(select(OpsJob).where(OpsJob.status == 'queued').order_by(OpsJob.created_at)
                        .with_for_update(skip_locked=True).limit(1))
        if not job:
            return False
        job_id = job.id
        job.status, job.started_at = 'running', now()
        job.attempts += 1
        db.commit()
        try:
            scoped_actor(db, db.get(User, job.user_id), job.shop_id)
            assets = {a.id: a for a in db.scalars(select(OpsAsset).where(OpsAsset.shop_id == job.shop_id))}
            result = ops_media.render(job, assets)
            job.result, job.status, job.error = result, 'succeeded', None
        except Exception as exc:
            logger.exception('Render job failed: %s', job_id)
            job.status = 'failed'
            job.error = str(exc)[:300] if isinstance(exc, ValueError) else '剪辑执行失败，请检查素材、编码及FFmpeg后重试'
        job.finished_at = now()
        db.commit()
        return True


async def run():
    await asyncio.to_thread(recover)
    while True:
        try:
            worked = await asyncio.to_thread(tick)
        except Exception:
            logger.exception('Operations worker tick failed')
            worked = False
        await asyncio.sleep(.2 if worked else 3)
