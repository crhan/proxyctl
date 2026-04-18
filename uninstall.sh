#!/bin/bash
# proxyctl 卸载脚本
# 用法：./uninstall.sh [--dry-run] [--keep-config]

set -euo pipefail

# 颜色定义
RED="\033[0;31m"
GREEN="\033[0;32m"
YELLOW="\033[0;33m"
NC="\033[0m"

# 路径
HOME_DIR="$HOME"
CONFIG_DIR="$HOME_DIR/.config/proxyctl"
BIN_DIR="$HOME_DIR/.local/bin"

# 选项
DRY_RUN="${1:-}"
KEEP_CONFIG="${2:-}"

log() { echo -e "$*"; }
info() { log "${GREEN}✓${NC} $*"; }
warn() { log "${YELLOW}⚠${NC} $*"; }
error() { log "${RED}✗${NC} $*"; }

# 卸载主程序
uninstall_binaries() {
    log "\n=== 卸载主程序 ==="

    for bin in proxyctl proxyctl-dns-watchdog proxyctl-stuck-snapshot; do
        local target="$BIN_DIR/$bin"
        if [ -f "$target" ]; then
            info "删除 $target"
            if [ -z "$DRY_RUN" ]; then
                rm -f "$target"
            fi
        else
            warn "$target 不存在"
        fi
    done
}

# 卸载配置
uninstall_config() {
    if [ "$KEEP_CONFIG" = "--keep-config" ]; then
        log "\n=== 保留配置文件 ==="
        warn "配置文件将保留在：$CONFIG_DIR"
        return
    fi

    log "\n=== 卸载配置 ==="

    if [ -d "$CONFIG_DIR" ]; then
        info "删除配置目录：$CONFIG_DIR"
        if [ -z "$DRY_RUN" ]; then
            rm -rf "$CONFIG_DIR"
        fi
    else
        warn "$CONFIG_DIR 不存在"
    fi
}

# 卸载系统 launchdaemons
uninstall_system_launchdaemons() {
    log "\n=== 卸载系统 launchdaemons ==="

    for label in com.mihomo.tun com.singbox.tun com.proxyctl.dns-lock; do
        local plist="/Library/LaunchDaemons/$label.plist"
        if [ -f "$plist" ]; then
            # 先卸载
            if [ -z "$DRY_RUN" ]; then
                sudo launchctl bootout "system/$label" 2>/dev/null || true
                sudo rm -f "$plist"
            fi
            info "卸载 $plist"
        else
            warn "$plist 不存在"
        fi
    done
}

# 显示后续步骤
show_next_steps() {
    log "\n=== 后续步骤 ==="

    if [ "$KEEP_CONFIG" != "--keep-config" ]; then
        echo "配置文件已删除。"
    fi

    echo ""
    echo "如需完全清理，可手动删除："
    echo "  - ~/.config/mihomo/ (Mihomo 配置)"
    echo "  - ~/.config/sing-box/ (Sing-box 配置)"
    echo ""
    echo "如要重新安装，运行："
    echo "  ./install.sh"
}

# 主流程
main() {
    log "================================"
    log "  proxyctl 卸载脚本"
    log "================================"

    if [ "$DRY_RUN" = "--dry-run" ]; then
        warn "干跑模式 - 不会实际删除"
    fi

    if [ "$KEEP_CONFIG" = "--keep-config" ]; then
        warn "保留配置文件"
    fi

    uninstall_system_launchdaemons
    uninstall_binaries
    uninstall_config
    show_next_steps

    log "\n================================"
    log "  卸载完成！"
    log "================================"
}

main "$@"