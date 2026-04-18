# proxyctl

**Proxy configuration lifecycle management** — 不是静态配置，而是配置演进框架。

## 定位

proxyctl 是一套 macOS 代理管理工具，核心价值在于提供**配置生命周期管理**：

```
配置变更 → 验证 (check) → 调试 (trace) → 优化 (audit) → 回滚
```

它不告诉你"用什么配置"，而是帮你"管好配置"。

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

### 快速安装

```bash
# 1. 克隆仓库
git clone https://github.com/crhan/proxyctl.git
cd proxyctl

# 2. 运行安装脚本
./install.sh

# 3. 配置 API
nano ~/.config/proxyctl/config.yaml
# 填入 api_secret: your-clash-api-secret

# 4. 验证
proxyctl --help
proxyctl status
```

### 手动安装

```bash
# 1. 克隆仓库
git clone https://github.com/crhan/proxyctl.git
cd proxyctl

# 2. 复制文件
cp bin/proxyctl ~/.local/bin/
chmod +x ~/.local/bin/proxyctl

# 3. 配置
mkdir -p ~/.config/proxyctl
cp config.yaml.example ~/.config/proxyctl/config.yaml
# 编辑 config.yaml，填入 api_secret
```

### 安装后端

```bash
# Mihomo 后端（首发支持）
brew install mihomo

# 或者 Sing-box 后端（预留）
brew install sing-box
```

详细安装指南请参考 [docs/INSTALL.md](docs/INSTALL.md)

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

## License

MIT

## 致谢

- [Mihomo](https://github.com/MetaCubeX/mihomo) - Clash Meta 内核
- [Sing-box](https://github.com/SagerNet/sing-box) - 下一代代理内核