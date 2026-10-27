# Портфолио Steam

Учёт инвестиций в предметы CS2 на торговой площадке Steam. Техническое задание — [TZ.md](TZ.md).

## Этап 1: сборщик цен и курсов

- `steamfolio/market/` — всё, что знает об устройстве площадки Steam (лучший ордер, лоты).
- `steamfolio/rates.py` — внутренний курс Steam (медиана по дорогим предметам).
- `steamfolio/collector.py` — один запуск: курсы, затем цены; история в SQLite.

## Этап 2: журнал операций

- `steamfolio/ledger.py` — пересчёт портфеля, средней цены, прибыли и ордеров из журнала (чистые функции).
- `steamfolio/journal.py` — запись операций, изменение, удаление, откат с предпросмотром и проверкой.
- `steamfolio/fx.py` — курс на дату операции: Steam с начала истории, раньше — ЦБ РФ.

## Этап 3: веб-интерфейс

Сайт: https://steamportfolio.duckdns.org (Caddy → uvicorn на 127.0.0.1:8000, служба `steamfolio-web`).

- `steamfolio/web/` — FastAPI: страницы «Портфолио» и «Ордера», JSON API, шаблоны и статика.
- `steamfolio/views.py` — строки таблиц: текущая цена, % к средней и за сутки/неделю/месяц, итоги в USD.
- `steamfolio/catalog.py` — справочник предметов (ByMykel/CSGO-API): иконки, типы, подсказки.
- `steamfolio/auth.py` — вход: scrypt, сессии на 30 дней, пауза после 5 неверных попыток.

## Этап 4: журнал операций

Вкладка «Журнал операций» (`/journal`): все операции с фильтрами, откат и его отмена с предпросмотром,
изменение и удаление покупок, продаж и исполнений ордеров (отдельными операциями «было → стало»).
API: `/api/journal`, `/api/journal/{id}/preview|cancel|restore|edit|delete`.

## Этап 5: статистика

Вкладка «Статистика» (`/stats`, API `/api/stats`): сводка, линии «Стоимость портфеля» и «Вложено»,
круговая диаграмма по типам с раскрытием и сменой типа, «Лучшие операции». Графики — Chart.js 4.4.1 (`static/`).

- `steamfolio/stats.py` — ряды графика, разбивка по типам, лучшие операции.
- `steamfolio/history.py` — оценка стоимости до первого планового сбора: история медианных цен продаж
  Steam (Market Beta, `QueryPriceHistory`), пересчёт в USD по курсу ЦБ на каждый день; загружается один раз на предмет.

## Этап 6: Telegram и резервные копии

- `steamfolio/alerts.py` — правила после каждого планового сбора (±50% за сутки, ×2 и ×3 за неделю по лучшему ордеру в USD),
  антиспам, минимальная цена; предупреждение после 2 неудачных сборов подряд и сообщение о восстановлении.
- `steamfolio/telegram.py` — бот только для исходящих сообщений владельцу; токен в `/var/lib/steamfolio/telegram_token` (600).
- `steamfolio/backup.py` — ежедневная копия базы в `/var/lib/steamfolio/backups` (14 штук, таймер `steamfolio-backup`
  в 01:00 МСК), по понедельникам — в Telegram (без сессий входа).
- Вкладка «Настройки» (`/settings`): пороги уведомлений, тестовое сообщение, отправка копии.

## Этап 7: сервер

- SSH только по ключу (`deploy/sshd-steamfolio.conf` → `/etc/ssh/sshd_config.d/00-steamfolio.conf`), fail2ban для SSH.
- Открыты только 22, 80, 443 (ufw). Службы поднимаются сами после сбоя (`Restart=always`) и перезагрузки.
- `steamfolio healthcheck` — таймер `steamfolio-health` раз в 15 минут открывает сайт; после 2 неудач подряд — сообщение в Telegram.

### Восстановление базы из копии

Копии: на сервере `/var/lib/steamfolio/backups/steamfolio-ГГГГ-ММ-ДД.db.gz` (14 последних дней), в Telegram — по понедельникам.
В копии из Telegram нет сессий входа — после восстановления нужно просто войти заново.

```bash
systemctl stop steamfolio-web steamfolio-collect.timer steamfolio-backup.timer steamfolio-health.timer
cp /var/lib/steamfolio/steamfolio.db /var/lib/steamfolio/steamfolio.broken.db   # на всякий случай
rm -f /var/lib/steamfolio/steamfolio.db-wal /var/lib/steamfolio/steamfolio.db-shm
gunzip -c /var/lib/steamfolio/backups/steamfolio-2026-11-02.db.gz > /var/lib/steamfolio/steamfolio.db
chown steamfolio:steamfolio /var/lib/steamfolio/steamfolio.db && chmod 600 /var/lib/steamfolio/steamfolio.db
systemctl start steamfolio-web steamfolio-collect.timer steamfolio-backup.timer steamfolio-health.timer
```

Файл из Telegram сначала копируется на сервер: `scp steamfolio-2026-11-02.db.gz root@СЕРВЕР:/var/lib/steamfolio/backups/`.
На новом сервере: склонировать репозиторий, `bash deploy/install.sh`, восстановить базу как выше,
`steamfolio set-password` и `steamfolio telegram setup` (токен бота в копию не входит).

Команды на сервере:

```bash
steamfolio report --links   # таблица последнего запуска
steamfolio runs             # история запусков
steamfolio track list       # отслеживаемые предметы
steamfolio demo             # демонстрация журнала и отката на временной базе
steamfolio set-password     # пароль для входа на сайт (логин admin)
steamfolio catalog type "Название" "Наклейки операций"   # переопределить тип предмета
steamfolio telegram setup   # ввести токен бота и привязать свой чат
steamfolio telegram test    # тестовое сообщение
steamfolio backup --send    # копия базы сейчас и сразу в Telegram
steamfolio healthcheck      # открывается ли сайт
fail2ban-client status sshd # заблокированные адреса
```

Установка или обновление на сервере (от root, из корня проекта): `bash deploy/install.sh`.
Настройки — `/etc/steamfolio/steamfolio.env` (образец: `deploy/steamfolio.env.example`).
