# ЧТЗ — ЭПИК-4: Прод-деплой MVP на VPS

**Маршрут:** Аналитик → DevOps (прямая задача, Маршрут 3 — инфраструктурная)
**Спецификация:** `PIPELINE_PROD.js`, `SKILL_DEVOPS_PROD.md`
**Статус:** утверждено (вопросы по SSH и .env закрыты пользователем)

## 1. Цель

Развернуть MVP-стек мониторинга (monitoring-app, prometheus, grafana) на VPS
`root@130.49.129.241` в `/root/monitoring/` по конвейеру
PRE-FLIGHT → SYNC → BUILD → DEPLOY → VERIFY → SMOKE → FINALIZE,
с абсолютной изоляцией от существующего приложения.

## 2. Задачи из BACKLOG

| ID | Задача | DoD |
|----|--------|-----|
| TASK-040 | Деплой по PIPELINE_PROD.js: rsync → build → up; порты 8088/9091/3300 на 127.0.0.1 | Все healthcheck зелёные, существующее приложение не тронуто |
| TASK-041 | Доступ: SSH-туннель, admin-пароль Grafana из .env, anonymous off | Доступ только через туннель |

## 3. Исходное состояние сервера (PREVIOUS_STATE, зафиксировано)

- Существующее приложение: 6 контейнеров `smarttraffic-*`
  (nginx, singbox, certbot, api, frontend, landing) — НЕ ТРОГАТЬ
- `/root/monitoring/` — отсутствует (первый деплой)
- Порты 8088/9091/3300 — свободны
- Диск: ~15.5 GB свободно
- Docker 29.4.1 + Compose v5.1.3 — ОК

## 4. Решённые вопросы (ответы пользователя)

1. **SSH:** пароль root предоставлен однократно; на VPS установлен публичный
   ключ `id_ed25519` — дальше только key auth. Пароль нигде не сохранён.
2. **.env:** локальный `.env` копируется на VPS вручную (ssh, chmod 600),
   НЕ через rsync.

## 5. План DevOps (по PIPELINE_PROD.js)

1. PRE-FLIGHT: SSH ✅ (key), порты ✅, диск ✅, docker ✅, .env → создать
   (копия локального, chmod 600), PREVIOUS_STATE зафиксирован ✅
2. SYNC: `rsync -avz --delete` c exclude (.env, .git, кеши, .venv) →
   spot-check целостности
3. BUILD: `docker compose -p monitoring build --no-cache` на VPS
4. DEPLOY: `docker compose -p monitoring up -d --force-recreate` →
   healthcheck ≤ 60 сек → логи без Traceback/error →
   `ss -tlnp`: все сокеты ТОЛЬКО 127.0.0.1
5. VERIFY: `/health` 200; `/metrics` содержит `monitoring_*`;
   Prometheus healthy + target UP; Grafana healthy, дашборды провиженены,
   anonymous off (401 без авторизации); сверка изоляции с PREVIOUS_STATE
6. SMOKE: автосмок на VPS + чеклист пользователю (SSH-туннель)
7. FINALIZE: отчёт деплоя, обновление BACKLOG.md (ЭПИК-4 → ✅)

## 6. Критерии приёмки

- [ ] Все контейнеры monitoring-* healthy
- [ ] Все сокеты 8088/9091/3300 на 127.0.0.1 (не 0.0.0.0)
- [ ] `/health` → 200; `/metrics` → `monitoring_*` > 0
- [ ] Prometheus target monitoring-app UP
- [ ] Grafana: дашборды загружены, anonymous off, вход admin
- [ ] Контейнеры smarttraffic-* в состоянии PREVIOUS_STATE (не тронуты)
- [ ] .env на VPS с правами 600, не в rsync/git
- [ ] Пользователю выдан чеклист доступа через SSH-туннель

## 7. Известные ограничения (не блокируют)

- `config/sites.yml` содержит примеры (example.com/example.org), счётчиков
  Метрики нет → трафик-панели без данных до добавления реальных сайтов
  (обновляется правкой конфига + рестартом, отдельной задачей)

## 8. Откат

`docker compose -p monitoring down` (только наш стек). Первый деплой —
предыдущего образа нет, откат = сервер в состоянии до деплоя.
Существующее приложение при любых сценариях не затрагивается.
