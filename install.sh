#!/bin/bash
# proxyctl 安装脚本（支持 macOS / Linux）
# 用法：./install.sh [--dry-run]

set -euo pipefail

# 颜色定义
RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
NC="\033[0m"

# 路径
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME"
CONFIG_DIR="$HOME_DIR/.config/proxyctl"
BIN_DIR="$HOME_DIR/.local/bin"
OS="$(uname -s)"   # Darwin | Linux

# 干跑模式
DRY_RUN="${1:-}"

log() { echo -e "$*"; }
info() { log "${GREEN}✓${NC} $*"; }
warn() { log "${YELLOW}⚠${NC} $*"; }
error() { log "${RED}✗${NC} $*"; }

# 检查前置条件
check_prerequisites() {
    log "\n=== 检查前置条件 (${OS}) ==="

    # 检查 Python 3
    if command -v python3 >/dev/null 2>&1; then
        info "Python 3: $(python3 --version)"
    else
        error "Python 3 未安装"
        exit 1
    fi

    # 检查后端（至少一个）
    local has_backend=false

    if command -v mihomo >/dev/null 2>&1 || [ -x "$BIN_DIR/mihomo" ]; then
        info "Mihomo: $(which mihomo 2>/dev/null || echo "$BIN_DIR/mihomo")"
        has_backend=true
    else
        warn "Mihomo 未安装"
    fi

    if command -v sing-box >/dev/null 2>&1 || [ -x "$BIN_DIR/sing-box" ]; then
        info "Sing-box: $(which sing-box 2>/dev/null || echo "$BIN_DIR/sing-box")"
        has_backend=true
    else
        warn "Sing-box 未安装"
    fi

    if [ "$has_backend" = false ]; then
        error "至少需要安装一个后端 (Mihomo 或 Sing-box)"
        exit 1
    fi

    # 检查 ~/.local/bin
    if [ ! -d "$BIN_DIR" ]; then
        warn "$BIN_DIR 不存在，将创建"
        if [ -z "$DRY_RUN" ]; then
            mkdir -p "$BIN_DIR"
        fi
    fi

    # 检查 ~/.local/bin 是否在 PATH 中
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        warn "$BIN_DIR 不在 PATH 中"
        echo "  请添加以下行到 ~/.bashrc 或 ~/.zshrc:"
        echo "    export PATH=\"$BIN_DIR:\$PATH\""
    fi

    # Linux 额外检查
    if [ "$OS" = "Linux" ]; then
        if command -v systemctl >/dev/null 2>&1; then
            info "systemd: $(systemctl --version | head -1)"
        else
            error "systemd 未找到（proxyctl Linux 模式依赖 systemd --user）"
            exit 1
        fi
    fi
}

# 创建配置目录
setup_config_dir() {
    log "\n=== 设置配置目录 ==="

    if [ ! -d "$CONFIG_DIR" ]; then
        info "创建配置目录：$CONFIG_DIR"
        if [ -z "$DRY_RUN" ]; then
            mkdir -p "$CONFIG_DIR"
        fi
    else
        info "配置目录已存在：$CONFIG_DIR"
    fi

    # 复制配置模板
    if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
        info "复制配置模板"
        if [ -z "$DRY_RUN" ]; then
            cp "$SCRIPT_DIR/config.yaml.example" "$CONFIG_DIR/config.yaml"
        fi
    else
        warn "配置文件已存在：$CONFIG_DIR/config.yaml"
    fi

    # macOS 子目录
    if [ "$OS" = "Darwin" ]; then
        for subdir in launchdaemons scripts; do
            local target_dir="$CONFIG_DIR/$subdir"
            if [ ! -d "$target_dir" ]; then
                info "创建目录：$target_dir"
                if [ -z "$DRY_RUN" ]; then
                    mkdir -p "$target_dir"
                fi
            fi
        done
    fi
}

# 安装主程序（通过 uv）
install_binaries() {
    log "\n=== 安装主程序 ==="

    # 检查 uv
    if ! command -v uv >/dev/null 2>&1; then
        error "uv 未安装。请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    info "uv: $(uv --version)"

    # 用 uv tool install 安装 proxyctl 到用户 PATH
    info "通过 uv tool install 安装 proxyctl"
    if [ -z "$DRY_RUN" ]; then
        uv tool install --force "$SCRIPT_DIR"
    fi

    # macOS 辅助脚本
    if [ "$OS" = "Darwin" ]; then
        for script in dns-watchdog stuck-snapshot; do
            local target_script="$BIN_DIR/proxyctl-$script"
            info "安装 $script → $target_script"
            if [ -z "$DRY_RUN" ]; then
                cp "$SCRIPT_DIR/scripts/$script" "$target_script"
                chmod +x "$target_script"
            fi
        done
    fi
}

