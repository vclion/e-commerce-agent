"""Offline fixture tests; deliberately outside backend's database conftest."""
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
import tempfile

import pytest

from scripts.generate_ops_data import build, expected_results, generate, validate


@pytest.fixture(scope='module')
def fixture_data():
    tables, meta = build()
    return tables, meta, expected_results(tables, meta)


def test_reproducible_files_and_alternative_date():
    with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
        one, two = generate(Path(first)), generate(Path(second))
        assert one['hashes'] == two['hashes']
        assert one['counts'] == two['counts']
    tables, meta = build(date(2026, 11, 1))
    assert meta['period_end_exclusive'] == '2026-11-01'
    assert validate(tables, meta, expected_results(tables, meta))


def test_totals_can_be_independently_recomputed_from_raw_rows(fixture_data):
    tables, meta, expected = fixture_data
    lower, upper = expected['period']['start'], expected['period']['end_exclusive']
    orders = [o for o in tables['orders'] if o['paid_at'] and lower <= o['paid_at'][:10] < upper]
    refunds = [r for r in tables['refunds'] if r['completed_at'] and lower <= r['completed_at'][:10] < upper]
    assert expected['total']['paid_fen'] == sum(o['paid_fen'] for o in orders)
    assert expected['total']['refund_fen'] == sum(r['refund_fen'] for r in refunds)
    assert expected['total']['paid_parent_orders'] == len({o['parent_order_id'] for o in orders})
    assert expected['total']['paid_sub_orders'] > expected['total']['paid_parent_orders']
    assert any(r['refund_type'] == 'partial' and r['refund_fen'] > 0 for r in refunds)
    assert any(next(o for o in tables['orders'] if o['sub_order_id'] == r['sub_order_id'])['paid_at'][:10] < lower for r in refunds)


def test_corrupted_daily_amount_is_rejected(fixture_data):
    tables, meta, expected = fixture_data
    changed = deepcopy(tables)
    changed['daily_reports'][0]['paid_fen'] += 1
    with pytest.raises(AssertionError):
        validate(changed, meta, expected)


def test_cross_shop_sku_is_rejected(fixture_data):
    tables, meta, expected = fixture_data
    changed = deepcopy(tables)
    changed['orders'][0]['shop_id'] = 'b'
    with pytest.raises(AssertionError):
        validate(changed, meta, expected)


def test_duplicate_report_is_rejected(fixture_data):
    tables, meta, expected = fixture_data
    changed = deepcopy(tables)
    changed['daily_reports'].append(dict(changed['daily_reports'][0]))
    with pytest.raises(AssertionError):
        validate(changed, meta, expected)


def test_missing_video_measurement_and_attribution(fixture_data):
    tables, meta, expected = fixture_data
    assert expected['missing_completed_views'] == [dict(video_id='TEST-VIDEO-b-29', date=(date.fromisoformat(meta['period_end_exclusive']) - timedelta(days=1)).isoformat())]
    assert sum(r['attributed_paid_fen'] for r in tables['video_daily']) == sum(r['paid_fen'] for r in tables['orders'] if r['video_id'])
    a = next(r for r in expected['by_shop'] if r['shop_id'] == 'a')
    b = next(r for r in expected['by_shop'] if r['shop_id'] == 'b')
    simple_average = (a['buyer_day_conversion'] + b['buyer_day_conversion']) / 2
    assert abs(simple_average - expected['total']['buyer_day_conversion']) > .000001
