#!/bin/sh
# Entrypoint Prometheus.
# ADR-010: ключи X-Monitoring-Key сайтов больше не нужны Prometheus —
# скрейпы site-* идут через relay приложения (app:8088), секретов в
# конфиге/ env контейнера нет. Скрипт сохраняет флаги запуска.
set -eu

TEMPLATE="/etc/prometheus/prometheus.yml.tmpl"
TARGET="/etc/prometheus/prometheus.yml"

cp "$TEMPLATE" "$TARGET"

# Ретеншн задаётся в КОНФИГЕ (storage.tsdb.retention) — там он применяется
# ДО открытия TSDB (main.go), тогда как флаг --storage.tsdb.retention.time в
# 3.x deprecated и при uses-сравнении блоков vs ретеншн даёт баг удаления
# backfill-блоков (ЧТЗ_Вебмастер_Backfill_по_дням); "$@" пробрасывает
# command из docker-compose (будущие флаги без правки скрипта).
exec prometheus \
  --config.file="$TARGET" \
  "$@"
