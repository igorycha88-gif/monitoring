#!/usr/bin/env bash
# Канонический wrapper проверок качества на хосте (ЧТЗ_Инфра_DevDeps_И_Хост_Тулчейн).
# Использует .venv (где установлены и runtime-, и dev-зависимости проекта).
# Не загрязняет system-python.
#
# Запуск: ./scripts/check.sh
# Эквивалент: .venv/bin/ruff check . && .venv/bin/mypy . && .venv/bin/pytest

set -euo pipefail

# Resolve repo root regardless of CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VENV="$REPO_ROOT/.venv/bin"

if [[ ! -x "$VENV/python" ]]; then
  echo "ERROR: .venv не найден в $REPO_ROOT" >&2
  echo "Создайте:  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

echo "=== ruff check ==="
"$VENV/ruff" check .

echo "=== mypy ==="
"$VENV/mypy" .

echo "=== pytest ==="
"$VENV/pytest"

echo
echo "✅ Все проверки пройдены: ruff + mypy + pytest"
