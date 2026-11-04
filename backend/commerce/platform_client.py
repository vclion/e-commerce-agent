"""Read-only official platform clients. Simulation is an explicit, isolated transport."""
import hashlib
import hmac
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx


class PlatformError(Exception):
    def __init__(self, category, message, *, retryable=False):
        super().__init__(message)
        self.category, self.retryable = category, retryable


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e').replace('\\b', '\\u0008')


def signed_params(app_key, secret, method, arguments, token='', timestamp=None):
    params = dict(app_key=app_key, method=method, param_json=canonical_json(arguments),
                  timestamp=timestamp or datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S'), v='2')
    message = secret + ''.join(k + str(params[k]) for k in sorted(params)) + secret
    params['sign'] = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    params['sign_method'] = 'hmac-sha256'
    if token:
        params['access_token'] = token
    return params


def check_response(response, provider):
    if response.status_code == 429:
        raise PlatformError('rate_limit', '平台限流，稍后重试。', retryable=True)
    if response.status_code >= 500:
        raise PlatformError('network', '平台服务暂时不可用。', retryable=True)
    if response.status_code in (401, 403):
        raise PlatformError('authorization', '平台认证或权限不足，请检查应用权限与店铺/账号授权。')
    if response.status_code != 200:
        raise PlatformError('response', f'平台返回HTTP {response.status_code}，请检查平台配置。')
    try:
        value = response.json()
    except ValueError:
        raise PlatformError('schema', '平台返回了非JSON内容。')
    if not isinstance(value, dict):
        raise PlatformError('schema', '平台返回结构不符合接口契约。')
    nested = value.get('data')
    codes = [0 if str(value.get('code')) == '10000' else 'doudian_error'] if provider == 'doudian' else [value.get('err_no', 0), value.get('error_code', 0)]
    if isinstance(nested, dict):
        codes.extend([nested.get('error_code', 0), nested.get('err_no', 0)])
    if any(str(code) not in ('0', '', 'None') for code in codes):
        raw = str(value.get('sub_msg', '')) + str(value.get('message', '')) + str(value.get('msg', '')) + str(nested)
        lower = raw.lower()
        if any(x in lower for x in ('限流', 'quota', '频繁', 'rate limit', 'too many')):
            raise PlatformError('rate_limit', '平台调用额度或频率受限。', retryable=True)
        if any(x in lower for x in ('permission', '权限', 'scope', '授权', 'token', 'secret', '签名')):
            raise PlatformError('authorization', '平台权限、授权或签名校验失败，请核对应用配置。')
        raise PlatformError('platform', '平台拒绝请求；请在平台API日志中核对接口权限和业务参数。')
    if not isinstance(nested, (dict, list)):
        raise PlatformError('schema', '平台缺少data字段，未写入业务数据。')
    return nested


class Client:
    def __init__(self, connection, credentials):
        self.connection, self.credentials = connection, credentials
        transport = None
        if connection.mode == 'simulation':
            from .platform_fixtures import transport_for
            transport = transport_for(connection)
        self.http = httpx.Client(timeout=httpx.Timeout(25, connect=8), transport=transport, follow_redirects=False)
        self.calls = 0

    def close(self):
        self.http.close()

    def request(self, method, url, **kwargs):
        self.calls += 1
        if self.calls > 600:
            raise PlatformError('limit', '本轮达到600次请求上限，请缩小同步范围后重试。')
        try:
            return self.http.request(method, url, **kwargs)
        except httpx.HTTPError:
            raise PlatformError('network', '连接平台失败或超时，请检查网络后重试。', retryable=True)

    def doudian(self, method, arguments, token=None):
        params = signed_params(self.connection.app_key, self.credentials.get('app_secret', ''), method, arguments,
                               self.credentials.get('access_token', '') if token is None else token)
        response = self.request('POST', 'https://openapi-fxg.jinritemai.com/' + method.replace('.', '/'),
                                params=params, headers={'Content-Type': 'application/json'})
        return check_response(response, 'doudian')

    def douyin(self, path, arguments, *, method='GET', oauth=False):
        if oauth:
            response = self.request('POST', 'https://open.douyin.com' + path, data=arguments)
        else:
            response = self.request(method, 'https://open.douyin.com' + path,
                                    headers={'access-token': self.credentials.get('access_token', '')},
                                    **({'params': arguments} if method == 'GET' else {'json': arguments}))
        return check_response(response, 'douyin')

    def token(self, code=None):
        if self.connection.provider == 'doudian':
            value = self.doudian('token.create', {'code': '', 'grant_type': 'authorization_self',
                                                'shop_id': self.connection.external_id}, token='')
            if value.get('shop_id') is not None and str(value['shop_id']) != self.connection.external_id:
                raise PlatformError('authorization', '平台返回的店铺与配置不一致，未保存凭证。')
        else:
            refresh = self.credentials.get('refresh_token') or ('simulation-refresh' if self.connection.mode == 'simulation' else None)
            if not code and not refresh:
                raise PlatformError('authorization', '请先完成抖音账号授权，或填写有效API凭证。')
            value = self.douyin('/oauth/access_token/' if code else '/oauth/refresh_token/',
                {'client_key': self.connection.app_key, **({'client_secret': self.credentials.get('app_secret', ''),
                'code': code, 'grant_type': 'authorization_code'} if code else
                {'refresh_token': refresh, 'grant_type': 'refresh_token'})}, oauth=True)
        token = value.get('access_token')
        try:
            expires = int(value['expires_in'])
        except (ValueError, TypeError, KeyError):
            expires = 0
        if not token or not 0 < expires <= 31536000:
            raise PlatformError('schema', '平台凭证响应缺少有效token或有效期。')
        value['expires_in'] = expires
        return value

    def product_pages(self):
        for page in range(1, 101):
            data = self.doudian('product.listV2', {'page': page, 'size': 100})
            rows = extract_list(data, ('data', 'list', 'product_list'))
            for row in rows:
                pid = row.get('product_id') or row.get('id')
                if not pid:
                    raise PlatformError('schema', '商品列表缺少product_id。')
                detail = self.doudian('product.detail', {'product_id': str(pid)})
                skus = self.doudian('sku.list', {'product_id': str(pid)})
                yield {**row, **detail, 'product_id': str(pid), '_skus': extract_list(skus, ('sku_list', 'data', 'list'))}
            total = data.get('total') if isinstance(data, dict) else None
            if len(rows) < 100 or (total is not None and page * 100 >= int(total)):
                return
        raise PlatformError('limit', '商品超过单次分页范围，未标记同步完整。')

    def order_pages(self, start, end):
        for page in range(10000):
            data = self.doudian('order.searchList', {'page': page, 'size': 100, 'order_by': 'update_time',
                'order_asc': True, 'update_time_start': int(start.timestamp()), 'update_time_end': int(end.timestamp())})
            rows = extract_list(data, ('shop_order_list', 'list'))
            yield from rows
            total = data.get('total') if isinstance(data, dict) else None
            if len(rows) < 100 or (total is not None and (page + 1) * 100 >= int(total)):
                return
        raise PlatformError('limit', '订单超过分页范围。')

    def refund_pages(self, start, end):
        cursor, seen = '0', set()
        while cursor not in seen:
            seen.add(cursor)
            data = self.doudian('afterSale.List', {'start_index': cursor, 'size': 100, 'time_type': 2,
                'start_time': str(int(start.timestamp())), 'end_time': str(int(end.timestamp()))})
            rows = extract_list(data, ('after_sale_list', 'items', 'list'))
            for row in rows:
                aid = row.get('after_sale_id') or (row.get('after_sale_info') or {}).get('after_sale_id')
                if not aid:
                    raise PlatformError('schema', '售后列表缺少after_sale_id。')
                yield self.doudian('afterSale.Detail', {'after_sale_id': str(aid)})
            if not data.get('has_more', len(rows) >= 100):
                return
            next_cursor = data.get('next_start_index') or data.get('next_index')
            if next_cursor is None:
                raise PlatformError('schema', '售后分页未返回下一游标。')
            cursor = str(next_cursor)
        raise PlatformError('schema', '平台重复返回同一分页游标。')

    def video_pages(self):
        cursor, seen = 0, set()
        while str(cursor) not in seen:
            seen.add(str(cursor))
            data = self.douyin('/video/list/', {'open_id': self.connection.external_id, 'cursor': cursor, 'count': 20})
            rows = extract_list(data, ('list',))
            yield from rows
            if not data.get('has_more'):
                return
            cursor = data.get('cursor')
            if cursor is None:
                raise PlatformError('schema', '视频列表缺少下一页游标。')
        raise PlatformError('schema', '视频列表分页游标重复。')


def extract_list(data, keys):
    if isinstance(data, list):
        return data
    for key in keys:
        if isinstance(data.get(key), list):
            return data[key]
    raise PlatformError('schema', '平台返回的列表字段不符合当前适配契约，未当作空列表处理。')
