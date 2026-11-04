# GitHub 同类 Agent 调研与功能建议

调研日期：2026-10-18。对象为当前“衣序·抖店运营 Agent”。

实施更新：首批“文案事实检查与定向改写”“字段映射与Excel导入”已完成，使用方式与验证见[导入与文案增强验收](../acceptance/导入与文案增强验收.md)。下文保留调研时的建议和范围。

结论：最接近产品定位的是 `3132882323-cyber/douyin-agent`；近期最值得借鉴的是文案事实检查与定向改写、真实报表适配、视频解析转写、素材视觉标签和配音字幕对齐。沿用现有 Vue、FastAPI、PostgreSQL、Dify 结构即可逐项实现。

核验范围：阅读公开仓库说明和下列关键源码，并对照本地实现；未部署或实测这些外部项目。下文“接入建议”“验收”均是针对本项目的设计判断，不代表外部项目已经实现这些具体行为。

## 当前已有能力与缺口

| 业务 | 当前实现依据 | 值得补充的部分 |
| --- | --- | --- |
| 经营数据导入 | `backend/commerce/ops_data.py` 支持 CSV、JSON、JSONL，已有校验、预览、去重 | 实际平台中文表头、字段映射模板、Excel 文件读取 |
| 每日工作 | `backend/commerce/ops_work.py` 按模板生成任务；缺报时提高优先级 | 按具体库存、发货风险决定任务优先级与触发条件 |
| 同行拆解 | `backend/commerce/ops_media.py` 使用提供的转写、媒体元数据及切换检测 | 自动转写与带时间证据的画面理解 |
| 网址解析 | `backend/commerce/video_parser.py` 已固定协议，默认返回 501 | 对接后续专用工具，保留现有请求与返回契约 |
| 脚本与成片 | 已有商品事实约束、分镜编辑、版本、FFmpeg 剪辑、字幕、封面与重试 | 文案事实复核、配音、音量控制及素材语义检索 |

`docs/design/电商运营Agent方案.md` 顶部说明“问题跟进”和“效果复盘”已按需求移除；此次建议不恢复这两个模块。

## 候选项目

### 1. douyin-agent：最接近的抖店运营副驾

