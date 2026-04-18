#!/usr/bin/env python3
"""
telemt-shaper: автоматический многоуровневый шейпер трафика для VPN-серверов с telemt.

Отслеживает потребление трафика по IP, шейпит превышающих порог через HTB + cake.
При продолжающемся давлении в потолок — повышает уровень шейпа (ужесточает лимит).
Снимает шейп после периода спокойствия.

Конфигурация: рядом со скриптом может лежать config.py — он переопределит любые
настройки ниже. Если файла нет — используются дефолты из этого файла.
"""

import fcntl
import importlib.util
import ipaddress
import logging
import math
import os
import re
import signal
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler

# === ЗАГРУЗКА ОПЦИОНАЛЬНОГО CONFIG.PY ===
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.py")

def _load_user_config():
    if not os.path.exists(_CONFIG_PATH):
        return None
    try:
        spec = importlib.util.spec_from_file_location("telemt_shaper_config", _CONFIG_PATH)
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        return cfg
    except Exception as e:
        sys.stderr.write(f"[FATAL] Ошибка в config.py: {e}\n")
        sys.exit(1)

_USER_CONFIG = _load_user_config()

def _cfg(name, default):
    if _USER_CONFIG is not None and hasattr(_USER_CONFIG, name):
        return getattr(_USER_CONFIG, name)
    return default

# === НАСТРОЙКИ (дефолты, переопределяются через config.py) ===
IFACE            = _cfg("IFACE", "ens18")

# Ingress-шейпинг через ifb.
# HTB как root qdisc шейпит только исходящий трафик. Для ограничения скорости
# в обе стороны входящий трафик редиректится на виртуальное устройство ifb,
# где применяется точно такая же HTB-иерархия.
SHAPE_INGRESS    = _cfg("SHAPE_INGRESS", True)
IFB_IFACE        = _cfg("IFB_IFACE", "ifb-telemt")

# Уровни шейпа. Применяются по порядку:
# - Чтобы попасть на L0 (первый шейп), IP должен превысить threshold_mbps[0]
#   exceed_ticks[0] раз подряд.
# - Чтобы перейти с L0 на L1, IP, уже шейпленный на L0, должен превысить
#   threshold_mbps[1] exceed_ticks[1] раз подряд. И так далее.
# Логично: каждый следующий threshold должен быть < предыдущего limit.
SHAPE_LEVELS     = _cfg("SHAPE_LEVELS", [
    {"threshold_mbps": 60, "limit_mbps": 40, "exceed_ticks": 2},  # L0
    {"threshold_mbps": 36, "limit_mbps": 24, "exceed_ticks": 4},  # L1
    {"threshold_mbps": 20, "limit_mbps": 8,  "exceed_ticks": 4},  # L2
])

CALM_RATIO       = _cfg("CALM_RATIO", 0.5)
UPGRADE_HEADROOM = _cfg("UPGRADE_HEADROOM", 0.9)
COOLDOWN_SECS    = _cfg("COOLDOWN_SECS", 120)
CHECK_INTERVAL   = _cfg("CHECK_INTERVAL", 5)
STATE_TTL_SECS   = _cfg("STATE_TTL_SECS", 600)
LOG_FILE         = _cfg("LOG_FILE", "/var/log/telemt-shaper.log")
LOG_MAX_BYTES    = _cfg("LOG_MAX_BYTES", 10 * 1024 * 1024)
LOG_BACKUP_COUNT = _cfg("LOG_BACKUP_COUNT", 3)
PID_FILE         = _cfg("PID_FILE", "/var/run/telemt-shaper.pid")
FILTER_PRIO      = _cfg("FILTER_PRIO", 10)

