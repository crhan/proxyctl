# proxyctl 架构设计

> 这不是一个配置文件仓库，而是一个**配置管理框架**。
>
> 它不告诉你"用什么配置"，而是帮你"管好配置"。

## 设计哲学

### 1. 配置生命周期管理

proxyctl 的核心价值在于提供**配置演进的闭环反馈**：

```
┌─────────────┐
│  配置变更   │  改 rules、调参数
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ proxyctl check │ ←── 验证：分流对吗？连通吗？
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ proxyctl audit │ ←── 发现：有遗漏的域名
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ 自动应用建议 │  或 proxyctl trace 调试
└─────────────┘
```

### 2. Agent 友好设计

这套 CLI 把"自描述"做成了一等公民。Agent 不必读 README、不必猜路径：

- `proxyctl agent-guide`：一份 ≤200 行的 markdown 入门，含能力边界、概念地图、退出码语义、故障决策树、JSON envelope 规范、non-interactive 承诺、footgun
- `proxyctl explain [<topic>]`：回答"我要改 X 去哪？"。Topic 内容由当前 backend 动态计算（路径、端口），不硬编码
- `proxyctl commands --json`：所有命令的元数据（`side_effects` / `needs_sudo` / `interactive` / `exit_codes` / `examples`），Agent 决策必备
- `proxyctl doctor --json`：极简 5 项布尔健康打分（比 status 精简、比 check 快），自动化决策入口
- `proxyctl config path | get`：让 Agent 无需 grep 就能定位/查询自身配置
- 统一 JSON envelope（schema v1）：`schema_version / cmd / ok / data / error / code / hint / doc`
- 分语义退出码：`2 USAGE / 3 NOT_FOUND / 5 ENGINE_DOWN / 6 CONFIG_ERR / 7 NETWORK_ERR / 8 LOCKED`
- `PROXYCTL_AGENT=1` 一键模式：自动 `--json` + 关色 + 非交互

更多 clig.dev 合规细节：TTY 自动检测、`NO_COLOR` 支持、`fcntl.flock` 写操作并发锁、SIGPIPE 安全（不抛 BrokenPipeError）。

### 3. 配置即代码 (Configuration as Code)

```
config.yaml 不是静态文件，而是：
- 有版本控制 (git)
- 有 CI/CD (proxyctl check 当测试)
- 有回滚机制 (config.bak)
- 有变更日志
```

## 系统架构

### 三层架构

```
┌─────────────────────────────────────────────────────────┐
│              CLI 层 (src/proxyctl/cli.py)                │
│  proxyctl - 主入口，命令解析 + dispatcher                  │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│              工具层 (src/proxyctl/*.py)                   │
│  status.py        - 状态面板                              │
│  check.py         - 健康检查 + bench                      │
│  trace.py         - 链路诊断                              │
│  audit.py         - 配置审计                              │
│  subscription.py  - 订阅状态读取（v0.4.4+）                │
│  explain.py       - 速查 + agent-guide 渲染                │
│  _io.py           - envelope / 退出码 / I/O 抽象            │
│  engine/          - 后端抽象                              │
│  core/plugin.py   - 用户插件加载                            │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│              后端层 (src/proxyctl/engine/)                │
│  MihomoBackend   - Mihomo (Clash Meta) 实现 — 首发         │
│  SingboxBackend  - Sing-box 实现（预留，未端到端验证）       │
└─────────────────────────────────────────────────────────┘
```

### 后端抽象

```python
# src/proxyctl/engine/base.py
class Backend(ABC):
    @property
    def label(self) -> str: ...

    @property
    def plist(self) -> str: ...

    @property
    def config_file(self) -> str: ...

    def get_mode(self) -> str: ...
    def check_config(self) -> bool: ...
    def get_api_url(self) -> str: ...
```

### 目录结构

> 以下为真实目录树（PEP 517 / `src/` layout，由 `uv build` 打包）。

```
proxyctl/
├── src/
│   └── proxyctl/                 # Python 包根
│       ├── __init__.py           # __version__（通过 importlib.metadata 动态读 pyproject）
│       ├── cli.py                # 主入口 + dispatcher + Backend 抽象 + lifecycle/dry-run/plan
│       ├── _io.py                # envelope v2 / 退出码 / set_no_color / extract_flags
│       ├── status.py             # 状态面板
│       ├── check.py              # 健康检查 + bench + _collect_fail_hints
│       ├── trace.py              # 链路诊断
│       ├── audit.py              # 配置审计 + apply
│       ├── subscription.py       # 订阅状态读取（v0.4.4+）
│       ├── explain.py            # explain topics + COMMANDS_META + agent-guide markdown
│       ├── completion.py         # bash/zsh/fish 补全脚本生成
│       ├── builtin_plugins/      # 内置 StatusSection / RouteHook 插件
│       ├── core/
│       │   └── plugin.py         # 插件加载 + 注册中心
│       └── engine/               # 后端抽象
│           ├── __init__.py
│           ├── base.py           # Backend 接口
│           ├── mihomo.py         # Mihomo 实现 — 首发
│           └── singbox.py        # Sing-box 实现（预留，未端到端验证）
├── tests/
│   ├── integration/              # 端到端 + contract 测试
│   └── unit/                     # 各模块单元测试
├── launchdaemons/                # macOS plist（launchctl bootstrap）
│   ├── com.mihomo.tun.plist
│   ├── com.singbox.tun.plist
│   └── com.proxyctl.dns-lock.plist
├── systemd/                      # Linux user units（systemctl --user）
│   ├── mihomo.service
│   └── sing-box.service
├── man/proxyctl.1                # man page
├── scripts/                      # 辅助脚本
├── config.yaml.example           # proxyctl 配置模板
├── install.sh                    # 双平台安装脚本（macOS / Linux）
├── uninstall.sh
├── pyproject.toml                # 项目元数据 / build 配置
├── uv.lock                       # 依赖锁
├── README.md                     # 使用文档
├── AGENTS.md                     # 仓库视角的 Agent 协作约定（编辑源码时读）
├── LLMS.md                       # 指向 AGENTS.md 与 proxyctl agent-guide 的 stub
├── MIGRATION-0.3.md              # 0.2.x → 0.3.0 破坏点清单与迁移指南
├── CHANGELOG.md                  # 版本历史（Keep a Changelog 格式）
└── ARCHITECTURE.md               # 本文件
```

