# 业务前端

Vue 3 + TypeScript + Vite，仅提供运营 Agent 工作台：经营概览、商品资料、店铺订单、选品研究、文案与报告、运行记录。登录页输入运营账号，默认 `operator`；登录后在侧栏切换授权店铺。源码位于 `src/`，依赖在 `package.json` 和 `package-lock.json` 中维护。

以下命令在项目根目录执行，先按根 README 安装项目内 Node：

```bash
export PATH="$PWD/.runtime/node/bin:$PATH"
npm --prefix frontend ci --no-fund --no-audit
npm --prefix frontend run dev
```

开发地址为 http://localhost:5173，Vite 将 `/api` 请求代理到 http://127.0.0.1:8000。后端需单独启动，登录、普通请求和 SSE 对话均使用此代理。

```bash
npm --prefix frontend run build
```

构建先执行 TypeScript 检查，再生成 `dist/`。根目录 `scripts/dev.sh` 会安装依赖、构建前端并启动本地演示，演示地址为 http://localhost:8000。
