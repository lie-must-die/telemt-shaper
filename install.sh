#!/usr/bin/env bash
# telemt-shaper installer
# Использование:
#   curl -fsSL https://raw.githubusercontent.com/lie-must-die/telemt-shaper/main/install.sh | sudo bash

set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-lie-must-die/telemt-shaper}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
RAW_URL="https://raw.githubusercontent.com/${GITHUB_REPO}/${GITHUB_BRANCH}"

INSTALL_DIR="/opt/telemt-shaper"
SERVICE_FILE="/etc/systemd/system/telemt-shaper.service"
SERVICE_NAME="telemt-shaper"

if [[ -t 1 ]]; then
    BOLD=$(tput bold); RED=$(tput setaf 1); GREEN=$(tput setaf 2)
    YELLOW=$(tput setaf 3); BLUE=$(tput setaf 4); CYAN=$(tput setaf 6); RESET=$(tput sgr0)
else
    BOLD=""; RED=""; GREEN=""; YELLOW=""; BLUE=""; CYAN=""; RESET=""
fi

info()    { echo -e "${BLUE}[i]${RESET} $*"; }
ok()      { echo -e "${GREEN}[✓]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[!]${RESET} $*"; }
error()   { echo -e "${RED}[✗]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}${CYAN}━━ $* ━━${RESET}"; }
prompt()  { echo -ne "${BOLD}?${RESET} $* "; }

die() { error "$*"; exit 1; }

cat <<'EOF'

  ┌──────────────────────────────────────────────┐
  │              telemt-shaper                   │
  │   Многоуровневый шейпер для MTProto-прокси   │
  └──────────────────────────────────────────────┘

EOF

# === ПРОВЕРКИ СИСТЕМЫ ===
step "Проверка системы"

[[ $EUID -eq 0 ]] || die "Требуется root. Запустите через sudo."
[[ "$(uname -s)" == "Linux" ]] || die "Поддерживается только Linux."
ok "root + Linux"

if ! command -v python3 &>/dev/null; then
    die "python3 не найден. Установите: apt install python3"
fi
PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
if [[ "$PY_MAJOR" -lt 3 ]] || { [[ "$PY_MAJOR" -eq 3 ]] && [[ "$PY_MINOR" -lt 8 ]]; }; then
    die "Требуется Python 3.8+. Найден: $PY_VERSION"
fi
ok "python3 $PY_VERSION"

MISSING=()
for bin in tc ss curl; do
    command -v "$bin" &>/dev/null || MISSING+=("$bin")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Не хватает утилит: ${MISSING[*]}"
    info "Устанавливаю недостающие пакеты..."
    apt-get update -qq
    apt-get install -y iproute2 curl >/dev/null
    for bin in "${MISSING[@]}"; do
        command -v "$bin" &>/dev/null || die "Не удалось установить $bin"
    done
fi
ok "tc, ss, curl"

if ! tc qdisc add dev lo root cake 2>/dev/null; then
    if ! modprobe sch_cake 2>/dev/null; then
        die "Модуль ядра sch_cake недоступен. Нужно ядро Linux >= 4.19 с поддержкой cake."
    fi
    if ! tc qdisc add dev lo root cake 2>/dev/null; then
        die "sch_cake не работает даже после modprobe. Обновите ядро."
    fi
fi
tc qdisc del dev lo root 2>/dev/null || true
ok "Модуль sch_cake доступен"

[[ -d /run/systemd/system ]] || die "systemd не обнаружен."
ok "systemd"

# === СУЩЕСТВУЮЩАЯ УСТАНОВКА ===
step "Проверка существующей установки"

EXISTING_CONFIG=false
if [[ -f "$SERVICE_FILE" ]] || [[ -d "$INSTALL_DIR" ]]; then
    warn "Обнаружена существующая установка."
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        info "Останавливаю службу..."
        systemctl stop "$SERVICE_NAME"
        ok "Служба остановлена"
    fi
    if [[ -f "$INSTALL_DIR/config.py" ]]; then
        EXISTING_CONFIG=true
    fi
else
    ok "Чистая установка"
fi

# === ВЫБОР ИНТЕРФЕЙСА ===
step "Выбор сетевого интерфейса"

DEFAULT_IFACE=$(ip route show default 2>/dev/null | awk '/^default/ {print $5; exit}')
mapfile -t IFACES < <(ip -br link show | awk '$1 != "lo" {print $1}' | sed 's/@.*//')

