#!/usr/bin/env bash
# telemt-shaper uninstaller

set -euo pipefail

INSTALL_DIR="/opt/telemt-shaper"
SERVICE_FILE="/etc/systemd/system/telemt-shaper.service"
SERVICE_NAME="telemt-shaper"
LOG_FILE="/var/log/telemt-shaper.log"
PID_FILE="/var/run/telemt-shaper.pid"

if [[ -t 1 ]]; then
    BOLD=$(tput bold); RED=$(tput setaf 1); GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3); BLUE=$(tput setaf 4); RESET=$(tput sgr0)
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; RESET=""
fi

info()   { echo -e "${BLUE}[i]${RESET} $*"; }
ok()     { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()   { echo -e "${YELLOW}[!]${RESET} $*"; }
error()  { echo -e "${RED}[✗]${RESET} $*" >&2; }

[[ $EUID -eq 0 ]] || { error "Требуется root."; exit 1; }

echo
echo "${BOLD}Удаление telemt-shaper${RESET}"
echo
read -rp "Вы уверены? [y/N] " -n 1 confirm </dev/tty
echo
[[ "$confirm" =~ ^[Yy]$ ]] || { info "Отменено."; exit 0; }

# 1. Остановить и disable службу
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "Останавливаю службу (это снимет все активные шейпы)..."
    systemctl stop "$SERVICE_NAME"
    ok "Служба остановлена"
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME" >/dev/null 2>&1
    ok "Служба отключена из автозапуска"
fi

# 2. Удалить unit
if [[ -f "$SERVICE_FILE" ]]; then
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    ok "Удалён ${SERVICE_FILE}"
fi

# 3. Спросить про конфиг
KEEP_CONFIG=false
if [[ -f "$INSTALL_DIR/config.py" ]]; then
    read -rp "Сохранить конфиг ($INSTALL_DIR/config.py)? [y/N] " -n 1 ans </dev/tty
    echo
    [[ "$ans" =~ ^[Yy]$ ]] && KEEP_CONFIG=true
fi

# 4. Удалить директорию
if [[ -d "$INSTALL_DIR" ]]; then
    if [[ "$KEEP_CONFIG" == true ]]; then
        TMPCFG=$(mktemp)
        cp "$INSTALL_DIR/config.py" "$TMPCFG"
        rm -rf "$INSTALL_DIR"
        mkdir -p "$INSTALL_DIR"
        mv "$TMPCFG" "$INSTALL_DIR/config.py"
        ok "Конфиг сохранён в ${INSTALL_DIR}/config.py"
    else
        rm -rf "$INSTALL_DIR"
        ok "Удалена директория ${INSTALL_DIR}"
    fi
fi

# 5. Спросить про логи
if [[ -f "$LOG_FILE" ]] || ls "${LOG_FILE}".* &>/dev/null; then
    read -rp "Удалить лог-файлы (${LOG_FILE}*)? [y/N] " -n 1 ans </dev/tty
    echo
    if [[ "$ans" =~ ^[Yy]$ ]]; then
        rm -f "$LOG_FILE" "${LOG_FILE}".*
        ok "Логи удалены"
    fi
fi

# 6. PID-файл
[[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"

# 7. Снять корневой HTB qdisc, ingress-qdisc и ifb-устройство — опционально
if command -v tc &>/dev/null; then
    echo
    warn "На интерфейсе остались tc-объекты, созданные telemt-shaper (HTB qdisc, ingress, ifb)."
    read -rp "Снести их? Укажите интерфейс или Enter чтобы пропустить: " iface </dev/tty
    if [[ -n "$iface" ]]; then
        # Корневой HTB qdisc
        if tc qdisc del dev "$iface" root 2>/dev/null; then
            ok "HTB qdisc снят с $iface"
        else
            warn "HTB qdisc на $iface не найден или уже снят"
        fi
        # Ingress qdisc (для ifb-редиректа)
        if tc qdisc del dev "$iface" ingress 2>/dev/null; then
            ok "Ingress qdisc снят с $iface"
        fi
    fi

    # ifb-устройство (имя по умолчанию, либо из config.py если сохранили)
    IFB_IFACE="ifb-telemt"
    if [[ -f "$INSTALL_DIR/config.py" ]]; then
        CFG_IFB=$(grep -E '^\s*IFB_IFACE\s*=' "$INSTALL_DIR/config.py" 2>/dev/null \
                  | head -1 | sed -E 's/.*=\s*"([^"]+)".*/\1/' | head -1)
        [[ -n "$CFG_IFB" ]] && IFB_IFACE="$CFG_IFB"
    fi
    if ip link show "$IFB_IFACE" &>/dev/null; then
        read -rp "Удалить ifb-устройство $IFB_IFACE? [y/N] " -n 1 ans </dev/tty
        echo
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            ip link del "$IFB_IFACE" 2>/dev/null && ok "ifb-устройство $IFB_IFACE удалено" \
                || warn "Не удалось удалить $IFB_IFACE"
        fi
    fi
fi

echo
ok "${BOLD}telemt-shaper удалён.${RESET}"
echo
