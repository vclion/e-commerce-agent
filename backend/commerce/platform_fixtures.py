"""Small synthetic native API responses; never used by a live connection."""
import json
from datetime import datetime, timedelta, timezone
import httpx


def transport_for(connection):
    def handle(request):
        at = datetime.now(timezone.utc)
        stamp = lambda day: int((at - timedelta(days=day)).timestamp())
        method = request.url.params.get('method')
        if method == 'token.create':
            value = dict(access_token='simulation-access', refresh_token='simulation-refresh', expires_in=7200,
                         shop_id=connection.external_id)
        elif method == 'product.listV2':
            value = dict(data=[dict(product_id='900001', name='接入演练棉衬衫', status=0)], total=1)
        elif method == 'product.detail':
            value = dict(product_id='900001', name='接入演练棉衬衫', description='合成接口演练商品，非实拍。',
                         category_name='衬衫', product_format_new={'面料': '100%棉'}, status=0)
        elif method == 'sku.list':
            value = dict(sku_list=[dict(sku_id='910001', price=19900, stock_num=12,
                                       spec_detail_name1='云白', spec_detail_name2='M')])
        elif method == 'order.searchList':
            value = dict(shop_order_list=[dict(order_id=f'92000{i}', order_status=3,
                create_time=stamp(i), pay_time=stamp(i), update_time=stamp(0),
                sku_order_list=[dict(order_id=f'93000{i}', sku_id='910001', product_id='900001',
                    product_name='接入演练棉衬衫', item_num=1, pay_amount=19900,
                    order_status=3, ship_time=stamp(i) + 60)]) for i in range(1, 8)], total=7)
        elif method == 'afterSale.List':
            value = dict(after_sale_list=[dict(after_sale_id='940001')], has_more=False)
        elif method == 'afterSale.Detail':
            value = dict(after_sale_info=dict(after_sale_id='940001', refund_amount=19900, after_sale_status=6,
                reason='接口演练退款', apply_time=stamp(2), refund_time=stamp(1), update_time=stamp(0)),
                order_info=dict(order_id='930003', sku_id='910001'))
        elif request.url.path in ('/oauth/access_token/', '/oauth/refresh_token/'):
            value = dict(access_token='simulation-video-access', refresh_token='simulation-video-refresh',
                         expires_in=7200, open_id='simulation-author', scope='video.list,data.external.item')
        elif request.url.path == '/video/list/':
            value = dict(list=[dict(item_id='SIM-VIDEO-001', title='接口演练视频', create_time=stamp(3),
                share_url='https://www.douyin.com/', statistics=dict(play_count=1200, digg_count=40,
                comment_count=3, share_count=2))], has_more=False)
        elif request.url.path == '/data/external/item/base/':
            value = dict(result=dict(total_play=1250, total_like=43, total_comment=4, total_share=2))
        else:
            return httpx.Response(400, json={'code': 1, 'message': 'Unsupported simulation API'})
        return httpx.Response(200, json={'code': 10000, 'msg': 'success', 'data': value} if method else {'data': {**value, 'error_code': 0}})
    return httpx.MockTransport(handle)