if [[ ${#IFACES[@]} -eq 0 ]]; then
    die "Не найдено ни одного интерфейса (кроме lo)."
fi

echo "Доступные интерфейсы:"
for i in "${!IFACES[@]}"; do
    iface="${IFACES[$i]}"
    addr=$(ip -br addr show "$iface" 2>/dev/null | awk '{$1=$2=""; print $0}' | xargs || true)
    marker=""
    if [[ "$iface" == "$DEFAULT_IFACE" ]]; then
        marker="${GREEN}← default route${RESET}"
    fi
    printf "  ${BOLD}%d)${RESET} %-12s %s %s\n" "$((i+1))" "$iface" "$addr" "$marker"
done
echo

DEFAULT_IDX=1
for i in "${!IFACES[@]}"; do
    if [[ "${IFACES[$i]}" == "$DEFAULT_IFACE" ]]; then
        DEFAULT_IDX=$((i+1))
        break
    fi
done

while true; do
    prompt "Введите номер интерфейса [${DEFAULT_IDX}]:"
    read -r choice </dev/tty || die "Не удалось прочитать ввод"
    choice="${choice:-$DEFAULT_IDX}"
    if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le ${#IFACES[@]} ]]; then
        SELECTED_IFACE="${IFACES[$((choice-1))]}"
        break
    fi
    warn "Неверный ввод."
done
ok "Выбран интерфейс: ${BOLD}${SELECTED_IFACE}${RESET}"

# === КОНФИГ ===
step "Конфигурация уровней шейпа"

cat <<EOF
По умолчанию используются проверенные значения:
  L0: trigger > 60 Мбит/с × 2 тика → лимит 40 Мбит/с
  L1: trigger > 36 Мбит/с × 4 тика → лимит 24 Мбит/с
  L2: trigger > 20 Мбит/с × 4 тика → лимит  8 Мбит/с

Их всегда можно поменять позже в ${INSTALL_DIR}/config.py
EOF

WRITE_CONFIG=true
if [[ "$EXISTING_CONFIG" == true ]]; then
    echo
    warn "Обнаружен существующий конфиг: ${INSTALL_DIR}/config.py"
    echo "  ${BOLD}1)${RESET} Оставить старый конфиг ${GREEN}(рекомендуется при обновлении)${RESET}"
    echo "  ${BOLD}2)${RESET} Перезаписать дефолтным"
    echo "  ${BOLD}3)${RESET} Показать diff и решить"
    while true; do
        prompt "Ваш выбор [1]:"
        read -r choice </dev/tty
        choice="${choice:-1}"
        case "$choice" in
            1) WRITE_CONFIG=false; break ;;
            2) WRITE_CONFIG=true; break ;;
            3)
                TMP_NEW=$(mktemp)
                if curl -fsSL "${RAW_URL}/config.py.example" -o "$TMP_NEW"; then
                    sed -i "s|^IFACE = .*|IFACE = \"${SELECTED_IFACE}\"|" "$TMP_NEW"
                    echo
                    diff -u "${INSTALL_DIR}/config.py" "$TMP_NEW" || true
                    rm -f "$TMP_NEW"
                fi
                ;;
            *) warn "Введите 1, 2 или 3" ;;
        esac
    done
fi

# === УСТАНОВКА ===
step "Установка файлов"

mkdir -p "$INSTALL_DIR"
ok "Создана директория ${INSTALL_DIR}"

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

info "Скачивание файлов из ${GITHUB_REPO}..."
for f in telemt-shaper.py telemt-shaper.service config.py.example; do
    if ! curl -fsSL "${RAW_URL}/${f}" -o "${TMPDIR}/${f}"; then
        die "Не удалось скачать ${f}. Проверьте доступ к ${RAW_URL}"
    fi
done
ok "Скачано: telemt-shaper.py, telemt-shaper.service, config.py.example"

install -m 755 "${TMPDIR}/telemt-shaper.py" "${INSTALL_DIR}/telemt-shaper.py"
ok "Установлен ${INSTALL_DIR}/telemt-shaper.py"

if [[ "$WRITE_CONFIG" == true ]]; then
    cp "${TMPDIR}/config.py.example" "${INSTALL_DIR}/config.py"
    sed -i "s|^IFACE = .*|IFACE = \"${SELECTED_IFACE}\"|" "${INSTALL_DIR}/config.py"
    chmod 644 "${INSTALL_DIR}/config.py"
    ok "Создан ${INSTALL_DIR}/config.py с интерфейсом ${SELECTED_IFACE}"
else
    ok "Старый ${INSTALL_DIR}/config.py оставлен без изменений"
fi

install -m 644 "${TMPDIR}/config.py.example" "${INSTALL_DIR}/config.py.example"

install -m 644 "${TMPDIR}/telemt-shaper.service" "$SERVICE_FILE"
ok "Установлен ${SERVICE_FILE}"

# === ЗАПУСК ===
step "Запуск службы"

systemctl daemon-reload
ok "daemon-reload"

systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
ok "enable ${SERVICE_NAME}"

systemctl start "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Служба запущена"
else
    error "Служба не запустилась. Смотрите логи:"
    echo "    journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
    exit 1
fi

# === ИТОГ ===
step "Готово!"

cat <<EOF

${GREEN}${BOLD}telemt-shaper установлен и работает.${RESET}

  ${BOLD}Полезные команды:${RESET}
    Статус:           ${CYAN}systemctl status ${SERVICE_NAME}${RESET}
    Логи службы:      ${CYAN}journalctl -u ${SERVICE_NAME} -f${RESET}
    События шейпа:    ${CYAN}tail -f /var/log/telemt-shaper.log${RESET}
    Перезапуск:       ${CYAN}systemctl restart ${SERVICE_NAME}${RESET}
    Остановка:        ${CYAN}systemctl stop ${SERVICE_NAME}${RESET}

  ${BOLD}Файлы:${RESET}
    Скрипт:           ${INSTALL_DIR}/telemt-shaper.py
    Конфиг:           ${INSTALL_DIR}/config.py
    Шаблон:           ${INSTALL_DIR}/config.py.example
    Service unit:     ${SERVICE_FILE}
    Лог событий:      /var/log/telemt-shaper.log
    PID:              /var/run/telemt-shaper.pid

  ${BOLD}Документация:${RESET}
    https://github.com/${GITHUB_REPO}

EOF
