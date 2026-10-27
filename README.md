# Портфолио Steam

Учёт инвестиций в предметы CS2 на торговой площадке Steam. Техническое задание — [TZ.md](TZ.md).

## Этап 1: сборщик цен и курсов

- `steamfolio/market/` — всё, что знает об устройстве площадки Steam (лучший ордер, лоты).
- `steamfolio/rates.py` — внутренний курс Steam (медиана по дорогим предметам).
- `steamfolio/collector.py` — один запуск: курсы, затем цены; история в SQLite.

Команды на сервере:

```bash
steamfolio report --links   # таблица последнего запуска
steamfolio runs             # история запусков
steamfolio track list       # отслеживаемые предметы
```

Установка или обновление на сервере (от root, из корня проекта): `bash deploy/install.sh`.
Настройки — `/etc/steamfolio/steamfolio.env` (образец: `deploy/steamfolio.env.example`).
