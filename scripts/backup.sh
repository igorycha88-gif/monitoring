#!/usr/bin/env bash
# ЭПИК-8 / TASK-081: ежедневный бэкап конфигов и дашбордов мониторинга.
# Состав: config/, prometheus/, grafana/, docker-compose.yml, Dockerfile, .env
# Хранение: $BACKUP_DIR/monitoring-<ts>.tar.gz (+.sha256), ротация 14 копий.
# Запуск: cron (03:00) или вручную. Выход != 0 при ошибке.
set -euo pipefail

SRC_DIR="${MONITORING_DIR:-/root/monitoring}"
BACKUP_DIR="${BACKUP_DIR:-/root/backups/monitoring}"
RETENTION="${RETENTION:-14}"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
    logger -t monitoring-backup "$*" || true
}

if [[ ! -d "$SRC_DIR" ]]; then
    log "ERROR: source dir not found: $SRC_DIR"
    exit 1
fi

for item in config prometheus grafana docker-compose.yml; do
    if [[ ! -e "$SRC_DIR/$item" ]]; then
        log "ERROR: required item missing: $SRC_DIR/$item"
        exit 1
    fi
done

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

STAMP="$(date -u '+%Y%m%d-%H%M%S')"
ARCHIVE="$BACKUP_DIR/monitoring-${STAMP}.tar.gz"

log "backup start: src=$SRC_DIR -> $ARCHIVE"

tar -czf "$ARCHIVE" -C "$SRC_DIR" \
    config prometheus grafana docker-compose.yml \
    $([[ -f "$SRC_DIR/Dockerfile" ]] && echo Dockerfile) \
    $([[ -f "$SRC_DIR/.env" ]] && echo .env)

chmod 600 "$ARCHIVE"
sha256sum "$ARCHIVE" > "${ARCHIVE}.sha256"
chmod 600 "${ARCHIVE}.sha256"

log "backup ok: $(basename "$ARCHIVE") size=$(stat -c '%s' "$ARCHIVE") bytes"

# Ротация: оставляем $RETENTION свежих архивов (+их .sha256), остальное удаляем.
ls -1t "$BACKUP_DIR"/monitoring-*.tar.gz 2>/dev/null | tail -n +$((RETENTION + 1)) | while read -r old; do
    rm -f "$old" "${old}.sha256"
    log "rotated out: $(basename "$old")"
done

log "backup done, archives kept: $(ls -1 "$BACKUP_DIR"/monitoring-*.tar.gz | wc -l)"
