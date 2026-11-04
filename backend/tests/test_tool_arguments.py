"""Regress Dify's observed Python-repr transport without accepting expressions."""
import json

import pytest
from pydantic import ValidationError

from commerce.schemas import ToolCall
from commerce.tool_arguments import MAX_LITERAL_CHARS, MAX_LITERAL_NODES, MAX_LITERAL_DEPTH


@pytest.mark.parametrize('encoding', [lambda value: value, json.dumps, repr])
def test_tool_arguments_preserve_nested_content_and_types(encoding):
    arguments = {'script_id': 'TEST-SCRIPT-a', 'expected_version': 1,
        'segments': [{'caption': "中文 O'Reilly 与 \"双引号\"，不替换引号\n第二行",
                      'locked': True, 'out_seconds': 4.5, 'optional': None}],
        'negative': -1, 'disabled': False}
    assert ToolCall(operation='ops_render', arguments=encoding(arguments)).arguments == arguments


@pytest.mark.parametrize('value', [
    "{'expected_version': 1, 'script_id': 'TEST-SCRIPT-a'}",
    '{"expected_version": 1, "script_id": "TEST-SCRIPT-a"}',
])
def test_original_render_failure_payload_is_decoded(value):
    assert ToolCall(operation='ops_render', arguments=value).arguments == {
        'expected_version': 1, 'script_id': 'TEST-SCRIPT-a'}


@pytest.mark.parametrize('value', [
    "{'script_id': __import__('os').getcwd()}",
    "{'expected_version': 1 + 1}",
    "{'items': [x for x in []]}",
    "{'items': [0] * 1000000000}",
    "{'items': (1, 2)}", "{'items': {1, 2}}", "{'bytes': b'x'}",
    "{1: 'non-string key'}", "{'nested': {None: 'non-string key'}}",
    "{'number': 1e999}", "{'complex': 1j}", "{'ellipsis': ...}",
    '[]', 'null', '1', '"not an object"', "{'broken':", '',
])
def test_invalid_or_executable_arguments_remain_validation_errors(value):
    with pytest.raises(ValidationError):
        ToolCall(operation='ops_render', arguments=value)


@pytest.mark.parametrize('value', [
    "{'text': '" + 'x' * MAX_LITERAL_CHARS + "'}",
    "{'items': [" + ','.join('None' for _ in range(MAX_LITERAL_NODES)) + ']}',
    "{'items': " + '[' * MAX_LITERAL_DEPTH + 'None' + ']' * MAX_LITERAL_DEPTH + '}',
])
def test_compatibility_parser_bounds_untrusted_literals(value):
    with pytest.raises(ValidationError):
        ToolCall(operation='ops_render', arguments=value)


def test_compatibility_limit_does_not_restrict_standard_json():
    arguments = {'text': 'x' * (MAX_LITERAL_CHARS + 1)}
    assert ToolCall(operation='ops_import_preview', arguments=json.dumps(arguments)).arguments == arguments
