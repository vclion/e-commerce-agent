"""Acceptance must detect wrong results without depending on shared operator state."""
from copy import deepcopy

import pytest

from tests.scripts.acceptance import (expected_financials, fixture_context, fixture_rows,
                                      check_financials, FINANCIAL_FIELDS)
from tests.scripts.verify_delivery import application_sources, read_junit


def test_source_expectations_handle_single_multiple_and_additional_shops():
    shops, end = fixture_context()
    rows = fixture_rows('daily_reports')
    original = expected_financials(shops, rows)
    extra = {**rows[-1], 'shop_id': 'extra-acceptance-shop', 'date': original['period']['start']}
    for field in FINANCIAL_FIELDS:
        extra[field] = 123
    expanded = rows + [extra]
    assert expected_financials(shops, expanded) == original
    result = expected_financials((*shops, extra['shop_id']), expanded)
    for field in FINANCIAL_FIELDS:
        assert result['total'][field] == original['total'][field] + 123
        assert sum(expected_financials((sid,), rows)['total'][field] for sid in shops) == original['total'][field]


def test_acceptance_rejects_corrupt_financials_and_unexpected_scope():
    shops, _ = fixture_context()
    expected = expected_financials(shops)
    actual = {'period': expected['period'], 'total': expected['total'],
              'by_shop': [dict(shop_id=sid, **values) for sid, values in expected['by_shop'].items()]}
    check_financials(actual, shops)
    corrupt = deepcopy(actual)
    corrupt['total']['paid_fen'] += 1
    with pytest.raises(AssertionError):
        check_financials(corrupt, shops)
    corrupt = deepcopy(actual)
    corrupt['by_shop'].append({'shop_id': 'unauthorized'})
    with pytest.raises(AssertionError):
        check_financials(corrupt, shops)


def test_app_expectations_follow_workflow_sources(tmp_path):
    for folder, name in [('chatflows', 'operator'), ('workflows', 'new-maintenance')]:
        path = tmp_path / 'dify' / folder
        path.mkdir(parents=True)
        (path / f'{name}.yml').write_text('app: {}')
    assert set(application_sources(tmp_path)) == {'operator', 'new-maintenance'}


@pytest.mark.parametrize('xml,status,passed', [
    ('<testsuite tests="4" skipped="1" failures="0" errors="0"/>', 'passed', 3),
    ('<testsuites><testsuite tests="4" failures="1" errors="1" skipped="1"/></testsuites>', 'failed', 1),
])
def test_selected_junit_does_not_hide_failures(tmp_path, monkeypatch, xml, status, passed):
    from tests.scripts import verify_delivery
    monkeypatch.setattr(verify_delivery, 'ROOT', tmp_path)
    path = tmp_path / 'current.xml'
    path.write_text(xml)
    evidence = read_junit(path)
    assert evidence['status'] == status and evidence['passed'] == passed
    assert evidence['verified_at']


def test_missing_junit_is_explicitly_unverified(tmp_path):
    assert read_junit(tmp_path / 'missing.xml')['status'] == 'missing'
