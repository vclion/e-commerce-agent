"""Write user-readable local access information without printing credentials."""
import json
from commerce.config import settings
from .dify_console import ROOT


def main():
    admin = json.loads((ROOT / '.runtime/dify-access.json').read_text())
    text = f'''# 本地访问信息（含密码，请保留在本机）

工程目录：`{ROOT}`

## 电商工作台

打开 http://localhost:8000 。

- 仅提供运营入口；账号为 `operator`，兼容 `operator-a`、`operator-b`。消费者与商家账号不可登录。
- 运营默认展示授权店铺汇总；登录后在侧栏筛选店铺。
- `-a` 为山序男装；切换知织女装时将后缀改成 `-b`。
- 演示账号共用密码：`{settings.demo_password}`

## Dify

打开 http://localhost:8088 。

- 管理员邮箱：`{admin['email']}`
- 管理员密码：`{admin['password']}`

1 个运营 Chatflow、6 个 Workflow 工具、DeepSeek 与 Agent 策略插件和30份资料已导入。

## 配置 DeepSeek

1. 登录 Dify，进入设置 → 模型供应商 → DeepSeek。
2. 填入自己的 DeepSeek API Key 并保存。Key 只需留在本地 Dify，无须发到聊天中。
3. 在 WSL 中执行：

```bash
cd {ROOT}
.venv/bin/python -m scripts.activate_dify
```

4. 刷新电商工作台，查看助手连接状态。

这份文件和所有凭据都在被 Git 忽略的 `.runtime/` 中。
'''
    path = ROOT / '.runtime/本地访问信息.md'
    path.write_text(text)
    path.chmod(0o600)
    print('Local access information written to .runtime/本地访问信息.md (credentials not printed).')


if __name__ == '__main__':
    main()

