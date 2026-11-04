# 电商运营 Agent

面向抖店商品销售与快递履约的运营工作台。项目仅服务运营人员，使用 Vue 3 + TypeScript、FastAPI、PostgreSQL 和本地 Dify；模型通过 Dify 调用 DeepSeek。

当前是可运行的本地测试版本，内置店铺、商品、订单、报表与视频素材为合成数据。报表导入、每日 SOP、短视频分析、同行拆解、脚本生成、实际视频剪辑已接入；已加载双店测试数据，可直接登录测试。新增“平台接入”提供商家自研配置、官方只读客户端、自动同步及独立离线演练；当前没有真实凭证，尚未完成真实平台授权验收。详见[商家自研平台接入](docs/guides/商家自研平台接入.md)。

## 当前入口

- 运营工作台：[http://localhost:8000](http://localhost:8000)
- Dify：[http://localhost:8088](http://localhost:8088)
- 业务 API 文档：[http://localhost:8000/docs](http://localhost:8000/docs)
- 本地登录信息：`.runtime/本地访问信息.md`，含密码，不进入 Git。

默认账号 `operator` 映射到既有 `operator-a`，`operator-a` 与 `operator-b` 继续可用。登录后默认查看全部授权店铺，也可在侧栏筛选单店；切店不改变账号和历史归属。

消费者、商家及测试买家不能登录，已签发的旧身份令牌也不能访问业务接口或助手工具。历史买家记录保留用于订单外键与历史数据，不再作为交互身份。

## 已实现

- 平台接入：保存待配置连接、服务端凭证加密、抖店自研凭证获取、抖音OAuth或API凭证、逐资源同步状态、增量与历史补数、定时任务、有限重试与重启恢复。正式店铺与演练数据分开；支持补充有依据的商品成本和规格并在同步后保留。

- 全店分析：使用已导入报表，统计成交、期间退款、净收款、父子订单与加权转化，核查缺报日期、低库存、超时发货和订单对账。
- 数据导入：CSV、JSON、JSONL、Excel（.xlsx）文件校验、工作表选择、中文表头映射、映射模板复用、样例预览、批次提交和重复去重。标准字段金额为分；映射模式逐字段确认元/分，未带时区的时间可按上海或UTC转换。详见[导入与文案增强说明](docs/acceptance/导入与文案增强验收.md)。
- 每日工作：每日定时或手动生成 SOP，重复执行不重置任务；负责人、状态、证据、阻塞原因与历史记录均保存。
- 短视频运营：导入账号视频与日指标，区分累计快照和增量，展示播放、点击、完播和可归因支付；缺失指标保留为未知。
- 视频网址解析接口（预留）：`POST /api/ops/videos/parse`，约定视频信息、发布文案和时间轴字幕的返回结构；专用工具未接入时返回501。接入说明见[短视频网址解析接口](docs/guides/短视频网址解析接口.md)。
- 同行拆解：接收转写与参考素材，结合实测时长、场景切换，通过 DeepSeek 生成结构拆解；保留依据和局限。
- 脚本与剪辑：基于本店商品事实、同行参考和素材生成分镜字幕，逐段编辑并保存版本；FFmpeg 实际截取、竖屏裁切、拼接、保留源音轨、烧录中文字幕，导出 MP4、封面和 SRT。队列持久化、失败可重试、服务重启可恢复。
- 文案检查与改写：脚本保存时检查价格、面料含量、尺码、促销及常见性能表述；可按当前商品资料重新检查。通过现有Dify工作流定向改写一轮，保留正常字幕、锁定分镜和素材时间范围，预览差异后采用为新版本。
- 商品资料：按品类、售价、尺码、面料与版型筛选，查询具体 SKU 和库存，比较商品事实。
- 店铺订单：读取授权店铺的最近订单与售后处理记录，不再提供消费者申请售后的操作。
- 选品研究：确定性评分、资料不足候选、竞品快照导入及对比。
- 运营内容：基于商品事实生成短视频口播或营销文案草稿，保存营销文案和经营报告。
- 对话：流式回复，登录和刷新后恢复历史，加载更早消息，按账号、店铺范围和左侧栏目隔离；切回栏目继续原会话，旧版总对话独立保留。新增报表提交、脚本检查与改写、剪辑重试、草稿保存、平台同步等聊天工具，写操作按栏目授权；实际成功操作显示后端执行结果标记。详见[栏目会话与聊天工具](docs/guides/栏目会话与聊天工具.md)。
- 对话恢复：明确显示模型连接、认证或限流问题，断流保留部分正文，失败消息可点击重试。本机 Windows/WSL 代理网络修复及验证见[对话连接修复](docs/acceptance/对话连接修复验收.md)。
- Agent 执行：可按指令生成 SOP、回填任务、保存脚本和创建本地剪辑任务；每轮最多8次业务调用，所有读写和媒体下载均检查授权店铺。
- Dify：1个运营 Chatflow、6个 Workflow（商品筛选、营销文案、选品研究、经营分析、视频脚本、同行拆解），30份知识资料。

已移除商品新增/编辑/上架、消费者售后提交及动作确认接口、页面和工具定义。旧 Dify 消费者与商家应用已关闭 API 与站点入口，旧 DSL 留有本机回退备份。

## 运营测试数据

商品资料和选品研究保留 `apparel-v1` 的商品与竞品快照。新版运营模块使用独立导入表，店铺订单、售后和全店分析优先使用导入数据；不将两套订单合计。原始资料说明见[服装数据说明](docs/guides/服装数据说明.md)。

新增 [`douyin-ops-v1`](data/douyin-ops-v1/README.md) 覆盖两家店60天数据：11,620条子订单、625条售后、120份日报、库存、60条视频与756条视频日指标、48条运营任务、脚本及占位剪辑素材。附18个验收场景与预期结果，以及CSV、JSONL和Excel查看版。

测试数据包已接入当前工作台。进入“数据导入”点击“加载完整测试数据”，即可重复加载；已跟进的任务、脚本与拆解结果保留。固定数据截止时间为2026-10-16 10:00（上海），最近7日两店支付合计443,315元、期间退款46,580元。完整使用方式与验收见[运营闭环验收](docs/acceptance/运营闭环验收.md)。

报表支持自定义字段映射和Excel读取；具体平台导出版本仍需按实际样例确认字段、单位和状态含义，未内置已验证的真实平台模板。官方平台自动同步已提供配置和离线演练，尚未以真实账号验收；自动发布、语音转写和语义画面识别尚未接入。生成与改写脚本调用真实 DeepSeek，剪辑处理真实本地媒体；测试素材是带标识的占位动画和提示音，不是服装实拍或中文配音。

## 启动

在 WSL 的项目根目录执行。要求已安装 Docker、Compose、Git 和 uv，版本约束见 `runtime.lock.json`。

```bash
cd ~/e-commerce-agent
bash scripts/dev.sh
```

此命令保留现有配置、数据库和演示记录。日志位于 `.runtime/api.log`。

分开开发时，在项目根目录运行后端：

```bash
.venv/bin/python scripts/start.py --stop
uv run uvicorn commerce.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir backend/commerce
```

另一个终端启动前端：

```bash
export PATH="$PWD/.runtime/node/bin:$PATH"
npm --prefix frontend run dev
```

开发地址为 `http://localhost:5173`；Vite 将 `/api` 代理到业务服务。演示入口由 FastAPI 提供构建后的前端。

## Dify 与模型

```bash
bash scripts/dify_up.sh
```

全新 Dify 环境按以下顺序初始化，已有环境不必重复安装插件或知识库：

```bash
.venv/bin/python -m scripts.dify_console
.venv/bin/python -m scripts.setup_integrations --plugins
# 在Dify插件页面检查官方DeepSeek与Agent策略插件已安装完成。
.venv/bin/python -m scripts.prepare_knowledge
.venv/bin/python -m scripts.setup_integrations --api-tools
.venv/bin/python -m scripts.generate_dify
.venv/bin/python -m scripts.import_dify
.venv/bin/python -m scripts.setup_integrations --workflow-tools
.venv/bin/python -m scripts.generate_dify
.venv/bin/python -m scripts.import_dify
```

在 Dify 的“设置 → 模型供应商 → DeepSeek”中配置自己的 API Key，然后执行：

```bash
.venv/bin/python -m scripts.activate_dify
.venv/bin/python -m scripts.write_access
```

激活脚本检查模型可用状态，只绑定运营 Chatflow，移除业务 `.env` 中已废弃的消费者/商家应用 Key 配置，并重启工作台。模型密钥保留在 Dify，不写入源码或公共 DSL。

当前应用使用官方 `deepseek-flash`、默认关闭思考模式、输出上限4096 token。版本和插件映射保存在 `runtime.lock.json`、`dify/plugins.lock.json`；当前工作空间应用映射见 `dify/apps.lock.json`，不能直接复用到另一个 Dify 空间。

## 验证

2026-10-17全面检查已完成，修复加载状态、时间显示、切店素材残留、历史任务查询和报表异常等问题。结果见[全面细节检查](docs/acceptance/全面细节检查.md)。

```bash
.venv/bin/python -m pytest -q --junitxml=tests/artifacts/acceptance-repair/backend-junit.xml
npm --prefix frontend run build
.venv/bin/python -m tests.scripts.browser_smoke
.venv/bin/python -m tests.scripts.browser_ops_smoke
# 官方接入离线演练；临时账号和连接检查后清理，不调用真实平台或模型。
.venv/bin/python -m tests.scripts.browser_platform_smoke
# 字段映射、Excel、文案检查与改写；1次真实模型调用，使用并清理临时账号与脚本。
.venv/bin/python -m tests.scripts.browser_enhancements_smoke
# 运营闭环验收：2次真实模型 Workflow 调用，生成可播放成片。
.venv/bin/python -m tests.scripts.smoke_ops
# 原生 Agent 调用运营查询、SOP 和剪辑工具，3次真实对话。
.venv/bin/python -m tests.scripts.smoke_ops_agent
.venv/bin/python -m tests.scripts.smoke_dify
.venv/bin/python -m tests.scripts.live_smoke --role operator
# 对话恢复专项验收，会产生2次真实模型调用，使用临时运营账号。
.venv/bin/python -m tests.scripts.browser_chat_smoke
# 对话失败恢复：模拟断流和手动重试，再执行2次真实模型对话。
.venv/bin/python -m tests.scripts.browser_chat_recovery_smoke
# 栏目会话隔离、流式切换、执行结果标记；3次真实模型调用，使用并清理临时账号。
.venv/bin/python -m tests.scripts.browser_workspace_chat_smoke
# 旧应用停用与营销Workflow验收，会产生1次真实模型调用。
.venv/bin/python -m tests.scripts.verify_operator
.venv/bin/python -m tests.scripts.verify_delivery
```

后端测试在独立 PostgreSQL schema 和临时媒体目录中运行，不改写现有演示数据。浏览器测试会重复导入测试日报，并清理本轮创建的唯一草稿。真实闭环测试会保留生成的脚本和成片，方便人工检查。结果见[运营闭环验收](docs/acceptance/运营闭环验收.md)，报告位于 `tests/artifacts/reports/`，截图位于被 Git 忽略的 `tests/artifacts/screenshots/`，数据包位于 `tests/artifacts/packages/`。

验收脚本统一位于 `tests/scripts/`，使用上面的 `python -m tests.scripts.<模块名>` 命令；原 `scripts/` 下的重复兼容入口已移除。运营闭环、原生 Agent、运营浏览器和交付检查的报告与截图写入 `tests/artifacts/acceptance-repair/`，其余输出分类见[测试目录说明](tests/README.md)。运营验收使用临时账号限定测试资料中的店铺范围，从原始日报计算金额预期，不依赖主账号当前新增的店铺。临时账号会清理；实际生成的本地脚本和成片转交演示运营账号保留。交付检查按工作流源文件核对全部发布应用，使用上述 JUnit 报告，并记录证据时间；也可通过 `--junit 路径` 明确指定报告。

知识库混合检索升级使用 Dify 原生索引，保留原文档 ID 与引用。先运行只读计划：

```bash
.venv/bin/python -m scripts.upgrade_knowledge
.venv/bin/python -m tests.scripts.knowledge_smoke
```

获得下载许可并安装、配置 Tongyi 嵌入模型插件后，才执行以下升级和验收。升级程序本身不会下载插件或依赖；执行前备份索引配置，索引就绪后切换查询配置，失败时尝试恢复原配置。混合检索的初始权重为语义 0.7、关键词 0.3，初始阈值 0.45，必须结合本项目问题集验收后调整，不能视为通用最优值。

```bash
.venv/bin/python -m scripts.upgrade_knowledge --apply
.venv/bin/python -m tests.scripts.knowledge_smoke --require-hybrid
.venv/bin/python -m tests.scripts.knowledge_smoke --held-out --require-hybrid
.venv/bin/python -m tests.scripts.knowledge_agent_smoke
```

对比报告位于 `tests/artifacts/knowledge-hybrid/`。2026-10-22 已安装 Tongyi 0.2.23 并启用混合检索：30 篇资料均通过逐篇向量检索，固定问题集从 5/9 提升为 9/9，另一个独立问题集通过 6/6；这些结果仅覆盖本项目合成资料和对应问题。实际 Agent 调用及鉴权读取原文档通过。迁移使用原文逐篇重建索引，避免只改资料库设置后出现状态完成但向量不存在的情况。

离线数据生成和校验：

```bash
.venv/bin/python -m scripts.generate_ops_data --media
.venv/bin/python -m scripts.generate_ops_data --check
.venv/bin/python -m pytest tests/unit/test_ops_data.py -q
```

## 项目结构

| 目录 | 职责 |
| --- | --- |
| `frontend/src/` | 运营页面、API客户端与样式 |
| `backend/commerce/` | 认证、店铺授权、查询、统计、知识访问与会话 |
| `backend/tests/` | 独立数据库集成测试 |
| `scripts/` | 启动、Dify配置、知识库升级与数据生成 |
| `tests/` | 离线单元测试、验收脚本及本地测试产物 |
| `dify/` | 运营Chatflow、6个业务Workflow、记忆整理Workflow和工作空间映射 |
| `contracts/` | Dify运营读写工具契约 |
| `data/` | 原有服装演示数据与新版运营测试包 |
| `knowledge/` | 商品与运营知识资料 |
| `docs/` | 使用说明、设计、验收与调研，见[文档导航](docs/README.md) |
| `.runtime/` | 私有凭据、日志、Node、回退备份与历史维护归档，Git忽略 |
| `vendor/dify/` | 固定版本的官方Dify源码，Git忽略 |

前后端依赖分别声明在 `frontend/package.json` 与 `backend/pyproject.toml`，Python工作区统一锁定在根目录 `uv.lock`。开发约定见[项目结构](docs/项目结构.md)。

## 当前边界

官方读取客户端、授权配置和同步框架已实现并提供离线演练，但没有真实凭证，不能声称已获得真实店铺数据；首次授权仍需按实际权限与返回核对。当前没有支付、退款执行、商品上架、视频发布或投放；视频语音转写与语义画面识别仍待接入。已有分析、SOP和剪辑可使用合成数据验收。没有流量时不计算转化率，没有完整费用时不报告净利润。

记忆系统已提供跨会话偏好、工作摘要、业务事件、经验候选、版本纠正、有效期、遗忘、店铺与栏目隔离，以及管理界面；详见[实施与验收](docs/acceptance/记忆系统实施与验收.md)。Embedding 已切换并启用阿里百炼 DashScope `text-embedding-v3`（1024 维），真实索引、语义召回、店铺隔离、纠正和遗忘 5 项检查通过。更换模型后自动补建向量索引，旧模型向量不混入召回，不运行本地模型。所有下载必须先获得用户明确允许。

上下文默认只带最近 3 轮完整原文、简短任务线索和少量活跃摘要索引。旧段按需读取，久未使用的索引退出上下文但仍可搜索；详见[索引式上下文管理](docs/design/索引式上下文管理.md)。超预算时提供[响应式压缩兜底](docs/design/响应式上下文压缩.md)，保留最新原文，且仅在无工具调用时允许一次上游超限重试。

当前知识库检索仍采用 Dify economy 关键词索引，与 Agent 记忆索引分开。记忆使用数据库队列和编号迁移，尚未引入生产级调度平台、Redis、通用数据库迁移框架或压测，当前面向本机测试。
