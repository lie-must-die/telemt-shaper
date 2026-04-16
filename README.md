# telemt-shaper

![Latest Release](https://img.shields.io/github/v/release/lie-must-die/telemt-shaper?color=neon) ![Stars](https://img.shields.io/github/stars/lie-must-die/telemt-shaper?style=social)

**Многоуровневый шейпер трафика для Telegram MTProto-прокси на базе [telemt](https://github.com/seriyps/mtproto_proxy).**

Отслеживает скорость каждого подключённого IP, и если кто-то начинает выжимать канал — автоматически режет ему скорость. Если клиент продолжает упираться в лимит — лимит ужесточается. Когда трафик утихает — шейп снимается.

## Зачем это

MTProto-прокси любят использовать как «бесплатный VPN» — качать фильмы, сериалы, музыку. Один такой клиент может выжать всю полосу сервера и положить опыт остальным. `telemt-shaper` ловит таких и аккуратно режет им скорость, не мешая остальным пользователям.

## Возможности

- **Многоуровневый шейп.** Не «ударил молотком и забыл», а постепенное ужесточение для тех, кто продолжает злоупотреблять. По умолчанию: 40 → 24 → 8 Мбит/с.
- **Автоматическое снятие** ограничений после периода тишины (по умолчанию 2 минуты).
- **Не трогает Telegram.** Серверы Telegram добавлены в список исключений из коробки.
- **Минимум зависимостей.** Только Python 3 и `iproute2`. Никаких внешних библиотек.
- **Безопасно при старте/остановке.** Graceful shutdown через SIGTERM, single-instance lock через flock.
- **Удобные логи.** Цветной вывод в консоль/journald + отдельный файл с историей всех событий шейпа.

## Установка

```bash
curl -fsSL https://raw.githubusercontent.com/lie-must-die/telemt-shaper/main/install.sh | sudo bash
```

Установщик интерактивный: проверит зависимости, покажет список интерфейсов, создаст конфиг, поставит systemd-службу и запустит её. Подробнее — в [docs/install.md](docs/install.md).

Если хочется сначала посмотреть что будет выполняться:

```bash
curl -fsSL https://raw.githubusercontent.com/lie-must-die/telemt-shaper/main/install.sh -o install.sh
less install.sh
sudo bash install.sh
```

## Требования

- Linux с ядром 4.19+ (для модуля `sch_cake`)
- Python 3.8+
- `iproute2` (`tc`, `ss`)
- systemd
- `telemt` (или другой MTProto-прокси с именем процесса `telemt`) — основной случай использования
- root — нужен для управления `tc` и чтения `bytes_sent/received` из всех сокетов

Протестировано на Ubuntu 22.04, 24.04 и Debian 12.

## Принцип работы

Кратко: каждые 5 секунд скрипт парсит вывод `ss -tnip`, считает скорость каждого IP по дельтам `bytes_sent/received` отдельных сокетов. Если IP стабильно превышает порог — создаём для него отдельный HTB-класс с `cake` qdisc и навешиваем фильтр по dst-адресу.

Подробное описание архитектуры, потоков данных и принятия решений — в [docs/how-it-works.md](docs/how-it-works.md).

## Конфигурация

Все настройки в `/opt/telemt-shaper/config.py`. Дефолты:

```python
IFACE = "eth0"  # подставляется автоматически при установке

SHAPE_LEVELS = [
    {"threshold_mbps": 60, "limit_mbps": 40, "exceed_ticks": 2},  # L0
    {"threshold_mbps": 36, "limit_mbps": 24, "exceed_ticks": 4},  # L1
    {"threshold_mbps": 20, "limit_mbps": 8,  "exceed_ticks": 4},  # L2
]

CALM_RATIO = 0.5
COOLDOWN_SECS = 120
```

Полное описание всех параметров — в [docs/configuration.md](docs/configuration.md).

После изменений:

```bash
sudo systemctl restart telemt-shaper
```

## Логи и мониторинг

```bash
# Вывод службы (всё что в консоль)
journalctl -u telemt-shaper -f

# История событий шейпа (ВСПЛЕСК / ШЕЙП / ЖМЁТ / АПГРЕЙД / УСПОКОИЛСЯ / СНЯТ)
tail -f /var/log/telemt-shaper.log

# Текущие активные шейпы
tc class show dev eth0 | grep -v '1:10'
tc filter show dev eth0
```

Пример события из лога:

```
2026-04-16 00:59:29 ШЕЙП         158.160.188.132      L0: 40 Mbit
2026-04-16 01:00:05 АПГРЕЙД      158.160.188.132      L0 → L1: 24 Mbit
2026-04-16 01:00:26 АПГРЕЙД      158.160.188.132      L1 → L2: 8 Mbit
2026-04-16 01:00:57 УСПОКОИЛСЯ   158.160.188.132      0.0 Mbit L2
2026-04-16 01:04:01 СНЯТ         158.160.188.132
```

## Обновление

Просто запустите установщик ещё раз — он сам остановит службу, обновит файлы, спросит про конфиг (рекомендуется оставить старый) и перезапустит:

```bash
curl -fsSL https://raw.githubusercontent.com/lie-must-die/telemt-shaper/main/install.sh | sudo bash
```

## Удаление

```bash
sudo bash /opt/telemt-shaper/uninstall.sh
```

Или скачать отдельно:

```bash
curl -fsSL https://raw.githubusercontent.com/lie-must-die/telemt-shaper/main/uninstall.sh | sudo bash
```

## Документация

- [Как это работает](docs/how-it-works.md) — архитектура, потоки данных, как принимаются решения о шейпе
- [Конфигурация](docs/configuration.md) — детальное описание всех параметров
- [Troubleshooting](docs/troubleshooting.md) — частые проблемы и их решения

## Issues и обратная связь

Багрепорты и предложения — в [GitHub Issues](https://github.com/lie-must-die/telemt-shaper/issues).

## Лицензия

[MIT](LICENSE)
