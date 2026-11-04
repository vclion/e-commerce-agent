"""Controlled fixture scope and independent acceptance expectations."""
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from sqlalchemy import delete, select, update

from commerce.auth import password_hash
from commerce.config import ROOT, settings
from commerce.db import SessionLocal
from commerce.models import (User, Membership, AccessToken, ChatRun, Conversation, Audit,
                             Draft, OpsImport, OpsScript, OpsJob, OpsSchedule)

ARTIFACTS = ROOT / 'tests/artifacts/acceptance-repair'
SHOTS = ARTIFACTS / 'screenshots'
FIXTURES = ROOT / 'data/douyin-ops-v1'
FINANCIAL_FIELDS = ('paid_fen', 'refund_fen', 'net_receipts_fen', 'paid_parent_orders', 'paid_sub_orders')


def fixture_rows(kind):
    return [json.loads(line) for line in (FIXTURES / f'{kind}.jsonl').read_text().splitlines() if line.strip()]


def fixture_context():
    truth = json.loads((FIXTURES / 'expected_results.json').read_text())
    return tuple(row['shop_id'] for row in truth['by_shop']), truth['period']['end_exclusive']


def expected_financials(shop_ids, rows=None):
    """Sum raw source reports, independently of the production aggregation code/API."""
    truth = json.loads((FIXTURES / 'expected_results.json').read_text())
    period = truth['period']
    selected = [row for row in (fixture_rows('daily_reports') if rows is None else rows)
                if row['shop_id'] in shop_ids and period['start'] <= row['date'] < period['end_exclusive']]
    assert selected, 'No source fixture reports in the selected scope/period'
    by_shop = {sid: {field: sum(row[field] for row in selected if row['shop_id'] == sid)
                     for field in FINANCIAL_FIELDS} for sid in shop_ids}
    return {'total': {field: sum(row[field] for row in selected) for field in FINANCIAL_FIELDS},
            'by_shop': by_shop, 'period': period}


def check_financials(actual, shop_ids):
    expected = expected_financials(shop_ids)
    assert actual['period']['start'] == expected['period']['start'], actual['period']
    assert actual['period']['end_exclusive'] == expected['period']['end_exclusive'], actual['period']
    assert {row['shop_id'] for row in actual['by_shop']} == set(shop_ids), actual['by_shop']
    for field in FINANCIAL_FIELDS:
        assert actual['total'][field] == expected['total'][field], (field, actual['total'], expected['total'])
    for row in actual['by_shop']:
        for field in FINANCIAL_FIELDS:
            assert row[field] == expected['by_shop'][row['shop_id']][field], (row['shop_id'], field, row)
    return expected


def write_report(filename, report):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    report = {'verified_at': datetime.now(timezone.utc).isoformat(), **report}
    (ARTIFACTS / filename).write_text(json.dumps(report, ensure_ascii=False, indent=2))


@contextmanager
def fixture_client():
    """Use a disposable account, unaffected by the operator's additional memberships.

    Existing source evaluation inputs stay under data/. Local render/script outputs
    remain owned by the demo operator; disposable tokens/conversations are removed.
    """
    shops, end = fixture_context()
    actor = 'acceptqa-' + uuid4().hex[:16]
    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.username == 'operator-a'))
        assert owner, 'Seed the demo operator before acceptance'
        owner_id = owner.id
        db.add(User(id=actor, username=actor, role='operator', shop_id=shops[0],
                    display_name='临时验收账号', password_hash=password_hash(settings.demo_password)))
        db.flush()
        db.add_all(Membership(user_id=actor, shop_id=sid) for sid in shops)
        db.commit()
    try:
        with httpx.Client(base_url='http://127.0.0.1:8000', timeout=150) as client:
            auth = client.post('/api/auth/login', json={'username': actor, 'password': settings.demo_password})
            auth.raise_for_status()
            token = auth.json()['access_token']
            client.headers['Authorization'] = 'Bearer ' + token
            yield client, shops, end, token
    finally:
        with SessionLocal() as db:
            for model in (OpsJob, OpsScript, OpsSchedule):
                db.execute(update(model).where(model.user_id == actor).values(user_id=owner_id))
            for model in (Draft, Audit, ChatRun, Conversation, AccessToken, OpsImport, Membership):
                db.execute(delete(model).where(model.user_id == actor))
            db.execute(delete(User).where(User.id == actor))
            db.commit()
