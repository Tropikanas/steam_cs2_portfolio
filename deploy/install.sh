#!/bin/bash
# Установка или обновление на сервере (Ubuntu 24.04). Запускать от root из корня проекта:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/steamfolio
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12-venv sqlite3 >/dev/null

id steamfolio >/dev/null 2>&1 || useradd --system --home-dir /var/lib/steamfolio --shell /usr/sbin/nologin steamfolio
install -d -o steamfolio -g steamfolio -m 750 /var/lib/steamfolio
install -d -m 755 /etc/steamfolio
[ -f /etc/steamfolio/steamfolio.env ] || install -m 640 -g steamfolio "$SRC_DIR/deploy/steamfolio.env.example" /etc/steamfolio/steamfolio.env

[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q "$SRC_DIR"

# Команда steamfolio для ручного запуска: от пользователя steamfolio и с настройками сервера.
cat > /usr/local/bin/steamfolio <<'EOF'
#!/bin/sh
set -a; . /etc/steamfolio/steamfolio.env; set +a
cd /var/lib/steamfolio
exec runuser -u steamfolio -- /opt/steamfolio/venv/bin/steamfolio "$@"
EOF
chmod 755 /usr/local/bin/steamfolio

install -m 644 "$SRC_DIR/deploy/steamfolio-collect.service" "$SRC_DIR/deploy/steamfolio-collect.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now steamfolio-collect.timer
echo "Готово. Следующий запуск:"
systemctl list-timers steamfolio-collect.timer --no-pager | head -2