# macOS: 安装 launchdaemons
install_launchdaemons() {
    if [ "$OS" != "Darwin" ]; then
        return
    fi

    log "\n=== 安装 launchdaemons ==="

    local target_ld_dir="$CONFIG_DIR/launchdaemons"

    for plist in com.mihomo.tun.plist com.singbox.tun.plist com.proxyctl.dns-lock.plist; do
        local src="$SCRIPT_DIR/launchdaemons/$plist"
        local dst="$target_ld_dir/$plist"

        if [ -f "$src" ]; then
            info "复制 $plist"
            if [ -z "$DRY_RUN" ]; then
                # 更新 plist 中的路径
                sed "s|/Users/yourname|$HOME_DIR|g" "$src" > "$dst"
            fi
        else
            warn "$plist 不存在"
        fi
    done

    echo ""
    echo "  部署到系统需要 sudo 权限："
    echo "    sudo cp $target_ld_dir/*.plist /Library/LaunchDaemons/"
}

# Linux: 安装 systemd user service
install_systemd_services() {
    if [ "$OS" != "Linux" ]; then
        return
    fi

    log "\n=== 安装 systemd user service ==="

    local user_unit_dir="$HOME_DIR/.config/systemd/user"
    if [ -z "$DRY_RUN" ]; then
        mkdir -p "$user_unit_dir"
    fi

    for unit in mihomo.service sing-box.service; do
        local src="$SCRIPT_DIR/systemd/$unit"
        local dst="$user_unit_dir/$unit"

        if [ -f "$src" ]; then
            if [ -f "$dst" ]; then
                warn "$unit 已存在，跳过（保留现有配置）"
            else
                info "安装 $unit → $dst"
                if [ -z "$DRY_RUN" ]; then
                    cp "$src" "$dst"
                fi
            fi
        fi
    done

    if [ -z "$DRY_RUN" ]; then
        systemctl --user daemon-reload 2>/dev/null || true
        info "systemd --user daemon-reload 完成"
    fi

    # 检查 linger
    local linger
    linger=$(loginctl show-user "$(whoami)" -p Linger 2>/dev/null | cut -d= -f2)
    if [ "$linger" != "yes" ]; then
        warn "用户 linger 未启用（服务在登出后会停止）"
        echo "  执行以下命令启用："
        echo "    sudo loginctl enable-linger $(whoami)"
    else
        info "用户 linger 已启用"
    fi
}

# 配置说明
show_config_instructions() {
    log "\n=== 配置说明 ==="

    echo "请编辑配置文件："
    echo "  $CONFIG_DIR/config.yaml"
    echo ""
    echo "必须配置的项："
    echo "  api_secret: your-clash-api-secret"
    echo ""
    echo "可选配置："
    echo "  backend: mihomo        # 或 singbox"
    echo "  api_base: http://127.0.0.1:9090"
    echo "  config_dir: $HOME_DIR/.config"
}

# 验证安装
verify_installation() {
    log "\n=== 验证安装 ==="

    if [ -z "$DRY_RUN" ]; then
        if command -v proxyctl >/dev/null 2>&1; then
            info "proxyctl 已安装"
            proxyctl --help >/dev/null 2>&1 && info "proxyctl --help 正常" || warn "proxyctl --help 失败"
        else
            warn "proxyctl 不在 PATH 中，请检查 PATH 设置"
        fi
    fi
}

# 主流程
main() {
    log "================================"
    log "  proxyctl 安装脚本 (${OS})"
    log "================================"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        warn "干跑模式 - 不会实际安装"
    fi

    check_prerequisites
    setup_config_dir
    install_binaries
    install_launchdaemons
    install_systemd_services
    show_config_instructions
    verify_installation

    log "\n================================"
    log "  安装完成！"
    log "================================"
    log ""
    log "下一步："
    log "  1. 编辑 ~/.config/proxyctl/config.yaml"
    log "  2. 配置 api_secret"
    log "  3. 运行 proxyctl status 验证"
    log ""

    if [ "$OS" = "Darwin" ]; then
        log "如需部署 launchdaemons 到系统："
        log "  sudo cp ~/.config/proxyctl/launchdaemons/*.plist /Library/LaunchDaemons/"
        log ""
    fi
    if [ "$OS" = "Linux" ]; then
        log "启动引擎："
        log "  proxyctl start"
        log ""
    fi
}

main "$@"