## 核心工具

### proxyctl status
系统状态面板：
- 引擎状态、端口监听
- TUN 接口、DNS 状态
- 系统代理、网络环境

### proxyctl check
全面健康检查（4 阶段）：
1. 基础状态（daemon、端口）
2. 代理组状态（节点延迟、存活率）
3. 连通性测试（google/github/国内网站）
4. 出口 IP 验证（分流是否正确）

### proxyctl trace
域名链路诊断：
- DNS 解析（fakeip/realip）
- 规则匹配预测
- 连通性测试
- 实际连接验证

### proxyctl audit
配置审计：
- 扫描日志找"走代理但实际是国内 IP"的域名
- 建议添加到直连规则
- 可自动应用优化建议

### proxyctl bench
代理组测速：
- 并发测速所有节点
- 实时进度条
- 结果展示

## DNS 防线体系

系统 DNS 必须指向 127.0.0.1（proxyctl DNS listener），否则 fakeip 不生效。
三类威胁会覆盖 DNS，对应三层防线：

| 威胁 | 防线 | 触发方式 |
|---|---|---|
| DHCP 续租/Wi-Fi 切换 | networksetup → 127.0.0.1 | proxyctl start/fix |
| AnyConnect VPN 推送 | 劫持 AnyConnect 自己的 DNS 条目 | AnyConnect 钩子 (即时) + dns-lock daemon (30s 兜底) |
| 其他网络事件 | scutil 兜底注入 | dns-lock daemon 轮询 |

### 关键机制

- **networksetup** (层 1): `networksetup -setdnsservers <svc> 127.0.0.1`，对抗 DHCP
- **劫持 AnyConnect DNS 条目** (层 2，核心): 直接修改 `State:/Network/Service/com.cisco.anyconnect/DNS` 的 `ServerAddresses` 为 127.0.0.1
- **scutil 兜底注入** (层 3): `State:/Network/Service/proxyctl-dns-override/DNS`，`SupplementalMatchOrder: 0`
- **dns-lock daemon**: `StartInterval: 30` 轮询，三层修复全做

## 配置管理

### 配置文件位置

| 文件 | 路径 |
|---|---|
| proxyctl 配置 | `~/.config/proxyctl/config.yaml` |
| Mihomo 配置 | `~/.config/mihomo/config.yaml` |
| Sing-box 配置 | `~/.config/sing-box/config.json` |

### 配置示例

```yaml
# ~/.config/proxyctl/config.yaml
backend: mihomo
api_base: http://127.0.0.1:9090
api_secret: your-clash-api-secret
config_dir: /Users/yourname/.config
dns_lock_label: com.proxyctl.dns-lock
```

## 开发指南

### 添加新后端

1. 在 `src/proxyctl/engine/` 下创建新的后端类，继承 `Backend`（见 `engine/base.py`）
2. 实现所有抽象方法（`label / plist / config_file / cache_file / log_file / api_url / get_mode`）
3. 在 `src/proxyctl/cli.py` 的 `get_backend()` 中加入分支注册
4. 注：cli.py 内目前还有一份历史遗留的 `MihomoBackend / SingboxBackend` 定义（双胞胎），合并是已知 backlog

### 添加新命令

1. 在 `src/proxyctl/` 下扩展或新建模块（小命令直接加在 cli.py，大命令拆模块如 status.py / check.py）
2. 实现 `cmd_*` 函数
3. 在 `src/proxyctl/cli.py` 的 dispatcher 表（`_DISPATCH`）中注册 handler
4. 在 `src/proxyctl/explain.py` 的 `COMMANDS_META` 中加 metadata（summary / args / exit_codes / examples / side_effects / needs_sudo / supports_dry_run / supports_json）
5. 如有新能力探针，在 `cli.py` 的 `supported_features` 字典中加 flag = True

### 测试

```bash
# 跑全量
uv run pytest -q

# 单独跑某模块
uv run pytest tests/unit/test_check.py -xvs

# 用本地源码版本跑命令（不需 install）
uv run proxyctl status

# 调试模式（启用更多日志）
export PROXYCTL_DEBUG=1
```

## 版本历史

- **v0.1** (2026-04) - 初始版本
  - Mihomo 后端支持
  - 核心工具：status/check/trace/audit/bench
  - DNS 防线体系
  - 故障现场抓取