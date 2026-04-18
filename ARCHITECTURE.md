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

这套 CLI 的**输出格式**和**命令结构**天然适合 Agent 消费：
- 结构化的输出（带颜色标记的表格）
- 原子化的命令（每个命令做一件事）
- 可脚本化的接口（返回码、JSON 输出）

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
│                    CLI 层 (bin/)                         │
│  proxyctl - 主入口，命令解析                              │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│                  工具层 (lib/)                           │
│  status.py  - 状态面板                                   │
│  check.py   - 健康检查                                   │
│  trace.py   - 链路诊断                                   │
│  audit.py   - 配置审计                                   │
│  engine/    - 后端抽象                                   │
└────────────────────┬────────────────────────────────────┘
                     │
┌────────────────────▼────────────────────────────────────┐
│                 后端层 (engine/)                         │
│  MihomoBackend   - Mihomo (Clash Meta) 实现              │
│  SingboxBackend  - Sing-box 实现（预留）                 │
└─────────────────────────────────────────────────────────┘
```

### 后端抽象

```python
# lib/engine/base.py
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

```
proxyctl/
├── bin/
│   └── proxyctl              # 主入口
├── lib/
│   ├── status.py             # 状态面板
│   ├── check.py              # 健康检查 + bench
│   ├── trace.py              # 链路诊断
│   ├── audit.py              # 配置审计
│   └── engine/               # 后端抽象
│       ├── __init__.py
│       ├── base.py           # Backend 接口
│       ├── mihomo.py         # Mihomo 实现
│       └── singbox.py        # Sing-box 实现（预留）
├── scripts/
│   ├── dns-watchdog          # DNS 看门狗
│   └── stuck-snapshot        # 故障现场抓取
├── launchdaemons/
│   ├── com.mihomo.tun.plist
│   ├── com.singbox.tun.plist
│   └── com.proxyctl.dns-lock.plist
├── config/
│   └── config.yaml.example   # 配置模板
├── docs/
│   ├── FEATURES.md           # 功能清单
│   └── INSTALL.md            # 安装指南
├── install.sh                # 安装脚本
├── uninstall.sh              # 卸载脚本
├── README.md                 # 使用文档
└── ARCHITECTURE.md           # 本文件
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

1. 在 `lib/engine/` 下创建新的后端类，继承 `Backend`
2. 实现所有抽象方法
3. 在 `bin/proxyctl` 中注册新后端

### 添加新命令

1. 在 `lib/` 下创建新模块
2. 实现 `cmd_*` 函数
3. 在 `bin/proxyctl` 的 `main()` 中注册命令

### 测试

```bash
# 本地测试
python3 bin/proxyctl status

# 调试模式
export PROXYCTL_DEBUG=1
```

## 版本历史

- **v0.1** (2026-04) - 初始版本
  - Mihomo 后端支持
  - 核心工具：status/check/trace/audit/bench
  - DNS 防线体系
  - 故障现场抓取