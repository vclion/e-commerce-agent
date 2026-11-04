# 测试与验收

在项目根目录使用已有 `.venv` 执行。源代码与本地生成产物分开存放：

| 位置 | 用途 |
| --- | --- |
| `backend/tests/` | 后端集成测试，使用独立 PostgreSQL schema |
| `tests/unit/` | 不依赖数据库、模型与平台的离线数据测试 |
| `tests/scripts/` | 浏览器、模型、知识库、记忆和运营闭环验收入口 |
| `tests/artifacts/` | 所有本地验收报告、截图和压缩包，Git 忽略 |

## 离线检查

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/unit -q -p no:cacheprovider
```

默认 `pytest` 同时收集 `backend/tests/` 与 `tests/unit/`；后端测试需要现有数据库。手动验收脚本不会由默认 `pytest` 执行。

## 手动验收入口

统一使用 `python -m tests.scripts.<模块名>`，例如：

```bash
.venv/bin/python -m tests.scripts.browser_smoke
.venv/bin/python -m tests.scripts.browser_platform_smoke
.venv/bin/python -m tests.scripts.browser_memory_settings
.venv/bin/python -m tests.scripts.smoke_ops
.venv/bin/python -m tests.scripts.smoke_ops_agent
.venv/bin/python -m tests.scripts.knowledge_smoke
.venv/bin/python -m tests.scripts.indexed_context_smoke
.venv/bin/python -m tests.scripts.verify_delivery
```

浏览器和闭环脚本可能创建临时账号或保留演示素材；模型、Embedding 和部分知识库验收会调用实际 API，按各脚本说明单独执行。`clear_chat_history` 是显式历史清理工具，不属于默认验收。

原 `scripts/` 下的验收兼容入口已删除；旧 `tiered_context_smoke` 命令由当前的 `tests.scripts.indexed_context_smoke` 替代。启动、部署配置、数据生成与打包仍在 `scripts/`。

## 产物与归档

| 子目录 | 内容 |
| --- | --- |
| `reports/` | 通用验收报告与旧应用停用记录 |
| `screenshots/` | 桌面、平板和手机布局截图 |
| `acceptance-repair/` | 运营闭环、Agent、运营浏览器和交付检查证据 |
| `knowledge-hybrid/` | 知识检索对比与升级证据 |
| `memory-settings/`、`chat-clear/` | 记忆设置和显式聊天清理专项证据 |
| `ops-data/` | 离线数据包检查、JUnit 和剪辑预览 |
| `packages/` | 可重新生成的数据压缩包 |
| `archive/2026-10-23/` | 早期三端、分级上下文和历史修复等旧报告 |

从项目根目录执行 `.venv/bin/python -m scripts.package_ops_data` 可重新校验并生成 `tests/artifacts/packages/douyin-ops-v1.zip`，需要现有 Excel 导出及 FFmpeg。原重复压缩包已清理，原始数据和媒体保留在 `data/`。

本次归档只整理文件与路径，历史测试结果不代表重新运行的验收结果。实际生成的业务成片继续保存在私有 `.runtime/ops-media/`，由后端鉴权访问。
