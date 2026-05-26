# NetEase Mail Codex Plugin

NetEase Mail Codex Plugin 是一个本地 Codex 插件，用 IMAP 和 SMTP 连接网易邮箱账号，包括 `163.com`、`126.com` 和 `yeah.net`。

它不自动化网易邮箱大师桌面客户端，而是通过标准邮件协议工作：IMAP 用于读取和搜索邮件，SMTP 用于发送邮件。

## 功能

- 检查网易邮箱配置状态，不暴露授权码。
- 列出 IMAP 邮箱文件夹。
- 搜索近期邮件，支持按发件人、主题和文本关键词过滤。
- 读取单封邮件的正文、收发件人、日期和附件摘要。
- 准备邮件草稿，但不保存到邮箱。
- 通过 SMTP 发送邮件，默认关闭，需要显式启用。

当前版本不会删除邮件、移动邮件、标记已读或修改邮箱状态。

## 目录结构

```text
.
├── .agents/
│   └── plugins/
│       └── marketplace.json
├── plugins/
│   └── netease-mail/
│       ├── .codex-plugin/
│       │   └── plugin.json
│       ├── .mcp.json
│       ├── scripts/
│       │   └── netease_mail_mcp.py
│       └── skills/
│           └── netease-mail/
│               └── SKILL.md
├── .gitignore
└── README.md
```

核心文件说明：

- `plugins/netease-mail/.codex-plugin/plugin.json`：Codex 插件 manifest。
- `plugins/netease-mail/.mcp.json`：MCP server 启动配置。
- `plugins/netease-mail/scripts/netease_mail_mcp.py`：IMAP/SMTP 插件实现。
- `plugins/netease-mail/skills/netease-mail/SKILL.md`：给 Codex 的使用说明。
- `.agents/plugins/marketplace.json`：本仓库作为本地 marketplace 时的入口。

## 安装方式一：从 GitHub 仓库使用

假设你把仓库克隆到：

```text
D:\codex-plugins\netease-mail
```

在 `C:\Users\<你的用户名>\.codex\config.toml` 中增加：

```toml
[marketplaces.netease-mail]
last_updated = "2026-05-26T00:00:00Z"
source_type = "local"
source = '\\?\D:\codex-plugins\netease-mail'

[plugins."netease-mail@netease-mail"]
enabled = true
```

其中：

- `[marketplaces.netease-mail]` 里的 `netease-mail` 是你给这个本地 marketplace 起的名字。
- `source` 指向仓库根目录，也就是包含 `.agents/plugins/marketplace.json` 的目录。
- `[plugins."netease-mail@netease-mail"]` 前半段是插件名，后半段是 marketplace 名。

修改后，完全退出并重启 Codex 桌面端。

## 安装方式二：安装到个人插件目录

也可以把插件复制到个人插件目录：

```text
C:\Users\<你的用户名>\plugins\netease-mail
```

然后在 `C:\Users\<你的用户名>\.agents\plugins\marketplace.json` 中加入：

```json
{
  "name": "netease-mail",
  "source": {
    "source": "local",
    "path": "./plugins/netease-mail"
  },
  "policy": {
    "installation": "AVAILABLE",
    "authentication": "ON_INSTALL"
  },
  "category": "Productivity"
}
```

再在 `C:\Users\<你的用户名>\.codex\config.toml` 中加入：

```toml
[marketplaces.personal]
last_updated = "2026-05-26T00:00:00Z"
source_type = "local"
source = '\\?\C:\Users\<你的用户名>'

[plugins."netease-mail@personal"]
enabled = true
```

修改后，完全退出并重启 Codex 桌面端。

## MCP 路径配置

仓库中的 `plugins/netease-mail/.mcp.json` 默认使用相对路径：

```json
{
  "mcpServers": {
    "netease-mail": {
      "command": "python",
      "args": [
        "./scripts/netease_mail_mcp.py"
      ]
    }
  }
}
```

如果你的 Codex 版本不能从相对路径启动 MCP server，请把 `args` 改成脚本的绝对路径。例如：

```json
{
  "mcpServers": {
    "netease-mail": {
      "command": "python",
      "args": [
        "D:\\codex-plugins\\netease-mail\\plugins\\netease-mail\\scripts\\netease_mail_mcp.py"
      ]
    }
  }
}
```

Windows 路径中的反斜杠需要写成 `\\`，因为 `.mcp.json` 是 JSON 文件。

## 邮箱配置

插件通过环境变量读取邮箱配置。不要把授权码写进 GitHub 仓库。

必需配置：

```powershell
setx NETEASE_MAIL_ADDRESS "<your-netease-mail-address>"
setx NETEASE_MAIL_AUTH_CODE "your-client-authorization-code"
```

`NETEASE_MAIL_AUTH_CODE` 应该使用网易邮箱的客户端授权码，不要使用网页登录密码。

可选配置：

```powershell
setx NETEASE_MAIL_IMAP_HOST "imap.163.com"
setx NETEASE_MAIL_IMAP_PORT "993"
setx NETEASE_MAIL_SMTP_HOST "smtp.163.com"
setx NETEASE_MAIL_SMTP_PORT "465"
```

发送邮件默认关闭。确认需要发信时再启用：

```powershell
setx NETEASE_MAIL_ENABLE_SMTP "true"
```

如果 SMTP 授权码和 IMAP 授权码不同，可以单独配置：

```powershell
setx NETEASE_MAIL_SMTP_PASSWORD "your-smtp-authorization-code"
```

使用 `setx` 后，需要重启 Codex 桌面端或重新打开终端，新的环境变量才会生效。

## 使用示例

在 Codex 中可以这样说：

```text
检查我的 163 邮箱配置状态
```

```text
列出我的 163 邮箱文件夹
```

```text
搜索我最近 7 天的 163 邮件
```

```text
用我的 163 邮箱给某个收件人发一封问候邮件
```

发送邮件时，插件仍会要求工具调用携带 `confirm_send: true`，用于防止误发。

## 本地验证

可以先直接运行 MCP 的配置检查工具，确认插件脚本和环境变量是否正常：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_config_status","arguments":{}}}' | python .\plugins\netease-mail\scripts\netease_mail_mcp.py
```

如果配置正常，会看到类似字段：

```json
{
  "address_configured": true,
  "imap_password_configured": true,
  "imap_ready": true,
  "smtp_enabled": false
}
```

`smtp_enabled` 为 `false` 只表示发信没有开启，不影响读信。


## 安全说明

- 插件不会返回或打印邮箱授权码。
- 默认只启用 IMAP 读取能力。
- SMTP 发信需要 `NETEASE_MAIL_ENABLE_SMTP=true`。
- 发信工具还要求显式确认参数 `confirm_send: true`。
- 当前版本不提供删除、移动或标记邮件的工具。
