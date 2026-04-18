# proxyctl 安装指南

## 系统要求

- macOS 12.0+ (Monterey 及以上)
- Python 3.8+
- Mihomo 或 Sing-box（至少一个）

## 快速安装

### 1. 克隆仓库

```bash
git clone https://github.com/crhan/proxyctl.git
cd proxyctl
```

### 2. 运行安装脚本

```bash
./install.sh
```

安装脚本会：
- 检查前置条件（Python、后端）
- 创建配置目录 `~/.config/proxyctl`
- 安装主程序到 `~/.local/bin`
- 复制配置模板

### 3. 配置 API

编辑配置文件：

```bash
nano ~/.config/proxyctl/config.yaml
```

必须配置：

```yaml
api_secret: your-clash-api-secret
```

### 4. 验证安装

```bash
proxyctl --help
proxyctl status
```

## 手动安装

如果不想使用安装脚本：

### 1. 创建目录

```bash
mkdir -p ~/.config/proxyctl
mkdir -p ~/.local/bin
```

### 2. 复制文件

```bash
# 主程序
cp bin/proxyctl ~/.local/bin/
chmod +x ~/.local/bin/proxyctl

# 脚本
cp scripts/dns-watchdog ~/.local/bin/proxyctl-dns-watchdog
cp scripts/stuck-snapshot ~/.local/bin/proxyctl-stuck-snapshot
chmod +x ~/.local/bin/proxyctl-*

# 配置模板
cp config.yaml.example ~/.config/proxyctl/config.yaml
```

### 3. 配置 PATH（如果需要）

添加以下行到 `~/.zshrc` 或 `~/.bashrc`：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

然后执行：

```bash
source ~/.zshrc
```

## 后端安装

### 安装 Mihomo

```bash
brew install mihomo
```

验证：

```bash
mihomo --version
```

### 安装 Sing-box

```bash
brew install sing-box
```

验证：

```bash
sing-box version
```

## 部署 launchdaemons（可选）

如需开机自启和系统级服务：

```bash
# 复制 plist 到系统目录
sudo cp launchdaemons/*.plist /Library/LaunchDaemons/

# 加载服务
sudo launchctl bootstrap system /Library/LaunchDaemons/com.mihomo.tun.plist
```

**注意：** plist 文件中的路径需要更新为你的实际路径。

## 卸载

```bash
# 保留配置文件
./uninstall.sh --keep-config

# 完全卸载（删除配置）
./uninstall.sh
```

手动清理（可选）：

```bash
# 删除系统 launchdaemons
sudo rm -f /Library/LaunchDaemons/com.proxyctl.*.plist
```

## 故障排查

### proxyctl 不在 PATH 中

```bash
# 检查 ~/.local/bin 是否在 PATH 中
echo $PATH

# 临时添加
export PATH="$HOME/.local/bin:$PATH"

# 永久添加（添加到 ~/.zshrc）
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Python 依赖

proxyctl 使用 Python 标准库，无需额外安装依赖。

如果提示找不到 `yaml` 模块：

```bash
pip3 install pyyaml
```

### 权限问题

如果安装脚本提示权限错误：

```bash
# 确保 ~/.local/bin 存在且有写权限
mkdir -p ~/.local/bin
chmod 755 ~/.local/bin
```

### 后端无法启动

检查后端二进制路径是否与 plist 中一致：

```bash
which mihomo
which sing-box
```

更新 plist 文件中的路径后重新加载。