# 业务后端

FastAPI + SQLAlchemy + PostgreSQL。`commerce/` 是 Python 包，`tests/` 是后端集成测试，依赖在本目录 `pyproject.toml` 中声明。与根项目组成 uv 工作区，共用根目录的 `uv.lock` 和 `.venv/`。

仅接受 `operator` 角色登录和访问；旧消费者、商家及测试买家即使持有未过期令牌也不能访问。订单和售后按授权店铺只读，商品上架、消费者售后提交与动作确认接口已移除。历史表和买家外键保留以维持既有数据。

以下命令均在项目根目录执行。首次运行先按根 README 初始化数据库和本地配置：

```bash
uv sync --frozen
uv run uvicorn commerce.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir backend/commerce
```

若演示服务正在占用 8000 端口，先执行 `.venv/bin/python scripts/start.py --stop`。API 文档位于 http://localhost:8000/docs，健康检查位于 `/api/health`。

```bash
uv run pytest -q
```

测试使用独立 PostgreSQL schema，自动创建和清理。`.env`、知识资料、数据文件和 Dify 配置统一位于项目根目录。API 通过 `commerce.config.ROOT` 访问这些共享资源。

开发时前端通过 Vite 代理访问 `/api`；一键演示时，API 同时提供 `frontend/dist/` 中的构建产物。
