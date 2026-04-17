# MCP 集成

Coco 支持任意遵循 [Model Context Protocol](https://modelcontextprotocol.io/)（MCP）的 server。
server 暴露的工具会被自动注册为带命名空间前缀 `mcp__<server>__<tool>` 的 Coco 工具，
agent 可以像使用内置工具一样调用它们。

MVP 范围：**stdio 传输 + tools only**（不做 resources / prompts / OAuth / HTTP+SSE）。

## 安装

可选依赖随 Coco 一起装：

```bash
pip install 'coco[mcp]'
```

这会拉入官方 `mcp` Python SDK。不装这个依赖时，Coco 一切正常，只是 MCP 相关命令
提示未启用。

## 配置

两个位置都会被读取（同名 server 项目级覆盖全局）：

- 全局：`~/.config/coco/mcp_servers.toml`
- 项目级：`<workspace>/.coco/mcp_servers.toml`

示例：

```toml
[fs]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/workspace"]

[git]
command = "uvx"
args = ["mcp-server-git"]

[sqlite]
command = "uvx"
args = ["mcp-server-sqlite", "--db-path", "/path/to/db.sqlite"]
env = { LOG_LEVEL = "INFO" }
```

每个表项的字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `command` | str | 必填。启动 server 的可执行文件 |
| `args` | list[str] | 可选。命令行参数 |
| `env` | table | 可选。附加环境变量；会合并到继承的 env 之上 |

## 运行

启动时看到：

```
  加载 2 个 MCP server…
  · MCP [fs] 注册了 7 个工具
  · MCP [git] 注册了 5 个工具
  MCP 就绪：共注册 12 个远端工具
```

单 server 启动失败不影响其他 server 或 Coco 本身，只会打印一行 warning。

## 命令

- `/mcp` — 列出配置过的 server 与当前状态（`running` / `idle` / `failed`）、工具数
- `/doctor` — 末尾会展示 MCP 依赖状态、已配置的 server 列表、当前状态

## 注意事项

- **权限**：MCP 工具默认 `is_read_only=False`，走 Coco 的权限确认流程（与 Write/Edit 同级）
- **懒启动**：server 在首次被 `discover_tools()` 或 `_get_client()` 触及时才启动；
  挂掉的 server 会被标记为 `failed`，同一进程内不会重试
- **退出清理**：Coco 进程退出时 `atexit` 触发 `shutdown_all`，关闭所有 session 与子进程
- **命名空间**：工具名形如 `mcp__fs__read_file`；不会与内置工具冲突
- **async → sync 桥**：所有 MCP SDK 的异步调用在一个后台 `BackgroundLoop` 线程里跑，
  Coco 主循环保持同步

## 不支持

- OAuth 流程
- HTTP+SSE 传输
- resources / prompts（仅 tools）
- 动态 reconnect（挂了就 `failed`）
- 并发 server 启动（顺序启动）