# Сети, для которых шейп не применяется (Telegram + RFC1918 + loopback).
SKIP_NETWORKS_RAW = _cfg("SKIP_NETWORKS_RAW", [
    # Telegram (https://core.telegram.org/resources/cidr.txt)
    "91.108.4.0/22", "91.108.8.0/22", "91.108.12.0/22",
    "91.108.16.0/22", "91.108.20.0/22", "91.108.56.0/22",
    "95.161.64.0/20",
    "149.154.160.0/20", "149.154.164.0/22",
    "149.154.168.0/22", "149.154.172.0/22",
    "185.76.151.0/24",
    # Приватные / служебные
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "127.0.0.0/8", "169.254.0.0/16",
])
SKIP_NETWORKS = [ipaddress.ip_network(n) for n in SKIP_NETWORKS_RAW]

DEBUG_MODE = "--debug" in sys.argv

# === СОСТОЯНИЕ ===
sock_state    = {}
ip_last_seen  = {}
shaped_ips    = {}
exceed_count  = {}
free_class_ids = []
next_class_id = 100
MAX_CLASS_ID  = 65000
_pid_fh = None

RE_SOCKET_LINE = re.compile(r'\b((?:\d{1,3}\.){3}\d{1,3}:\d+)\b')
RE_BYTES_SENT  = re.compile(r'bytes_sent:(\d+)')
RE_BYTES_RECV  = re.compile(r'bytes_received:(\d+)')

# === ЛОГИРОВАНИЕ ===
class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.WARNING:  "\033[33m",
        logging.ERROR:    "\033[1;31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, fmt, datefmt=None, use_color=True):
        super().__init__(fmt, datefmt=datefmt)
        self.use_color = use_color

    def format(self, record):
        msg = super().format(record)
        if self.use_color and record.levelno in self.COLORS:
            return f"{self.COLORS[record.levelno]}{msg}{self.RESET}"
        return msg


def setup_logging():
    use_color = sys.stderr.isatty()
    fmt_console = ColorFormatter('%(asctime)s [%(levelname)s] %(message)s',
                                 datefmt='%H:%M:%S', use_color=use_color)
    fmt_file = logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console = logging.StreamHandler()
    console.setFormatter(fmt_console)
    console.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
    file_handler.setFormatter(fmt_file)
    file_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)

    shape_logger = logging.getLogger("shaper.events")
    shape_logger.addHandler(file_handler)
    shape_logger.propagate = False

setup_logging()
log = logging.getLogger(__name__)
shape_log = logging.getLogger("shaper.events")


def log_event(event, ip, extra=""):
    shape_log.info(f"{event:<12} {ip:<20} {extra}")


def run(args, check=False):
    return subprocess.run(args, capture_output=True, text=True, check=check)


