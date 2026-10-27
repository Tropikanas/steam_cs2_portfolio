#!/bin/bash
# Установка или обновление на сервере (Ubuntu 24.04). Запускать от root из корня проекта:
#   bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/steamfolio
SRC_DIR=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE=/etc/steamfolio/steamfolio.env

DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.12-venv sqlite3 caddy ufw fail2ban >/dev/null

id steamfolio >/dev/null 2>&1 || useradd --system --home-dir /var/lib/steamfolio --shell /usr/sbin/nologin steamfolio
install -d -o steamfolio -g steamfolio -m 750 /var/lib/steamfolio
install -d -m 755 /etc/steamfolio
[ -f "$ENV_FILE" ] || install -m 640 -g steamfolio "$SRC_DIR/deploy/steamfolio.env.example" "$ENV_FILE"
grep -q '^STEAMFOLIO_DOMAIN=' "$ENV_FILE" || grep '^STEAMFOLIO_DOMAIN=' "$SRC_DIR/deploy/steamfolio.env.example" >> "$ENV_FILE"

[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
rm -rf "$SRC_DIR/build" "$SRC_DIR"/*.egg-info
"$APP_DIR/venv/bin/pip" install -q "$SRC_DIR"

# Команда steamfolio для ручного запуска: от пользователя steamfolio и с настройками сервера.
cat > /usr/local/bin/steamfolio <<'EOF'
#!/bin/sh
set -a; . /etc/steamfolio/steamfolio.env; set +a
cd /var/lib/steamfolio
exec runuser -u steamfolio -- /opt/steamfolio/venv/bin/steamfolio "$@"
EOF
chmod 755 /usr/local/bin/steamfolio

# Миграции базы — до перезапуска сайта.
/usr/local/bin/steamfolio runs -n 1 >/dev/null

install -m 644 "$SRC_DIR"/deploy/steamfolio-collect.service "$SRC_DIR"/deploy/steamfolio-collect.timer \
    "$SRC_DIR"/deploy/steamfolio-backup.service "$SRC_DIR"/deploy/steamfolio-backup.timer \
    "$SRC_DIR"/deploy/steamfolio-health.service "$SRC_DIR"/deploy/steamfolio-health.timer \
    "$SRC_DIR"/deploy/steamfolio-web.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now steamfolio-collect.timer steamfolio-backup.timer steamfolio-health.timer
systemctl enable steamfolio-web.service
systemctl restart steamfolio-web.service

# HTTPS: Caddy сам выпускает и продлевает сертификат Let's Encrypt.
DOMAIN=$(sed -n 's/^STEAMFOLIO_DOMAIN=//p' "$ENV_FILE" | tr -d '"')
cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
	encode gzip
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		X-Frame-Options DENY
		Referrer-Policy same-origin
		-Server
	}
	reverse_proxy 127.0.0.1:8000
}
EOF
systemctl enable caddy >/dev/null 2>&1
systemctl reload-or-restart caddy

# Межсетевой экран: открыты только SSH, HTTP (нужен для выпуска сертификата) и HTTPS.
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# SSH: только по ключу (если ключ уже добавлен — иначе можно потерять доступ к серверу).
if [ -s /root/.ssh/authorized_keys ]; then
    install -m 644 "$SRC_DIR/deploy/sshd-steamfolio.conf" /etc/ssh/sshd_config.d/00-steamfolio.conf
    sshd -t && systemctl reload ssh
else
    echo "ВНИМАНИЕ: нет ключей в /root/.ssh/authorized_keys — вход по паролю оставлен включённым"
fi
install -m 644 "$SRC_DIR/deploy/fail2ban-sshd.local" /etc/fail2ban/jail.d/sshd.local
systemctl enable fail2ban >/dev/null 2>&1
systemctl restart fail2ban

echo "Готово: https://$DOMAIN"
systemctl list-timers 'steamfolio-*' --no-pager | head -3
