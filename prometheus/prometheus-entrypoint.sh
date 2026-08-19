#!/bin/sh
# Entrypoint Prometheus (ЭПИК-9, ADR-007 D3).
# Подставляет SITE_METRICS_API_KEY из env в шаблон конфига при старте:
# ключ остаётся только в env (.env хоста, chmod 600) и памяти контейнера.
set -eu

TEMPLATE="/etc/prometheus/prometheus.yml.tmpl"
TARGET="/etc/prometheus/prometheus.yml"

API_KEY="${SITE_METRICS_API_KEY:-}"

# Ключ не задан → заглушка: контейнер стартует, скрейпы site-* получают
# 403 от nginx сайта → up=0, SiteMetricsEndpointDown в pending (как ADR-006 §3).
[ -n "$API_KEY" ] || API_KEY="NOT_CONFIGURED"

# esc-последок для sed (ключ теоретически содержит '/', ':' безопасен)
ESCAPED_KEY=$(printf '%s' "$API_KEY" | sed 's/[&/\]/\\&/g')

sed -e "s/__SITE_METRICS_API_KEY__/${ESCAPED_KEY}/g" "$TEMPLATE" > "$TARGET"

# ADR-008: retention 400d — годовые срезы топов запросов; "$@" пробрасывает
# command из docker-compose (будущие флаги без правки скрипта).
exec prometheus \
  --config.file="$TARGET" \
  --storage.tsdb.retention.time=400d \
  "$@"
