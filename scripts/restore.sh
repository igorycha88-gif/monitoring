#!/usr/bin/env bash
# ЭПИК-8 / TASK-081: восстановление конфигов из архива backup.sh.
# Использование: restore.sh <архив.tar.gz> [dest] [--with-env]
#   dest        — куда распаковать (default: /root/monitoring)
#   --with-env  — восстановить и .env (иначе существующий .env не трогаем)
# Без --with-env свежесозданный .env из архива НЕ копируется.
set -euo pipefail

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
}

usage() {
    echo "usage: $0 <archive.tar.gz> [dest-dir] [--with-env]" >&2
    exit 1
}

WITH_ENV=0
ARCHIVE=""
DEST="/root/monitoring"

for arg in "$@"; do
    case "$arg" in
        --with-env) WITH_ENV=1 ;;
        -h|--help) usage ;;
        *)
            if [[ -z "$ARCHIVE" ]]; then
                ARCHIVE="$arg"
            else
                DEST="$arg"
            fi
            ;;
    esac
done

[[ -n "$ARCHIVE" ]] || usage
[[ -f "$ARCHIVE" ]] || { log "ERROR: archive not found: $ARCHIVE"; exit 1; }

if [[ -f "${ARCHIVE}.sha256" ]]; then
    log "verifying sha256: $(basename "$ARCHIVE")"
    (cd "$(dirname "$ARCHIVE")" && sha256sum -c "$(basename "${ARCHIVE}.sha256")") \
        || { log "ERROR: sha256 mismatch for $ARCHIVE"; exit 1; }
else
    log "WARN: no .sha256 next to archive, skipping integrity check"
fi

TMPDIR_EXTRACT="$(mktemp -d /tmp/monitoring-restore.XXXXXX)"
trap 'rm -rf "$TMPDIR_EXTRACT"' EXIT

log "extracting to temp dir: $TMPDIR_EXTRACT"
tar -xzf "$ARCHIVE" -C "$TMPDIR_EXTRACT"

mkdir -p "$DEST"

if [[ "$WITH_ENV" -eq 1 ]]; then
    log "copying ALL content (with .env) to $DEST"
    if [[ -f "$TMPDIR_EXTRACT/.env" ]]; then
        cp -p "$TMPDIR_EXTRACT/.env" "$DEST/.env"
        chmod 600 "$DEST/.env"
    fi
else
    log "copying content WITHOUT .env (use --with-env to restore it)"
fi

# Копируем всё, кроме .env (обработан выше), не затирая права.
shopt -s dotglob
for item in "$TMPDIR_EXTRACT"/*; do
    name="$(basename "$item")"
    [[ "$name" == ".env" ]] && continue
    rm -rf "${DEST:?}/$name"
    cp -a "$item" "${DEST:?}/$name"
done
shopt -u dotglob

log "restore done: $ARCHIVE -> $DEST"
log "next step: cd $DEST && docker compose -p monitoring up -d --force-recreate"
