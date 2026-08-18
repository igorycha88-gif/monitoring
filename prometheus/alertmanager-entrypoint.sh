#!/bin/sh
# Entrypoint Alertmanager (ЭПИК-7, ADR-006 §3).
# Подставляет секреты из env в шаблон конфига при старте контейнера:
# токен остаётся только в env (.env хоста) и памяти контейнера.
set -eu

TEMPLATE="/etc/alertmanager/alertmanager.yml.tmpl"
TARGET="/etc/alertmanager/alertmanager.yml"

BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
CHAT_ID="${TELEGRAM_CHAT_ID:-}"

# Секреты не заданы → стартуем с заглушкой: алерты видны в UI Prometheus,
# доставка в Telegram активируется после заполнения .env (ADR-006 §3).
# chat_id=0 НЕВАЛИДЕН для alertmanager ("missing chat_id") → заглушка 1;
# отправка в несуществующий чат логируется ошибкой, сервис остаётся healthy.
[ -n "$BOT_TOKEN" ] || BOT_TOKEN="0000000000:NOT_CONFIGURED"
[ -n "$CHAT_ID" ] || CHAT_ID="1"

# esc-последок для sed (токен содержит '/', ':' безопасен)
ESCAPED_TOKEN=$(printf '%s' "$BOT_TOKEN" | sed 's/[&/\]/\\&/g')

sed \
  -e "s/__TELEGRAM_BOT_TOKEN__/${ESCAPED_TOKEN}/g" \
  -e "s/__TELEGRAM_CHAT_ID__/${CHAT_ID}/g" \
  "$TEMPLATE" > "$TARGET"

exec alertmanager \
  --config.file="$TARGET" \
  --storage.path=/alertmanager
