# WeChat Local MCP

在 Windows 上通过 MCP 查找微信会话，按条数和时间范围读取指定私聊或群聊。本机实测版本为微信 4.1.15.13。读取范围取决于电脑上保存的记录，最新消息可能要等微信写入主数据库。公众号链接和全文提取尚未接入。

## 配置

1. 安装 Python 3 和依赖：`python -m pip install -r requirements.txt`。
2. 将 `WECHAT_CAPTURE_FILE` 指向本机 WeFlow 成功捕获的 JSON 文件，其结构需包含 `success: true` 和 `data.decrypt_key`。该文件是密钥，请放在仓库外。
3. 如果有多个账号或微信资料不在 Windows 文档的默认位置，将 `WECHAT_DB_STORAGE` 指向目标账号的 `db_storage` 文件夹。
4. 在支持本地 stdio MCP 的客户端中添加服务器。命令填 `python`，参数填 `-B`、`-u` 和 `wechat-local/server.py` 的绝对路径，并为服务器进程配置上述环境变量。

Codex 也可以将本仓库目录添加为本地插件市场，再安装 `wechat-local@wechat-private`。启动 Codex 的环境需要能找到 Python 和上述变量。

- `find_conversation` 按名称或 ID 找联系人或群聊。
- `read_conversation` 按会话 ID 读取消息，单次最多 100 条。文件传输助手的 ID 为 `filehelper`。

服务器只读原数据库，并在本机临时目录处理解密副本。工具返回的消息会进入调用它的 AI 客户端。请按需指定会话和时间范围。仓库不包含密钥、消息、微信数据库或 WeFlow 可执行文件。
