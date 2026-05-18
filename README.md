# proxyctl

[![PyPI](https://img.shields.io/pypi/v/proxyctl.svg)](https://pypi.org/project/proxyctl/)
[![CI](https://github.com/crhan/proxyctl/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/crhan/proxyctl/actions/workflows/ci.yml)
[![Python](https://img.shields.io/pypi/pyversions/proxyctl.svg)](https://pypi.org/project/proxyctl/)
[![License](https://img.shields.io/pypi/l/proxyctl.svg)](https://github.com/crhan/proxyctl/blob/main/LICENSE)

**代理配置的生命周期管理 — 为 AI Agent 打造。**

macOS + Linux，单 CLI 托管 **mihomo** 代理内核的完整生命周期：
启停 · 健康检查 · 链路诊断 · 日志驱动的配置审计 · 切网软恢复。
（sing-box 后端骨架已搭，未端到端验证 — 详见下方矩阵）
所有命令支持 `--json` envelope、`--dry-run` 真 plan、语义化退出码、
错误带可执行 hints —— **agent 可端到端编程**，不是给人看完再手工敲键盘。

---

## 30 秒：让 agent 用 proxyctl 接管你的代理

复制这段 prompt，喂给 Claude / Cursor / 任何 LLM agent（要求它能跑 shell）：

```text
帮我用 proxyctl 接管本机代理：
1. 跑 `proxyctl agent-guide` 拿协议；扫现有系统代理 + 引擎配置
2. 用 `--dry-run --json` 生成等价接管方案，先给我看 plan 再落地
3. 跑 `proxyctl check --json` + `proxyctl audit 7 --json`，
   给我 3 条最值得动的优化（按收益/风险排序）
```

agent 会：自描述协议 → 读取本机现状 → 用 dry-run 演示要做什么 →
落地后审计日志找出"该走直连却在过代理"的域名、被错误代理的内网请求、
被忽略的分流规则漏洞，最后给排好序的改进列表。

<details>
<summary><b>更详细的版本</b>（让 agent 把每一步讲透、出报告）</summary>

```text
帮我用 proxyctl 系统化接管本机代理。按这个流程：

1. 自描述阶段
   - 跑 `proxyctl agent-guide` 拿 envelope schema / 退出码 / 决策树
   - 跑 `PROXYCTL_AGENT=1 proxyctl --version` 读 supported_features
   - 一切后续命令带 `--json`，错误读 `hints[]` 路由下一步

2. 现状普查
   - `PROXYCTL_AGENT=1 proxyctl status`（拿引擎 + 系统代理 + DNS 一站式）
   - `networksetup -getwebproxy` 等系统命令交叉印证
   - 看 `~/.config/{mihomo,clash}/config.yaml`、`env | grep -i proxy`
   - 用一段话告诉我：现在谁在代理我、怎么代理、为什么

3. 接管方案
   - 生成等价的 proxyctl 接管配置（含 backend / api / dns_lock 等）
   - 跑 `proxyctl start --dry-run --json`（v0.4.2+ 真实化 plan，每步 argv 可读）
   - 把 plan 列给我审，OK 后真落地

4. 健康打底
   - `proxyctl check --json` —— 任一 stage 失败时 envelope.hints
     已聚合真凶摘要（v0.4.3+），不必挖 stages.*.ok
   - `proxyctl doctor --json` —— 5 项快速打分
   - `proxyctl audit 7 --json` —— 扫近 7 天访问日志，找走代理但落地国内的域名

5. 给我可执行的改进列表
   - 最多 3 条，按 (收益 × 把握) / 风险 排序
   - 每条给出：现状、建议改动、可复读的 proxyctl 命令、回滚方法
   - 引用 `proxyctl explain <topic>` 让我能自查
```

</details>

---

## For AI Agents — 凭什么 "for agent"

proxyctl 把 agent 友好度做成一等公民。完整接入协议见
[AGENTS.md](AGENTS.md)（仓库视角）与 `proxyctl agent-guide`（运行时视角）。

```bash
proxyctl agent-guide                 # Agent 入门 markdown（注入当前路径/端口）
proxyctl agent-guide --list-sections # 15 个段，按需取小块（省 token）
proxyctl --version --json            # schema_version + supported_features 探测
proxyctl commands --json             # 全部命令元数据（机读）
proxyctl commands --schema           # 上面 JSON 的 JSON Schema
proxyctl explain                     # "我想改 X 去哪？" 速查
proxyctl doctor --json               # 5 项健康打分 + healthy 布尔字段
PROXYCTL_AGENT=1 proxyctl <cmd>      # 一键 --json + 关色 + 非交互
```

**硬核能力**：

- **envelope schema v2**：`schema_version / cmd / ok / data / error / code / hints[] / warnings[] / doc / meta{ts,elapsed_ms,proxyctl_version,request_id}` ——
  失败时 `hints[]` 聚合真凶摘要（v0.4.3+），不必挖 stages.*.ok
- **退出码分语义**：`0 OK / 2 USAGE / 3 NOT_FOUND / 4 PERMISSION / 5 ENGINE_DOWN / 6 CONFIG_ERR / 7 NETWORK_ERR / 8 LOCKED / 9 TIMEOUT / 10 DEPENDENCY_MISSING`
- **`--dry-run` 真 plan**：写命令输出 `data.plan = [PlanStep, ...]`，
  自 0.4.0 起 `plan.target` 全部真实化（`subprocess` action 的 target.split()
  可直接当 argv 复读），自 0.4.2 起 `start / stop / restart / restart-clean / recover`
  也加入 `--dry-run` 行列。PlanStep.action 枚举：
  `subprocess / system_op / fs_write / fs_copy / fs_write_atomic / fs_remove / edit_yaml / scan_log / http_put`
- **CI 层 contract test**（`tests/integration/test_plan_exec_contract.py`）
  保证 plan ↔ exec **永不漂移**
- **错误带可执行 hints + explain topic**：agent 可路由下一步而不需要规则匹配
- **`audit/check` 支持 `--plain` TSV**：4 行 stage/ok/detail，agent 友好的备选
- **`proxyctl help <cmd>` 与 `<cmd> --help` 同源**
- **非 TTY 自动关色 / 不读 stdin / 不 prompt / 写操作 fcntl.flock 互斥**

示例：dry-run 拿 argv：

```bash
proxyctl stop --dry-run --json | jq -r '.data.plan[] | select(.action=="subprocess").target'
# macOS → launchctl bootout system/com.proxyctl.dns-lock
#       → launchctl bootout system/com.mihomo.tun
# Linux → systemctl --user stop mihomo.service
```

---

## 核心能力（按使用频次）

### `status` —— 一站式系统面板
```bash
proxyctl status                   # 引擎 / 端口 / TUN / DNS / 系统代理 / 网络环境
proxyctl status --json            # 同上，envelope 输出
```
插件扩展点（StatusSection）可加企业 VPN / Tailscale / TUIC relay 等业务面板。

### `check` —— 4 阶段健康检查
```bash
proxyctl check                    # 基础 → 代理组 → 连通性 → 出口 IP
proxyctl check --json             # 失败时 hints[] 聚合真凶摘要
proxyctl check --plain            # 4 行 TSV
```

### `trace` —— 域名链路诊断
```bash
proxyctl trace github.com         # DNS / 规则匹配 / 连通性 / 实际连接
proxyctl trace github.com --json
```

### `audit` —— 日志驱动的配置优化
```bash
proxyctl audit 7                  # 扫最近 7 天日志，找"走代理但落地国内"的域名
proxyctl audit apply --dry-run    # 生成 rules 补丁 plan，审阅后再 apply
```

### `bench` —— 节点测速（NDJSON 流式 + summary envelope）
```bash
proxyctl bench                    # 测全部 URLTest / Fallback / LoadBalance 组
proxyctl bench proxy claude       # 测指定组
```

### `recover` —— 切网后软恢复
```bash
proxyctl recover                  # 不重启引擎，热重载 + 清 fakeip + 刷代理组延迟
proxyctl recover --dry-run        # 看 3 个 Clash API endpoint 再决定
```

---

## 安装

### 方式 1：PyPI（推荐）

```bash
uv tool install proxyctl                # uv（推荐）
pipx install proxyctl                   # 或 pipx
pip install --user proxyctl             # 或 pip

proxyctl --version                      # → proxyctl v0.4.3
proxyctl --help
```

### 方式 2：后端引擎

```bash
brew install mihomo                     # 首发后端，macOS / Linux 都通
# Linux 也可用各发行版包管理器（apt / dnf / pacman）
# sing-box 后端见末尾"平台支持矩阵"，目前未端到端验证
```

### 方式 3：配置 API

```bash
mkdir -p ~/.config/proxyctl
curl -fsSL https://raw.githubusercontent.com/crhan/proxyctl/main/config.yaml.example \
  -o ~/.config/proxyctl/config.yaml
# 编辑 ~/.config/proxyctl/config.yaml，填 api_secret 等
```

### 方式 4：系统服务托管（可选）

需要把 mihomo（或预留中的 sing-box）作为系统服务托管（开机自启 + 守护进程重启）：

```bash
git clone https://github.com/crhan/proxyctl.git
cd proxyctl
./install.sh        # 自动识别 macOS（LaunchDaemon）/ Linux（systemd user unit）
```

> 只想跑 `proxyctl status / check / trace` 等只读命令的话，**不必跑 install.sh**。

---

## 配置示例

```yaml
# ~/.config/proxyctl/config.yaml

backend: mihomo                         # mihomo (默认) | singbox（预留，未端到端验证）

api_base: http://127.0.0.1:9090         # Clash API
api_secret: your-clash-api-secret

config_dir: /Users/yourname/.config

dns_lock_label: com.proxyctl.dns-lock   # DNS 看门狗 launchd label

proxy_port: 7890                        # 自 0.1.4 起可配
no_proxy_extra:                         # 自 0.1.5 起追加 NO_PROXY
  - "*.internal.example.com"
  - 10.0.0.0/8
```

完整字段见 [config.yaml.example](config.yaml.example)。

---

## 命令速查

| 命令 | 功能 | dry-run | json |
|---|---|:---:|:---:|
| `start / stop / restart / restart-clean` | 启停引擎 + DNS/代理注入 | ✅ | ✅ |
| `status` | 一站式系统面板 | — | ✅ |
| `check` | 4 阶段健康检查 | — | ✅ |
| `doctor` | 5 项健康打分 | — | ✅ |
| `trace <domain>` | 域名链路诊断 | — | ✅ |
| `audit [days] [apply]` | 日志驱动配置优化 | ✅ | ✅ |
| `bench [groups]` | 节点测速 (NDJSON) | — | ✅ |
| `fix` | 修复 DNS / 代理 / 热重载 | ✅ | ✅ |
| `recover` | 切网后软恢复 | ✅ | ✅ |
| `mode tun\|proxy` | 切换 TUN / 代理模式 | ✅ | ✅ |
| `dns-lock / dns-unlock` | 启停 DNS 看门狗 | ✅ | ✅ |
| `daemon` | 管理 extra_daemons | ✅ | ✅ |
| `agent-guide / commands / explain` | agent 自描述三件套 | — | ✅ |

---

## 平台支持矩阵（Mihomo 后端）

| 命令 | macOS | Linux |
|---|:---:|:---:|
| start / stop / restart / restart-clean | ✅ launchd | ✅ systemd user unit |
| status / check / doctor / trace / bench | ✅ | ✅ |
| audit (含 apply) | ✅ | ✅ |
| recover (切网软恢复) | ✅ | ✅ |
| mode tun \| proxy 切换 | ✅ | ✅ |
| dns-lock 看门狗 | ✅ | N/A* |

\* Linux 下系统 DNS 由 systemd-resolved / NetworkManager / resolvconf
管理，机制差异大，看门狗目前 macOS-only。Linux 用户用引擎自身的 fakeip
或 `nameserver-policy` 即可达成类似效果。

### Sing-box 后端 — 预留 / 未端到端验证

`SingboxBackend`、launchd plist、systemd unit、`audit` 的
sing-box JSON config 解析、`trace` 的 sing-box 日志 grep、`engine` /
`mode` 切换命令都已实现，单测覆盖路径/配置/API URL 解析。**但没人在
生产里跑过完整的 start → check → audit → recover 闭环**——
`config.yaml.example` 自己写着"首发支持 mihomo，singbox 后端预留中"。

想用 sing-box 的话：基础启停 / status / audit 大概率能跑，
`bench` 和 `recover` 依赖 Clash API 兼容子集，行为可能不齐；遇到 bug
欢迎提 issue 或 PR。

---

## 深入阅读

| 文档 | 受众 / 内容 |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构哲学、DNS 三层防线、配置生命周期闭环、后端抽象 |
| [AGENTS.md](AGENTS.md) | 仓库内 coding agent 工作协议（贡献者向） |
| `proxyctl agent-guide` | 运行时 agent 入门 markdown（动态注入当前路径/端口） |
| [MIGRATION-0.3.md](MIGRATION-0.3.md) | 0.2.x → 0.3.x envelope schema v2 迁移 |
| [CHANGELOG.md](CHANGELOG.md) | 版本历史 |

---

## 开发

```bash
git clone https://github.com/crhan/proxyctl.git
cd proxyctl
uv sync --group dev                    # 装运行 + 测试依赖
uv run pytest                          # 跑 523 个测试
uv run proxyctl status                 # 用本地源码版本

export PROXYCTL_DEBUG=1                # 调试模式
```

---

## License

MIT — 见 [LICENSE](LICENSE)。

## 致谢

- [Mihomo](https://github.com/MetaCubeX/mihomo) —— Clash Meta 内核
- [Sing-box](https://github.com/SagerNet/sing-box) —— 下一代代理内核
