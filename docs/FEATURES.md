# proxyctl 功能清单

## 核心功能（v0.1）

### 1. 状态面板 (`proxyctl status`)

**用途：** 一眼看懂系统代理状态

**输出内容：**
- 引擎状态（daemon PID、运行时长）
- 端口监听（DNS 53、代理 7890、API 9090）
- TUN 接口（iface、IP、MTU）
- DNS 状态（系统 DNS、lock daemon）
- 网络环境（en0 IP、内网状态、Tailscale）
- 系统代理设置

**使用场景：**
- 每天早上检查代理是否正常
- 切网后快速确认状态
- 排查问题前的基线检查

---

### 2. 健康检查 (`proxyctl check`)

**用途：** 全面验证代理系统健康度

**四阶段检查：**
1. **基础状态** - daemon、端口、DNS、内网连通性
2. **代理组** - 节点延迟、存活率、最近测试时间
3. **连通性** - Google/GitHub/Discord/国内网站
4. **出口 IP** - proxy/claude/direct 三路 IP 对比

**使用场景：**
- 订阅更新后验证
- 切网后确认恢复
- 配置变更后的回归测试

---

### 3. 链路诊断 (`proxyctl trace <domain>`)

**用途：** 诊断域名访问链路，回答"为什么这个域名走了这个出口"

**四阶段诊断：**
1. **DNS 解析** - A 记录、CNAME、fakeip/realip 标签
2. **规则匹配** - 逐条匹配规则，显示命中原因
3. **连通性** - 实际 HTTP 请求测试
4. **实际连接** - 从 Clash API 读取真实路由

**使用场景：**
- 某个网站访问失败
- 验证规则是否生效
- 调试分流异常

---

### 4. 配置审计 (`proxyctl audit [days]`)

**用途：** 发现"走代理但实际是国内 IP"的域名，优化直连规则

**工作流程：**
1. 扫描代理日志（最近 N 天）
2. 反查域名真实 IP（DoH）
3. 判断是否国内 IP
4. 建议添加到直连规则
5. 可选：自动应用建议

**使用场景：**
- 每月例行优化
- 发现漏网的国内域名
- 减少不必要的代理开销

---

### 5. 节点测速 (`proxyctl bench [groups...]`)

**用途：** 对代理组全部节点并发测速

**功能：**
- 并发测速（默认 16 线程）
- 实时进度条
- 结果展示（节点延迟、存活率）

**使用场景：**
- 切网后刷新节点状态
- 定期清理失效节点
- 选择最佳节点

---

### 6. DNS 看门狗 (`proxyctl dns-lock`)

**用途：** 防止系统 DNS 被覆盖（DHCP/VPN/其他）

**三层防线：**
1. networksetup - 对抗 DHCP
2. 劫持 AnyConnect DNS 条目 - 对抗 VPN
3. scutil 兜底注入 - 对抗其他覆盖源

**使用场景：**
- VPN 用户防止 DNS 被覆盖
- Wi-Fi 切换频繁的环境
- 需要稳定 DNS 的场景

---

### 7. 软恢复 (`proxyctl recover`)

**用途：** 切网后快速恢复，不重启进程

**操作：**
1. 热重载配置（清 DNS 缓存）
2. Flush fakeip cache
3. 触发所有代理组 healthcheck

**使用场景：**
- 公司↔家切换后
- Wi-Fi 漫游后
- 代理组全挂时的第一尝试

---

### 8. 故障现场抓取 (`proxyctl-stuck-snapshot`)

**用途：** proxy 组全挂时的现场数据抓取

**抓取内容：**
- 当前网络环境
- proxyctl check 完整输出
- Mihomo 内部 DNS 解析
- 外部 DNS 对比（路由器/公网）
- 代理组状态
- TCP 连通性
- 活跃连接数
- Mihomo TCP socket 列表
- 最近日志

**使用场景：**
- 切网后 proxy 组全挂
- 需要排查根因时
- 提交 issue 时附带数据

---

## 基础命令

| 命令 | 功能 |
|---|---|
| `proxyctl start` | 启动后端 |
| `proxyctl stop` | 停止后端 |
| `proxyctl restart` | 重启后端 |
| `proxyctl restart-clean` | 重启并清除缓存 |
| `proxyctl log` | tail -f 日志 |
| `proxyctl fix` | 修复 DNS/代理 |
| `proxyctl mode tun\|proxy` | 切换模式 |

---

## 后端支持

| 功能 | Mihomo | Sing-box |
|---|---|---|
| status | ✅ | ✅ |
| check | ✅ | ✅ |
| trace | ✅ | ✅ |
| audit | ✅ | ✅ |
| bench | ✅ | ⚠️ (API 限制) |
| recover | ✅ | ❌ |
| mode 切换 | ✅ | ✅ |
| dns-lock | ✅ | ✅ |

---

## 功能分类

### 核心生命周期工具（⭐ 开源核心）
- `proxyctl status` - 配置健康度仪表盘
- `proxyctl check` - 配置验证器
- `proxyctl trace` - 规则调试器
- `proxyctl audit` - 配置优化器
- `proxyctl bench` - 性能基准测试

### 引擎管理（⚠️ 配置化）
- `proxyctl start/stop/restart` - 启停后端
- `proxyctl mode tun\|proxy` - 模式切换
- `proxyctl fix` - 修复
- `proxyctl recover` - 软恢复

### 系统层集成（✅ 部分开源）
- `proxyctl dns-lock` - DNS 看门狗
- `proxyctl dns-unlock` - 停止看门狗
- `proxyctl-stuck-snapshot` - 故障抓取

### 本地特定功能（❌ 不开源）
- claude-proxy 管理 - 私有服务
- 内网路由注入 - 企业特定
- 订阅更新脚本 - 私有配置