def is_skipped(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(ip in net for net in SKIP_NETWORKS)


def burst_kb_for(limit_mbps):
    rate_kbit = limit_mbps * 1000
    return max(4, rate_kbit // 800)


# === SINGLE-INSTANCE LOCK ===
def acquire_pid_lock():
    global _pid_fh
    try:
        _pid_fh = open(PID_FILE, "a+")
    except PermissionError as e:
        log.error(f"Нет прав на {PID_FILE}: {e}")
        sys.exit(1)
    try:
        fcntl.flock(_pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        try:
            _pid_fh.seek(0)
            existing = _pid_fh.read().strip()
            log.error(f"Уже запущен другой экземпляр (PID {existing}). Выход.")
        except Exception:
            log.error("Уже запущен другой экземпляр. Выход.")
        _pid_fh.close()
        _pid_fh = None
        sys.exit(1)
    _pid_fh.seek(0)
    _pid_fh.truncate()
    _pid_fh.write(f"{os.getpid()}\n")
    _pid_fh.flush()
    os.fsync(_pid_fh.fileno())


def release_pid_lock():
    global _pid_fh
    if _pid_fh is None:
        return
    try:
        fcntl.flock(_pid_fh, fcntl.LOCK_UN)
        _pid_fh.close()
        os.unlink(PID_FILE)
    except Exception:
        pass
    _pid_fh = None


# === SANITY CHECKS И BOOTSTRAP HTB ===
def check_prerequisites():
    if os.geteuid() != 0:
        log.error("Скрипт должен запускаться от root.")
        sys.exit(1)
    r = run(["ip", "link", "show", IFACE])
    if r.returncode != 0:
        log.error(f"Интерфейс {IFACE} не найден.")
        sys.exit(1)
    for binary in ("tc", "ss"):
        r = run(["which", binary])
        if r.returncode != 0:
            log.error(f"Не найден бинарник: {binary}")
            sys.exit(1)
    if not SHAPE_LEVELS:
        log.error("SHAPE_LEVELS пуст.")
        sys.exit(1)
    for i in range(len(SHAPE_LEVELS) - 1):
        cur_limit = SHAPE_LEVELS[i]["limit_mbps"]
        nxt_threshold = SHAPE_LEVELS[i + 1]["threshold_mbps"]
        if nxt_threshold >= cur_limit:
            log.warning(f"L{i+1}.threshold ({nxt_threshold}) >= L{i}.limit ({cur_limit}) — "
                        f"апгрейд на L{i+1} НИКОГДА не сработает. "
                        f"Рекомендуется L{i+1}.threshold <= {math.floor(cur_limit * UPGRADE_HEADROOM)}")
            continue
        suggested = cur_limit * UPGRADE_HEADROOM
        if nxt_threshold > suggested:
            log.warning(f"L{i+1}.threshold ({nxt_threshold}) > {int(UPGRADE_HEADROOM*100)}% "
                        f"от L{i}.limit ({suggested:.1f}) — апгрейд может не срабатывать. "
                        f"Рекомендуется значение {math.floor(suggested)} и ниже.")


def _setup_htb_tree(iface):
    """Строит дерево qdisc/class на интерфейсе: htb 1: (root) → 1:10 (10gbit, default) → fq."""
    run(["tc", "qdisc", "del", "dev", iface, "root"])
    r = run(["tc", "qdisc", "add", "dev", iface, "root", "handle", "1:",
             "htb", "default", "10", "r2q", "1"])
    if r.returncode != 0:
        log.error(f"Не удалось создать корневой HTB на {iface}: {r.stderr.strip()}")
        sys.exit(1)
    run(["tc", "class", "add", "dev", iface, "parent", "1:", "classid", "1:10",
         "htb", "rate", "10gbit", "quantum", "1514"])
    run(["tc", "qdisc", "add", "dev", iface, "parent", "1:10", "fq"])


def _setup_ingress_redirect():
    """Поднимает ifb-устройство, HTB на нём, ingress qdisc на IFACE и редирект."""
    # 1. Модуль ifb
    r = run(["modprobe", "ifb"])
    if r.returncode != 0:
        log.error(f"Не удалось загрузить модуль ifb: {r.stderr.strip()}")
        log.error("Для ingress-шейпа нужен модуль ifb. "
                  "Отключи SHAPE_INGRESS=False в config.py или установи модуль.")
        sys.exit(1)

    # 2. Виртуальное ifb-устройство (создаём с нашим именем, чтобы не конфликтовать
    #    с ifb0/ifb1, которые могут быть заняты другими сервисами)
    r = run(["ip", "link", "show", IFB_IFACE])
    if r.returncode != 0:
        r = run(["ip", "link", "add", "name", IFB_IFACE, "type", "ifb"])
        if r.returncode != 0:
            log.error(f"Не удалось создать {IFB_IFACE}: {r.stderr.strip()}")
            sys.exit(1)
    run(["ip", "link", "set", "dev", IFB_IFACE, "up"])

    # 3. HTB-дерево на ifb (зеркало дерева на IFACE)
    _setup_htb_tree(IFB_IFACE)

    # 4. Ingress qdisc на IFACE и редирект всего трафика на ifb
    run(["tc", "qdisc", "del", "dev", IFACE, "ingress"])
    r = run(["tc", "qdisc", "add", "dev", IFACE, "handle", "ffff:", "ingress"])
    if r.returncode != 0:
        log.error(f"Не удалось создать ingress на {IFACE}: {r.stderr.strip()}")
        sys.exit(1)
    r = run(["tc", "filter", "add", "dev", IFACE, "parent", "ffff:", "protocol", "ip",
             "u32", "match", "u32", "0", "0",
             "action", "mirred", "egress", "redirect", "dev", IFB_IFACE])
    if r.returncode != 0:
        log.error(f"Не удалось настроить mirred-редирект на {IFB_IFACE}: {r.stderr.strip()}")
        sys.exit(1)


def setup_htb():
    log.info(f"Настраиваю HTB на {IFACE}...")
    _setup_htb_tree(IFACE)
    if SHAPE_INGRESS:
        log.info(f"Настраиваю ingress-редирект {IFACE} → {IFB_IFACE}...")
        _setup_ingress_redirect()
        log.info(f"HTB готов (egress: {IFACE}, ingress: {IFB_IFACE}).")
    else:
        log.info(f"HTB готов (только egress: {IFACE}).")


# === СБОР СКОРОСТЕЙ ===
def get_ip_speeds():
    result = run(["ss", "-tnip", "state", "established"])
    lines = result.stdout.splitlines()
    now = time.time()

    seen_sockets = set()
    ip_delta_bytes = {}

    for i, line in enumerate(lines):
        if "telemt" not in line:
            continue
        matches = RE_SOCKET_LINE.findall(line)
        if len(matches) < 2:
            continue
        local_addr, remote_addr = matches[0], matches[1]
        remote_ip = remote_addr.rsplit(':', 1)[0]
        if is_skipped(remote_ip):
            continue
        tcp_info = lines[i + 1] if i + 1 < len(lines) else ""
        m_sent = RE_BYTES_SENT.search(tcp_info)
        m_recv = RE_BYTES_RECV.search(tcp_info)
        sent = int(m_sent.group(1)) if m_sent else 0
        recv = int(m_recv.group(1)) if m_recv else 0
        total = sent + recv

        key = (local_addr, remote_addr)
        seen_sockets.add(key)

        prev = sock_state.get(key)
        if prev and now > prev['ts']:
            delta = total - prev['bytes']
            if delta > 0:
                dt = now - prev['ts']
                ip_delta_bytes.setdefault(remote_ip, {'bytes': 0, 'dt': dt})
                ip_delta_bytes[remote_ip]['bytes'] += delta
                if dt > ip_delta_bytes[remote_ip]['dt']:
                    ip_delta_bytes[remote_ip]['dt'] = dt

        sock_state[key] = {'bytes': total, 'ts': now, 'remote_ip': remote_ip}

    stale = [k for k in sock_state if k not in seen_sockets]
    for k in stale:
        del sock_state[k]

    speeds_bps = {}
    for ip, info in ip_delta_bytes.items():
        if info['dt'] > 0:
            speeds_bps[ip] = (info['bytes'] * 8) / info['dt']
            ip_last_seen[ip] = now

    for ip in {s['remote_ip'] for s in sock_state.values()}:
        speeds_bps.setdefault(ip, 0)
        ip_last_seen[ip] = now

    return speeds_bps


# === УПРАВЛЕНИЕ ШЕЙПОМ ===
def alloc_class_id():
    global next_class_id
    if free_class_ids:
        return free_class_ids.pop()
    if next_class_id >= MAX_CLASS_ID:
        return None
    cid = next_class_id
    next_class_id += 1
    return cid


def free_class_id(cid):
    free_class_ids.append(cid)


def filter_prio_for(cid):
    """
    Возвращает уникальный prio для фильтра данного class_id.

    ПОЧЕМУ это важно: `tc filter del ... prio N` удаляет ВСЕ фильтры на указанном
    prio за один раз (аргументы `match ip dst X` и `flowid Y` в del-команде
    игнорируются ядром). Если все фильтры вешать на один prio — первый же
    remove_shape снесёт фильтры всех шейпнутых IP разом. Поэтому каждому
    class_id назначаем отдельный prio.

    Диапазон prio — 16-битный (1..65535). FILTER_PRIO — базовый prio (по умолчанию 10),
    cid начинается со 100, значит prio = 10, 11, 12, ... Запас огромный.
    """
    return FILTER_PRIO + cid - 100


def _teardown_ip_tc(cid, prio):
    """
    Удаляет все tc-объекты для class_id на обеих интерфейсах.
    Используется и для rollback, и для remove_shape. Ошибки игнорируются —
    функция должна быть безопасна к вызову на частично уже удалённых объектах.
    """
    # IFACE (egress)
    run(["tc", "filter", "del", "dev", IFACE, "parent", "1:",
         "protocol", "ip", "prio", str(prio)])
    run(["tc", "qdisc", "del", "dev", IFACE, "parent", f"1:{cid}"])
    run(["tc", "class", "del", "dev", IFACE, "classid", f"1:{cid}"])
    # IFB (ingress mirror)
    if SHAPE_INGRESS:
        run(["tc", "filter", "del", "dev", IFB_IFACE, "parent", "1:",
             "protocol", "ip", "prio", str(prio)])
        run(["tc", "qdisc", "del", "dev", IFB_IFACE, "parent", f"1:{cid}"])
        run(["tc", "class", "del", "dev", IFB_IFACE, "classid", f"1:{cid}"])


def add_shape(ip, level):
    cid = alloc_class_id()
    if cid is None:
        log.error(f"Нет свободных class_id для {ip}")
        return None

    limit_mbps = SHAPE_LEVELS[level]["limit_mbps"]
    burst_kb = burst_kb_for(limit_mbps)
    prio = filter_prio_for(cid)

    # На IFACE шейпим исходящий трафик (server → client), матчим по dst.
    cmds = [
        ["tc", "class", "add", "dev", IFACE, "parent", "1:10",
         "classid", f"1:{cid}", "htb",
         "rate", f"{limit_mbps}mbit", "ceil", f"{limit_mbps}mbit",
         "burst", f"{burst_kb}kb", "quantum", "1514"],
        ["tc", "qdisc", "add", "dev", IFACE, "parent", f"1:{cid}", "cake",
         "bandwidth", f"{limit_mbps}mbit"],
        ["tc", "filter", "add", "dev", IFACE, "parent", "1:",
         "protocol", "ip", "prio", str(prio), "u32",
         "match", "ip", "dst", f"{ip}/32", "flowid", f"1:{cid}"],
    ]
    # На IFB шейпим входящий трафик (client → server), приходит после mirred-редиректа.
    # В редиректе пакет сохраняет оригинальные src/dst, поэтому матчим по src.
    if SHAPE_INGRESS:
        cmds += [
            ["tc", "class", "add", "dev", IFB_IFACE, "parent", "1:10",
             "classid", f"1:{cid}", "htb",
             "rate", f"{limit_mbps}mbit", "ceil", f"{limit_mbps}mbit",
             "burst", f"{burst_kb}kb", "quantum", "1514"],
            ["tc", "qdisc", "add", "dev", IFB_IFACE, "parent", f"1:{cid}", "cake",
             "bandwidth", f"{limit_mbps}mbit"],
            ["tc", "filter", "add", "dev", IFB_IFACE, "parent", "1:",
             "protocol", "ip", "prio", str(prio), "u32",
             "match", "ip", "src", f"{ip}/32", "flowid", f"1:{cid}"],
        ]

    for cmd in cmds:
        r = run(cmd)
        if r.returncode != 0:
            log.error(f"Ошибка tc для {ip}: {' '.join(cmd)} → {r.stderr.strip()}")
            _teardown_ip_tc(cid, prio)
            free_class_id(cid)
            return None

    dirs = "egress+ingress" if SHAPE_INGRESS else "egress"
    msg = f"L{level}: {limit_mbps} Mbit"
    log.info(f"ШЕЙП: {ip} → {msg} (class 1:{cid}, {dirs})")
    log_event("ШЕЙП", ip, msg)
    return cid


def change_shape_level(ip, new_level):
    state = shaped_ips[ip]
    cid = state['class_id']
    limit_mbps = SHAPE_LEVELS[new_level]["limit_mbps"]
    burst_kb = burst_kb_for(limit_mbps)

    ifaces = [IFACE]
    if SHAPE_INGRESS:
        ifaces.append(IFB_IFACE)

    cmds = []
    for iface in ifaces:
        cmds.append(
            ["tc", "class", "change", "dev", iface, "parent", "1:10",
             "classid", f"1:{cid}", "htb",
             "rate", f"{limit_mbps}mbit", "ceil", f"{limit_mbps}mbit",
             "burst", f"{burst_kb}kb", "quantum", "1514"])
        cmds.append(
            ["tc", "qdisc", "change", "dev", iface, "parent", f"1:{cid}", "cake",
             "bandwidth", f"{limit_mbps}mbit"])

    for cmd in cmds:
        r = run(cmd)
        if r.returncode != 0:
            log.error(f"Ошибка tc change для {ip}: {' '.join(cmd)} → {r.stderr.strip()}")
            return False

    old_level = state['level']
    state['level'] = new_level
    msg = f"L{old_level} → L{new_level}: {limit_mbps} Mbit"
    log.info(f"АПГРЕЙД: {ip} → {msg} (class 1:{cid})")
    log_event("АПГРЕЙД", ip, msg)
    return True


def remove_shape(ip):
    info = shaped_ips.get(ip)
    if not info:
        return
    cid = info['class_id']
    prio = filter_prio_for(cid)
    _teardown_ip_tc(cid, prio)
    log.info(f"СНЯТ шейп: {ip} (class 1:{cid})")
    log_event("СНЯТ", ip)
    free_class_id(cid)
    del shaped_ips[ip]


def gc_state():
    now = time.time()
    stale_ips = [ip for ip, ts in ip_last_seen.items()
                 if now - ts > STATE_TTL_SECS and ip not in shaped_ips]
    for ip in stale_ips:
        ip_last_seen.pop(ip, None)
        exceed_count.pop(ip, None)


def shutdown(signum, frame):
    log.info(f"Получен сигнал {signum}, снимаю все шейпы...")
    for ip in list(shaped_ips.keys()):
        try:
            remove_shape(ip)
        except Exception as e:
            log.error(f"Ошибка при снятии шейпа {ip}: {e}")
    release_pid_lock()
    log.info("Завершено.")
    sys.exit(0)


def process_ip(ip, bps, now):
    mbps = bps / 1_000_000

    if ip in shaped_ips:
        state = shaped_ips[ip]
        cur_level = state['level']
        cur_limit_mbps = SHAPE_LEVELS[cur_level]["limit_mbps"]
        has_next = cur_level + 1 < len(SHAPE_LEVELS)

        if has_next:
            nxt = SHAPE_LEVELS[cur_level + 1]
            nxt_threshold_bps = nxt["threshold_mbps"] * 1_000_000
            if bps >= nxt_threshold_bps:
                state['upgrade_count'] += 1
                state['calm_since'] = None
                log.info(f"Жмёт {ip} ({mbps:.1f} Мбит/с) на L{cur_level} "
                         f"→ копим L{cur_level+1} [{state['upgrade_count']}/{nxt['exceed_ticks']}]")
                log_event("ЖМЁТ", ip,
                          f"{mbps:.1f} Mbit L{cur_level} [{state['upgrade_count']}/{nxt['exceed_ticks']}]")
                if state['upgrade_count'] >= nxt['exceed_ticks']:
                    if change_shape_level(ip, cur_level + 1):
                        state['upgrade_count'] = 0
                return
            else:
                state['upgrade_count'] = 0

        calm_threshold_bps = cur_limit_mbps * 1_000_000 * CALM_RATIO
        if bps < calm_threshold_bps:
            if state['calm_since'] is None:
                state['calm_since'] = now
                log.info(f"Успокоился: {ip} ({mbps:.1f} Мбит/с) на L{cur_level} "
                         f"— ждём {COOLDOWN_SECS}с")
                log_event("УСПОКОИЛСЯ", ip, f"{mbps:.1f} Mbit L{cur_level}")
            elif now - state['calm_since'] >= COOLDOWN_SECS:
                remove_shape(ip)
                exceed_count[ip] = 0
        else:
            state['calm_since'] = None
    else:
        l0 = SHAPE_LEVELS[0]
        l0_threshold_bps = l0["threshold_mbps"] * 1_000_000
        if bps >= l0_threshold_bps:
            exceed_count[ip] = exceed_count.get(ip, 0) + 1
            log.info(f"Всплеск {ip} ({mbps:.1f} Мбит/с) "
                     f"[{exceed_count[ip]}/{l0['exceed_ticks']}]")
            log_event("ВСПЛЕСК", ip,
                      f"{mbps:.1f} Mbit [{exceed_count[ip]}/{l0['exceed_ticks']}]")
            if exceed_count[ip] >= l0['exceed_ticks']:
                cid = add_shape(ip, level=0)
                if cid:
                    shaped_ips[ip] = {
                        'class_id': cid,
                        'level': 0,
                        'calm_since': None,
                        'upgrade_count': 0,
                    }
                # Сбрасываем счётчик в любом случае: и при успехе (IP теперь
                # в shaped_ips), и при неудаче (чтобы не крутить его в бесконечность;
                # если проблема не исчезнет — на следующие 3 тика накопится снова).
                exceed_count[ip] = 0
        else:
            exceed_count[ip] = 0


def main():
    check_prerequisites()
    acquire_pid_lock()
    setup_htb()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    cfg_status = "config.py" if _USER_CONFIG else "defaults"
    log.info(f"Запуск telemt-shaper на {IFACE} (PID {os.getpid()}, конфиг: {cfg_status})")
    for i, lvl in enumerate(SHAPE_LEVELS):
        log.info(f"  L{i}: trigger > {lvl['threshold_mbps']} Мбит/с "
                 f"× {lvl['exceed_ticks']} тиков ({lvl['exceed_ticks']*CHECK_INTERVAL}с) "
                 f"→ лимит {lvl['limit_mbps']} Мбит/с")
    log.info(f"Cooldown: {COOLDOWN_SECS}с | Calm ratio: {CALM_RATIO} от лимита текущего уровня")
    if DEBUG_MODE:
        log.debug("DEBUG режим включён")

    last_gc = time.time()

    while True:
        try:
            speeds = get_ip_speeds()

            if DEBUG_MODE:
                top = sorted([(ip, bps) for ip, bps in speeds.items() if bps > 0],
                             key=lambda x: x[1], reverse=True)[:5]
                for ip, bps in top:
                    marker = ""
                    if ip in shaped_ips:
                        marker = f"  [SHAPED L{shaped_ips[ip]['level']}]"
                    log.debug(f"  {ip}: {bps/1_000_000:.2f} Мбит/с{marker}")
                log.debug("")

            now = time.time()

            for ip, bps in speeds.items():
                process_ip(ip, bps, now)

            for ip in list(shaped_ips.keys()):
                if ip not in speeds:
                    state = shaped_ips[ip]
                    if state['calm_since'] is None:
                        state['calm_since'] = now
                    elif now - state['calm_since'] >= COOLDOWN_SECS:
                        remove_shape(ip)
                        exceed_count[ip] = 0

            if now - last_gc > 60:
                gc_state()
                last_gc = now

        except Exception as e:
            log.error(f"Ошибка в основном цикле: {e}", exc_info=DEBUG_MODE)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
