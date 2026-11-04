"""Stable sidebar identities; conversation boundaries are checked by the server."""
from typing import Literal

Workspace = Literal['general', 'today', 'ops-analysis', 'ops-videos', 'ops-imports',
                    'platform', 'catalog', 'orders', 'research', 'drafts', 'audit']
WORKSPACES = {
    'general': '旧版总对话', 'today': '今日工作', 'ops-analysis': '全店分析',
    'ops-videos': '短视频运营', 'ops-imports': '数据导入', 'platform': '平台接入',
    'catalog': '商品资料', 'orders': '店铺订单', 'research': '选品研究',
    'drafts': '文案与报告', 'audit': '运行记录',
}

# Each tuple is (read/write, argument contract). The DSL and OpenAPI use this same registry.
OPERATIONS = {
    'workspace_context': ('read', '{}；查询当前栏目、可用操作和页面表单入口'),
    'ops_schedules': ('read', '{}'),
    'ops_schedule_set': ('write', '{shop_id,enabled:bool,hour?:0到23}'),
    'ops_assets': ('read', '{}；查询素材和运营商品'),
    'ops_peer_create': ('write', '{shop_id,title,transcript,source?,asset_id?}'),
    'ops_peer_analyze': ('write', '{reference_id}；调用现有完整同行分析流程并保存结果'),
    'ops_script_generate': ('write', '{product_id,reference_id?,brief?}；生成并保存可剪辑脚本'),
    'ops_script_review': ('write', '{script_id,expected_version}；事实检查'),
    'ops_script_rewrite': ('write', '{script_id,expected_version}；返回改写候选及差异，尚未保存；用户要求采用后用ops_script_save保存，保持锁定段'),
    'ops_render_retry': ('write', '{job_id}；仅重试已失败剪辑任务'),
    'ops_imports': ('read', '{}；查询当前范围内本人上传或校验的批次ID'),
    'ops_import_batch': ('read', '{batch_id}；查询校验错误与前5行预览'),
    'ops_import_fields': ('read', '{kind}；字段契约及示例'),
    'ops_import_mappings': ('read', '{}'),
    'ops_import_inspect': ('read', '{kind,filename,text,sheet?,mapping?}；检查用户提供的文本报表；Excel/大文件使用页面上传表单'),
    'ops_import_preview': ('write', '{kind,filename,text,sheet?,mapping?}；校验并生成批次，尚未入库'),
    'ops_import_commit': ('write', '{batch_id}；用户要求导入后提交已校验批次，错误批次不入库'),
    'ops_import_mapping_save': ('write', '{name,kind,mapping}；复用页面字段映射结构'),
    'ops_demo_import': ('write', '{}；仅在用户明确要求加载合成测试数据时执行，正式店铺禁止'),
    'content_list': ('read', '{}；本人营销文案与经营报告'),
    'content_save': ('write', '{kind:marketing或report,title,content,source_ids?:[]}；保存为草稿'),
    'competitors_import': ('write', '{items:[{product_id,title,price_fen,specs,source,captured_at:带时区时间,currency?:CNY}]}；保存用户提供的竞品快照，不访问外部网址'),
    'platform_connections': ('read', '{}；仅配置状态、缺失项和同步信息，不返回凭据'),
    'platform_products': ('read', '{connection_id}；查看该连接的已同步商品和可补录字段'),
    'platform_syncs': ('read', '{connection_id}'),
    'platform_connect': ('write', '{connection_id}；检查已配置连接，不接收凭据'),
    'platform_sync': ('write', '{connection_id,days?:1到90,resources?:[]}；排队同步，完成状态需查询platform_syncs'),
    'platform_schedule_set': ('write', '{connection_id,enabled:bool,interval_minutes?:15到1440}'),
    'platform_disconnect': ('write', '{connection_id}；用户明确要求时断开连接并停止后续同步'),
    'platform_product_facts_set': ('write', '{connection_id,product_id,evidence,cost_fen?,size?,color?,material?,fit?,season?,occasion?}；补录本地商品事实，不修改平台商品'),
    'audit_events': ('read', '{}；本人当前店铺范围业务与工具审计'),
    'chat_run_records': ('read', '{workspace?:栏目ID,status?:running或succeeded或failed或interrupted}；查询运行状态摘要。运行记录栏目可按指定栏目查看摘要，不自动读取其他栏目正文'),
}
READ_OPERATIONS = {op for op, (kind, _) in OPERATIONS.items() if kind == 'read'}
WRITE_OPERATIONS = set(OPERATIONS) - READ_OPERATIONS
EFFECT_LABELS = {
    'ops_sop_generate': '已生成或核对每日SOP', 'ops_task_update': '已更新任务',
    'ops_schedule_set': '已保存每日SOP计划', 'ops_peer_create': '已保存同行参考',
    'ops_peer_analyze': '已保存同行分析', 'ops_script_generate': '已生成并保存脚本',
    'ops_script_save': '已保存脚本', 'ops_script_review': '已完成文案事实检查',
    'ops_script_rewrite': '已生成改写候选，尚未保存', 'ops_render': '已提交剪辑请求',
    'ops_render_retry': '已将失败剪辑重新排队', 'ops_import_preview': '已生成报表校验结果',
    'ops_import_commit': '已提交导入批次', 'ops_import_mapping_save': '已保存字段映射模板',
    'ops_demo_import': '已加载合成测试数据', 'content_save': '已保存内容草稿',
    'competitors_import': '已保存竞品快照', 'platform_connect': '已完成连接检查',
    'platform_sync': '已提交同步请求', 'platform_schedule_set': '已保存同步计划',
    'platform_disconnect': '已断开平台连接', 'platform_product_facts_set': '已补录本地商品事实',
}

# Read access remains shop-scoped, so a report can cite products/tasks across modules.
# Writes are bound to the originating column and cannot be enabled by model arguments.
WORKSPACE_WRITES = {
    'today': {'ops_sop_generate', 'ops_task_update', 'ops_schedule_set', 'content_save'},
    'ops-analysis': {'content_save'},
    'ops-videos': {'ops_peer_create', 'ops_peer_analyze', 'ops_script_generate',
                   'ops_script_save', 'ops_script_review', 'ops_script_rewrite',
                   'ops_render', 'ops_render_retry', 'content_save'},
    'ops-imports': {'ops_import_preview', 'ops_import_commit', 'ops_import_mapping_save', 'ops_demo_import'},
    'platform': {'platform_connect', 'platform_sync', 'platform_schedule_set', 'platform_product_facts_set', 'platform_disconnect'},
    'catalog': {'platform_product_facts_set'},
    'orders': set(), 'research': {'competitors_import', 'content_save'},
    'drafts': {'content_save'}, 'audit': set(),
}


def allowed_writes(workspace):
    if workspace == 'general':
        return {'ops_sop_generate', 'ops_task_update', 'ops_script_save', 'ops_render'}
    return WORKSPACE_WRITES.get(workspace, set())


def operation_instructions():
    return '\n'.join(f'{op}:{description}' for op, (_, description) in OPERATIONS.items())