[项目主页](https://github.com/3132882323-cyber/douyin-agent)；核验源码：[规则引擎](https://github.com/3132882323-cyber/douyin-agent/blob/c978755257cda95ba8700467f74cfe41cb3e7b3a/bridge/rule_engine.py)、[商品关联与诊断](https://github.com/3132882323-cyber/douyin-agent/blob/c978755257cda95ba8700467f74cfe41cb3e7b3a/bridge/douyin_commerce_graph.py)。

公开代码有确定性规则判断、诊断优先级与去重，商品关联依赖稳定 ID；README 还明确区分公开社区源码与内部商业能力。

接入建议：增强现有“今日工作”，让任务显示“涉及 SKU、触发指标、数据时间、建议动作”，根据发货紧迫度、库存风险和数据完整性排序。规则和阈值由后端计算，模型解释依据。先复用现有商品、订单、库存和任务表。

验收：同一 SKU 的库存与订单风险能关联；缺少稳定 ID 的记录提示待匹配；重复巡检不覆盖任务回填。此项只增强现有每日工作。

### 2. ecommerce-content-agent：生成后检查，再定向改写

[项目主页](https://github.com/aaadanzghe/ecommerce-content-agent)；核验源码：[检查](https://github.com/aaadanzghe/ecommerce-content-agent/blob/029d1985231e1f395aa87efc7e8a556411af890d/src/agents/compliance.py)、[改写](https://github.com/aaadanzghe/ecommerce-content-agent/blob/029d1985231e1f395aa87efc7e8a556411af890d/src/agents/rewrite.py)。

代码把规则命中与模型检查合并，再把评分、问题和商品事实交给改写步骤。其简单子串词表只适合作为实现参考，不能据此宣称内容满足全部平台规则。

接入建议：在现有脚本生成后，检查价格、面料、尺码、促销和性能表述是否有商品资料支撑；对问题句给出依据、改写建议和差异。时长、素材范围等继续由代码校验；模型评分作为编辑参考，最多自动改写一轮。

验收：资料没有的“纯棉”“限时五折”会被标记；运营锁定的分镜不被重写；改写后保存新版本，并重新检查商品事实。

### 3. NarratoAI：抽帧与视觉理解

[项目主页](https://github.com/linyqh/NarratoAI)；核验源码：[抽帧](https://github.com/linyqh/NarratoAI/blob/9fa69e022d4add41205ee385207561df8796b3f1/app/utils/video_processor.py)、[视觉适配器](https://github.com/linyqh/NarratoAI/blob/9fa69e022d4add41205ee385207561df8796b3f1/webui/utils/vision_analyzer.py)。

代码支持定时抽帧及视觉模型批量分析，可借鉴其分离媒体处理与模型理解的方式。

接入建议：给本店素材生成带时间区间的“全身试穿、面料近景、领口细节、上身搭配”等标签，让分镜推荐可用片段。同行拆解增加关键帧和时间证据。先让运营确认标签，再用于选段；标签不能代替商品面料等事实字段。

验收：点击标签能定位原片段；图片识别与语音转写分别注明来源；视觉服务失败时保留已有文字拆解能力。需要单独配置视觉服务，当前文字模型链路不能直接承担画面识别。

### 4. MoneyPrinterTurbo：配音、字幕与音轨组织

[项目主页](https://github.com/harry0703/MoneyPrinterTurbo)；核验源码：[内容与音频任务流程](https://github.com/harry0703/MoneyPrinterTurbo/blob/9e7d8b7efa6b912058156943906542b53b945f0a/app/services/task.py)。

项目有脚本、配音、字幕和视频合成流程，音频生成区分自定义音频与 TTS，并处理字幕生成。

接入建议：保留现有剪辑队列，在分镜上增加口播音轨、配音试听、原声/BGM 音量以及字幕对齐。按实际配音时长验证分镜，时长冲突时提示调整文案或选段。

验收：30 秒左右的服装短视频可试听、预览并导出；字幕不越界，音轨不被截断；重新渲染固定使用对应脚本版本。本店商品展示继续使用本店素材。

### 5. douyin-transcriber：网址到转写的实现参考

[项目主页](https://github.com/vangie/douyin-transcriber)；核验源码：[本地转写](https://github.com/vangie/douyin-transcriber/blob/80fc29b7edcfcc3e56eade5a40c54e1c7f564aaf/internal/transcriber/local.go)。

项目提供短链接解析、音频下载、本地 whisper.cpp 或云端转写的流程。检查到本地转写函数返回字符串，并清理输出中的时间信息，不能直接当成带时间轴字幕工具。

接入建议：借鉴“解析—取媒体—提音频—ASR”的分层，通过已有 `VideoParser` 协议适配后续提供的专用工具；字幕需要额外取得真实时间轴。耗时较长的转写适合复用后台任务机制。

验收：真实可访问视频得到有来源的转写；发布描述与口播分开；无时间轴时不伪造字幕。网页解析的当前可用性、WSL 部署和处理耗时仍需实测。

### 6. DA_Multi_Agent_Workflow：电商数据标准化与采集分层

[项目主页](https://github.com/liuchaoqi-7/DA_Multi_Agent_Workflow)；核验源码：[抖店订单服务](https://github.com/liuchaoqi-7/DA_Multi_Agent_Workflow/blob/e229ea96613a0fc803b10ad64e33a4831c36ee51/API_data_collect/order_D/doudian/order_service.py)。

项目把平台采集、字段转换和分析分开，订单服务有分页和重试。其 `_process_order` 取 `sku_order_list[0]`，不能直接套用本项目的父子订单统计。

接入建议：近期先借鉴字段适配层，增加真实导出报表模板和映射预览，将金额、日期、平台状态转换为本项目标准结构，再走现有校验和批次提交。后续取得平台接口权限，再实现相同标准结构的 API 适配器。

验收：中文 CSV/Excel 可映射、预览和提交；同一父订单的多条子订单完整保留；金额不重复换算；未知状态报出具体行号。真实平台模板最终以实际导出样例确认。

## 建议实施顺序

以下优先级与工作量为本项目适配判断。

| 顺序 | 功能 | 工作量 | 前置条件 | 主要落点 |
| --- | --- | --- | --- | --- |
| 1 | 文案事实检查与定向改写 | 较小 | 现有商品资料与脚本 | `ops_media.py`、Dify 内容 Workflow、脚本编辑页 |
| 2 | 报表字段映射与 Excel 导入 | 中等 | 实际导出表头样例；开发可先用标明来源的测试模板 | 导入适配层、`ops_data.py`、数据导入页 |
| 3 | 视频解析与语音转写 | 中等，外部依赖较多 | 后续专用解析工具、ASR 和真实视频验证 | `video_parser.py`、后台任务、同行参考页 |
| 4 | 风险触发任务与优先级 | 中等 | 库存、订单和发货时间完整 | `ops_work.py`、`ops_data.py`、今日工作页 |
| 5 | 配音、BGM 与字幕对齐 | 中等 | TTS 或提供的口播音频 | `ops_media.py`、`ops_worker.py`、剪辑页 |
| 6 | 素材视觉标签与选段推荐 | 中等至较大 | 视觉模型服务、实拍素材验证 | 素材元数据、媒体处理、脚本上下文 |

有专用解析工具后，第 3 项可提前，直接补齐“链接 → 转写 → 同行拆解”的缺口。若近期目标是接入真实经营数据，第 2 项应先于内容增强。

## 未列入近期范围的候选

[one-mini](https://github.com/AKA-liang/one-mini) 也有选品与财务分析 Agent，但其 README 列出浏览器 Profile 独占、登录态依赖、重复实现等限制。可以参考结果组织方式；现有选品评分已可用，近期收益低于上述缺口。

此次仅调研并形成建议，未接入外部服务、安装这些项目或修改业务代码。
