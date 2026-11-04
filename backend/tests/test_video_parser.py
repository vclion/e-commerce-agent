"""Parser integration boundary: unavailable, scoped, typed and replaceable."""
import asyncio

import pytest

from commerce import video_parser as parsing
from commerce.main import app

PATH = '/api/ops/videos/parse'
BODY = {'shop_id': 'a', 'url': 'https://v.douyin.com/example/'}


@pytest.fixture
def adapter():
    previous = app.dependency_overrides.get(parsing.get_video_parser)
    def install(parser):
        app.dependency_overrides[parsing.get_video_parser] = lambda: parser
    yield install
    if previous is None:
        app.dependency_overrides.pop(parsing.get_video_parser, None)
    else:
        app.dependency_overrides[parsing.get_video_parser] = previous


def test_unconfigured_parser_returns_no_invented_content(client, auth):
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 501
    assert response.json()['code'] == 'video_parser_not_configured'
    assert '尚未接入' in response.json()['detail']
    assert not {'video', 'transcript', 'subtitles'} & response.json().keys()


def test_parser_contract_is_available_in_openapi(client):
    response = client.get('/openapi.json')
    assert response.status_code == 200
    spec = response.json()
    operation = spec['paths'][PATH]['post']
    assert operation['requestBody']['content']['application/json']['schema']['$ref'].endswith('/VideoParseRequest')
    assert {'200', '400', '422', '501', '502', '504'} <= operation['responses'].keys()
    for key in ['caption', 'transcript', 'transcript_source', 'subtitles']:
        assert key in spec['components']['schemas']['VideoParseResponse']['properties']


@pytest.mark.parametrize('url', ['not a URL', 'file:///etc/passwd', 'javascript:alert(1)',
                               'https://username:password@example.com/video', 'https://example.com/' + 'a' * 4096])
def test_invalid_video_urls_are_rejected_before_parser(client, auth, adapter, url):
    class Unused:
        name = 'must-not-run'
        async def parse(self, *args, **kwargs):
            pytest.fail('Parser called for invalid input')
    adapter(Unused())
    assert client.post(PATH, headers=auth(), json={**BODY, 'url': url}).status_code == 422


def test_parser_requires_login_and_current_shop_scope(client, auth, adapter):
    class Unused:
        name = 'must-not-run'
        async def parse(self, *args, **kwargs):
            pytest.fail('Parser called before authorization')
    adapter(Unused())
    assert client.post(PATH, json=BODY).status_code == 401
    headers = {**auth(), 'X-Shop-ID': 'b'}
    assert client.post(PATH, headers=headers, json=BODY).status_code == 403
    assert client.post(PATH, headers=auth(), json={**BODY, 'shop_id': 'unknown'}).status_code == 403


def test_supplied_adapter_returns_separate_caption_and_timed_transcript(client, auth, adapter):
    calls = []
    class SuppliedTool:
        name = 'test-parser'
        async def parse(self, url, *, shop_id):
            calls.append((url, shop_id))
            return parsing.VideoParseContent(
                resolved_url='https://www.douyin.com/video/test-id',
                video=parsing.ParsedVideo(video_id='test-id', title='接口测试视频', duration_seconds=4),
                caption='发布文案，不是视频口播', transcript='第一句。第二句。', transcript_source='platform_subtitles',
                subtitles=[parsing.VideoSubtitle(start_seconds=0, end_seconds=2, text='第一句。'),
                           parsing.VideoSubtitle(start_seconds=2, end_seconds=4, text='第二句。')])
    adapter(SuppliedTool())
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == [(BODY['url'], 'a')]
    assert result['source_url'] == BODY['url'] and result['shop_id'] == 'a'
    assert result['parser'] == 'test-parser'
    assert result['caption'] == '发布文案，不是视频口播'
    assert result['transcript'] == '第一句。第二句。'
    assert result['subtitles'][1] == {'start_seconds': 2, 'end_seconds': 4, 'text': '第二句。'}


def test_video_without_subtitles_preserves_unknown_values(client, auth, adapter):
    class NoSubtitles:
        name = 'test-parser'
        async def parse(self, url, *, shop_id):
            return {'video': {'title': '无字幕测试视频'}, 'warnings': ['工具未返回字幕']}
    adapter(NoSubtitles())
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 200
    result = response.json()
    for key in ['transcript', 'transcript_source', 'subtitles', 'caption']:
        assert result[key] is None
    assert result['warnings'] == ['工具未返回字幕']


@pytest.mark.parametrize('payload', [
    {},
    {'video': {'title': '测试'}, 'subtitles': [{'start_seconds': 2, 'end_seconds': 1, 'text': '错误时间轴'}]},
    {'video': {'title': '测试', 'duration_seconds': 1}, 'subtitles': [{'start_seconds': 0, 'end_seconds': 2, 'text': '超出视频'}]},
    {'video': {'title': '测试'}, 'transcript_source': 'asr'},
])
def test_invalid_tool_output_is_not_success(client, auth, adapter, payload):
    class Invalid:
        name = 'invalid-tool'
        async def parse(self, url, *, shop_id):
            return payload
    adapter(Invalid())
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 502
    assert response.json()['code'] == 'video_parse_failed'


def test_tool_errors_and_unsupported_urls_are_sanitized(client, auth, adapter):
    class Broken:
        name = 'broken-tool'
        async def parse(self, url, *, shop_id):
            raise RuntimeError('private-upstream-config')
    adapter(Broken())
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 502 and 'private-upstream-config' not in response.text
    class Unsupported:
        name = 'unsupported-tool'
        async def parse(self, url, *, shop_id):
            raise parsing.UnsupportedVideoURL
    adapter(Unsupported())
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 400 and response.json()['code'] == 'video_url_unsupported'


def test_slow_parser_times_out(client, auth, adapter, monkeypatch):
    class Slow:
        name = 'slow-tool'
        async def parse(self, url, *, shop_id):
            await asyncio.sleep(1)
    adapter(Slow())
    monkeypatch.setattr(parsing, 'PARSER_TIMEOUT_SECONDS', 0.01)
    response = client.post(PATH, headers=auth(), json=BODY)
    assert response.status_code == 504 and response.json()['code'] == 'video_parse_timeout'
