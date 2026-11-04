"""Daily SOP, accountable task changes and reports."""
from collections import Counter
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .auth import scope_shop_ids
from .business import fail, audit
from .models import OpsTask, now, uid
from .ops_data import ZONE, records, overview

STATES = {'pending', 'in_progress', 'blocked', 'review', 'completed', 'cancelled'}


def task_view(row):
    return {**{k: getattr(row, k) for k in ['id', 'shop_id', 'template_id', 'title', 'owner', 'due_at',
            'priority', 'status', 'evidence', 'note', 'version', 'history', 'context', 'updated_at']}, 'date': row.work_date.isoformat()}


def get_task(db, user, task_id, lock=False):
    stmt = select(OpsTask).where(OpsTask.id == task_id, OpsTask.shop_id.in_(scope_shop_ids(user)))
    row = db.scalar(stmt.with_for_update() if lock else stmt)
    if not row:
        fail(404, '任务不存在或不可访问')
    return row


def tasks(db, user, day=None):
    stmt = select(OpsTask).where(OpsTask.shop_id.in_(scope_shop_ids(user)))
    if day:
        stmt = stmt.where(OpsTask.work_date == day)
    rows = list(db.scalars(stmt.order_by(OpsTask.work_date.desc(), OpsTask.due_at, OpsTask.id).limit(300)))
    return {'items': [task_view(r) for r in rows], 'counts': dict(Counter(r.status for r in rows))}


def generate(db, user, day=None):
    day = day or now().astimezone(ZONE).date()
    if abs((day - now().astimezone(ZONE).date()).days) > 90:
        fail(422, 'SOP日期需在今天前后90天内')
    templates = records(db, user, 'sop_templates')
    if not templates:
        fail(409, '请先导入SOP模板或加载测试数据')
    snapshot = overview(db, user, 7)
    created = 0
    for template in templates:
        sid = template['shop_id']
        fact = next((r for r in snapshot['by_shop'] if r['shop_id'] == sid), {})
        note = '需要核实的数据：' + ('、'.join(fact.get('missing_dates', [])) or '最近周期数据已覆盖')
        context = {'as_of': snapshot['as_of'], 'baseline': fact, 'source': snapshot['source'],
                   'low_stock': [r for r in snapshot['low_stock'] if r['shop_id'] == sid],
                   'overdue_shipments': [r for r in snapshot['overdue_shipments'] if r['shop_id'] == sid]}
        task_id = uid()
        statement = insert(OpsTask).values(id=task_id, shop_id=sid, work_date=day,
            template_id=template['template_id'], title=template['title'], owner='待分配',
            due_at=datetime.combine(day, time(int(template['due_hour'])), ZONE), status='pending',
            priority='high' if not fact.get('complete') else 'normal', evidence='', note=note,
            context=context, history=[{'at': now().isoformat(), 'by': user.id, 'action': 'generate'}], version=1, updated_at=now())
        inserted = db.scalar(statement.on_conflict_do_nothing(index_elements=['shop_id', 'work_date', 'template_id']).returning(OpsTask.id))
        created += bool(inserted)
    audit(db, user, 'ops.sop.generate', {'date': day.isoformat(), 'created': created})
    db.commit()
    return {'created': created, 'date': day.isoformat(), **tasks(db, user, day)}


def change_task(db, user, task_id, body):
    row = get_task(db, user, task_id, True)
    if row.version != body['expected_version']:
        fail(409, '任务已被更新，请刷新后重试')
    status = body.get('status', row.status)
    evidence = body.get('evidence', row.evidence)
    note = body.get('note', row.note)
    if status not in STATES:
        fail(422, '任务状态不合法')
    if status == 'completed':
        if not evidence.strip():
            fail(422, '完成任务需要填写实际完成证据')
    if status == 'blocked' and not note.strip():
        fail(422, '阻塞任务需要填写原因')
    previous = row.status
    for key in ['owner', 'evidence', 'note']:
        if key in body:
            setattr(row, key, body[key])
    row.status = status
    row.version += 1
    row.updated_at = now()
    row.history = [*row.history, {'at': now().isoformat(), 'by': user.id, 'from': previous, 'to': status,
                                 'note': note, 'evidence': evidence}]
    audit(db, user, 'ops.task.update', {'id': row.id, 'from': previous, 'to': status})
    db.commit()
    return task_view(row)


def daily_report(db, user, day=None):
    day = day or now().astimezone(ZONE).date()
    rows = tasks(db, user, day)['items']
    overdue = [task_view(t) for t in db.scalars(select(OpsTask).where(
        OpsTask.shop_id.in_(scope_shop_ids(user)), OpsTask.work_date < day,
        OpsTask.status.not_in(['completed', 'cancelled'])).order_by(OpsTask.due_at).limit(100))]
    completed = [r for r in rows if r['status'] == 'completed']
    return {'date': day.isoformat(), 'counts': dict(Counter(r['status'] for r in rows)),
            'completed': completed, 'completion_rate': len(completed) / len(rows) if rows else None,
            'unfinished': [r for r in rows if r['status'] not in ['completed', 'cancelled']],
            'overdue': overdue,
            'next_steps': [dict(shop_id=r['shop_id'], title=r['title'], owner=r['owner']) for r in [*overdue, *rows] if r['status'] in ['blocked', 'pending', 'in_progress', 'review']][:30],
            'note': '任务完成以实际回填证据为准；生成日报不会关闭任务。'}


