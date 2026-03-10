# prn-site-ping

Небольшая утилита на **Tkinter**: показывает кнопки принтеров, проверяет доступность (DNS resolve + TCP:80),
раскрашивает кнопки и открывает веб-интерфейс принтера по клику.

Также умеет периодически синхронизировать список очередей с print-server (по умолчанию `\\dc02`).

## Быстрый старт

### Запуск без установки

```bash
python -m prn_site_ping --config ./config/printers.txt
```

### Установка как пакет (удобно для Codex/CI)

```bash
python -m pip install -e .
prn-site-ping --config ./config/printers.txt
```

## Конфиг принтеров

Формат простой: **один принтер в строке**.
Пустые строки и строки, начинающиеся с `#`, игнорируются.

Пример `config/printers.txt`:

```text
# POG
PRN-160-POG
PRN-204-POG

# 5 этаж
PRN-5-104
PRN-5-105
```

Можно сделать локальный override и не коммитить его:
- `config/printers.local.txt`
- или передать путь через `--config` / переменную `PRN_SITE_PING_CONFIG`

## Параметры запуска

```bash
python -m prn_site_ping --help
```

Полезные:
- `--config PATH` — путь до файла со списком принтеров
- `--columns N` — количество колонок кнопок (по умолчанию 3)
- `--timeout SECONDS` — таймаут TCP-проверки (по умолчанию 1.0)
- `--print-server HOST` — print-server для автосинхронизации (по умолчанию `dc02`)
- `--sync-interval N` — интервал синхронизации в секундах (по умолчанию 300, `0` отключает)

## Файлы состояния

Приложение пишет:
- лог: `printer_manager.log`
- позицию окна: `window_position.txt`

По умолчанию — в директорию данных приложения:
- Windows: `%APPDATA%\prn-site-ping\`
- Linux: `~/.config/prn-site-ping/`
- macOS: `~/Library/Application Support/prn-site-ping/`

(Если не получилось — fallback в текущую папку.)
