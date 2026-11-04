# DeepSeek 接入验收

> 本文保留早期三端接入的历史结果。2026-10-16起仅启用运营应用，当前结果见[运营单入口验收](运营单入口验收.md)。

日期：2026-10-15。API Key 已直接写入本地 Dify 的 DeepSeek 凭据配置，未写入源码、临时文件或业务`.env`。当时三个工作台绑定了各自的 Chatflow 服务 Key。

当前模型为官方 DeepSeek 插件中的`deepseek-flash`，Dify状态为`active`。

## 已执行的真实请求

| 角色 | 场景 | 实际工具 | 结果 |
| --- | --- | --- | --- |
| 消费者 | 200元以内、至少65W的充电器，推荐1款 | `tool.products` | 返回SKU-000-1，159.00元，65W |
| 商家 | 查询SKU-000-1并生成商品文案 | `tool.product_detail` | 读取商品事实，生成文案；未保存、未发布 |
| 运营 | 最近7个完整自然日的成交额、订单数、退款额 | `tool.analytics` | 最终复测金额与接口一致；未保存报告 |

通过登录鉴权、业务接入层、Dify应用、DeepSeek、业务工具和SSE回复的完整路径调用。记录保存在`tests/artifacts/reports/live-model-report.json`。

首轮运营回复出现一处金额换算错误，已改为业务层使用十进制精确转换并向模型返回人民币元字符串，避免模型再次换算。定向复测正确引用成交额41,674.00元、136笔订单和期间退款额178.00元。报告保留首轮与复测原始响应。

新增金额格式测试后，16项后端测试通过。该检查是少量真实调用的冒烟验证，不代表批量模型质量、提示注入效果或P95性能指标已经验收。

## 以后需要重新配置时

在Dify设置页面填写Key，或在WSL中运行以下命令并按隐藏提示输入：

```bash
cd ~/commerce-copilot
.venv/bin/python -m scripts.configure_deepseek
.venv/bin/python -m scripts.activate_dify
```

脚本不回显Key。刷新 http://localhost:8000 即可读取新的助手连接状态。
