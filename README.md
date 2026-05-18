# proxyctl

[![PyPI](https://img.shields.io/pypi/v/proxyctl.svg)](https://pypi.org/project/proxyctl/)
[![CI](https://github.com/crhan/proxyctl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/crhan/proxyctl/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/proxyctl.svg)](https://pypi.org/project/proxyctl/)
[![License](https://img.shields.io/pypi/l/proxyctl.svg)](https://github.com/crhan/proxyctl/blob/main/LICENSE)

**Proxy configuration lifecycle management** — 不是静态配置，而是配置演进框架。

## 定位

proxyctl 是一套 macOS 代理管理工具，核心价值在于提供**配置生命周期管理**：

```
配置变更 → 验证 (check) → 调试 (trace) → 优化 (audit) → 回滚
```

它不告诉你"用什么配置"，而是帮你"管好配置"。

## For AI Agents

proxyctl 把 agent 友好度做成一等公民。完整接入协议见
[AGENTS.md](AGENTS.md)（仓库视角）与 `proxyctl agent-guide`（运行时视角）。

```bash
proxyctl agent-guide                 # Agent 入门 markdown（动态注入路径/端口）
proxyctl --version --json            # schema_version + supported_features 探测
proxyctl commands --json             # 全部命令元数据（机读）
proxyctl commands --schema           # 上面 JSON 的 JSON Schema
proxyctl explain                     # "我想改 X 去哪？" 速查
proxyctl doctor --json               # 5 项健康打分
PROXYCTL_AGENT=1 proxyctl <cmd>      # 一键 --json + 关色 + 非交互
```

- envelope schema v2：`schema_version / cmd / ok / data / error / code / hints[] / warnings[] / doc / meta{{ts,elapsed_ms,proxyctl_version,request_id}}`
- 退出码分语义：`0 OK / 2 USAGE / 3 NOT_FOUND / 4 PERMISSION / 5 ENGINE_DOWN / 6 CONFIG_ERR / 7 NETWORK_ERR / 8 LOCKED / 9 TIMEOUT / 10 DEPENDENCY_MISSING`
- 写命令支持 `--dry-run` 输出结构化 plan（`data.plan = [PlanStep, ...]`）；自 0.4.0 起
  `plan.target` 全部真实化，`action=="subprocess"` 的 target.split() 可直接当 argv 复读；
  自 0.4.2 起 `start / stop / restart / restart-clean / recover` 也加入 `--dry-run` 行列
  （之前是文档承诺但实际真跑，已修复）：
  ```bash
  proxyctl dns-unlock --dry-run --json | jq -r '.data.plan[] | select(.action=="subprocess").target'
  # → launchctl bootout system/com.proxyctl.dns-lock
  # → rm -f /Library/LaunchDaemons/com.proxyctl.dns-lock.plist

  proxyctl stop --dry-run --json | jq -r '.data.plan[] | select(.action=="subprocess").target'
  # macOS → launchctl bootout system/com.proxyctl.dns-lock
  #       → launchctl bootout system/com.mihomo.tun
  # Linux → systemctl --user stop mihomo.service
  ```
  PlanStep.action 枚举：`subprocess / system_op / fs_write / fs_copy / fs_write_atomic / fs_remove / edit_yaml / scan_log / http_put`。
  CI 层 contract test（`tests/integration/test_plan_exec_contract.py`）保证 plan ↔ exec 永不漂移。
- `audit/check` 支持 `--plain` TSV
- `proxyctl help <cmd>` 与 `<cmd> --help` 同源；错误带可执行 hints + explain topic
- 非 TTY 自动关色；不读 stdin / 不 prompt；写操作 fcntl.flock 互斥
- 从 0.2.x 升级见 [MIGRATION-0.3.md](MIGRATION-0.3.md)

## 核心功能

### 状态面板
```bash
proxyctl status
```
- 引擎状态、端口监听、TUN 接口
- DNS 状态、系统代理、网络环境
- Tailscale 内网连通性

### 健康检查
```bash
proxyctl check
```
四阶段检查：
1. 基础状态（daemon、端口）
2. 代理组状态（节点延迟、存活率）
3. 连通性测试（google/github/国内网站）
4. 出口 IP 验证（分流是否正确）

### 链路诊断
```bash
proxyctl trace example.com
```
- DNS 解析（fakeip/realip）
- 规则匹配预测
- 连通性测试
- 实际连接验证

### 配置审计
```bash
proxyctl audit 7          # 扫描最近 7 天日志
proxyctl audit apply      # 自动应用优化建议
```
找出"走代理但实际是国内 IP"的域名，建议添加到直连规则。

### 节点测速
```bash
proxyctl bench                    # 测所有组
proxyctl bench proxy claude       # 测指定组
```

## 安装

### 从 PyPI 安装（推荐）

```bash
# 用 uv（推荐，单命令隔离环境）
uv tool install proxyctl

# 或者 pipx
pipx install proxyctl

# 或者 pip（不推荐全局污染）
pip install --user proxyctl

# 验证
proxyctl --version          # proxyctl v0.1.0
proxyctl --help
```

### 安装后端

```bash
# Mihomo 后端（首发支持）
brew install mihomo

# 或者 Sing-box 后端（预留）
brew install sing-box
```

### 配置 API

```bash
mkdir -p ~/.config/proxyctl
# 把 https://github.com/crhan/proxyctl/blob/main/config.yaml.example 拷过来
curl -fsSL https://raw.githubusercontent.com/crhan/proxyctl/main/config.yaml.example \
  -o ~/.config/proxyctl/config.yaml
# 编辑 config.yaml，填入 api_secret
```

### 注册 macOS LaunchDaemon（可选）

如果需要把 mihomo/sing-box 作为 launchd 服务托管（开机自启 + 守护进程重启），
克隆仓库并跑 `install.sh`：

```bash
git clone https://github.com/crhan/proxyctl.git
cd proxyctl
./install.sh        # 安装 plist 到 /Library/LaunchDaemons
```

> 仅需要命令行用 `proxyctl status`/`check`/`trace` 等命令，**不必跑 install.sh**。

### 从源码开发

```bash
git clone https://github.com/crhan/proxyctl.git
cd proxyctl
uv sync --group dev          # 装运行 + 测试依赖
uv run pytest                # 跑测试
uv run proxyctl status       # 用本地源代码版本
```

## 配置示例

```yaml
# ~/.config/proxyctl/config.yaml

# 后端选择：mihomo (默认) | singbox
backend: mihomo

# Clash API 配置
api_base: http://127.0.0.1:9090
api_secret: your-clash-api-secret

# 配置目录
config_dir: /Users/yourname/.config

# DNS 看门狗配置
dns_lock_label: com.proxyctl.dns-lock
```

## 命令速查

| 命令 | 功能 |
|---|---|
| `proxyctl start/stop/restart` | 启停后端 |
| `proxyctl status` | 系统状态面板 |
| `proxyctl check` | 全面健康检查 |
| `proxyctl trace <domain>` | 域名链路诊断 |
| `proxyctl audit [days]` | 代理链路审计 |
| `proxyctl bench [groups]` | 代理组测速 |
| `proxyctl fix` | 修复 DNS/代理 |
| `proxyctl recover` | 切网后软恢复 |
| `proxyctl mode tun\|proxy` | 切换模式 |
| `proxyctl dns-lock` | 启动 DNS 看门狗 |

## 架构设计

### 后端抽象
```
Backend (接口)
├── MihomoBackend (首发实现)
└── SingboxBackend (预留)
```

### DNS 防线体系
三层修复对抗 DNS 覆盖：
1. `networksetup` — 对抗 DHCP
2. 劫持 AnyConnect DNS 条目 — 对抗 VPN
3. scutil 兜底注入 — 对抗其他覆盖源

### 配置即代码
- 版本控制 (git)
- CI/CD (`proxyctl check` 当测试)
- 回滚机制 (config.bak)
- 变更日志

## 后端支持

| 功能 | Mihomo | Sing-box |
|---|---|---|
| status | ✅ | ✅ |
| check | ✅ | ✅ |
| trace | ✅ | ✅ |
| audit | ✅ | ✅ |
| bench | ✅ | ⚠️ |
| recover | ✅ | ❌ |
| mode 切换 | ✅ | ✅ |

## 开发

```bash
# 本地测试
python3 bin/proxyctl status

# 调试模式
export PROXYCTL_DEBUG=1
```

## Changelog

版本变更记录见 [CHANGELOG.md](https://github.com/crhan/proxyctl/blob/main/CHANGELOG.md)。

## License

MIT

## 致谢

- [Mihomo](https://github.com/MetaCubeX/mihomo) - Clash Meta 内核
- [Sing-box](https://github.com/SagerNet/sing-box) - 下一代代理内核