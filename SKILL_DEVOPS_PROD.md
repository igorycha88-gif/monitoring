# Скилл AI-DevOps: Продакшн-деплой на VPS (с изоляцией)

## Роль

Ты — DevOps-инженер ПРОДАКШН-деплоя проекта «Мониторинг сайтов». Деплоишь стек на VPS **130.49.129.241** в `/root/monitoring/`. Базовый скилл: `SKILL_DEVOPS.md`. Этот файл — прод-расширение.

## ДВА АБСОЛЮТНЫХ ПРИНЦИПА

### 1. ИЗОЛЯЦИЯ (на сервере живёт чужое приложение!)

```
VPS 130.49.129.241
├── /root/<чужой проект>/     ← СУЩЕСТВУЮЩЕЕ приложение. НЕ ТРОГАТЬ.
│   └── свои контейнеры, порты, nginx, cron, .env
│
└── /root/monitoring/          ← НАШ проект. Только тут работаем.
    └── monitoring-app (8088), prometheus (9091), grafana (3300)
        ВСЕ на 127.0.0.1
```

**ЗАПРЕЩЕНО:**
- `docker stop/rm/restart` ЧУЖИХ контейнеров
- Править чужой nginx / файрвол-правила чужих портов
- `docker system prune`, `docker volume prune`, `docker image prune -a` (глобально)
- Занимать занятые порты (проверка `ss -tlnp` ПЕРЕД запуском)
- Устанавливать системные пакеты без согласования

**РАЗРЕШЕНО:** только `/root/monitoring/`, контейнеры `monitoring-*`, сеть `monitoring-net`, порты 8088/9091/3300 на 127.0.0.1, `docker compose -p monitoring ...`

### 2. БЕЗОПАСНОСТЬ БЕЗ ДОМЕНА

- ВСЕ сервисы биндятся на **127.0.0.1** (проверка: `ss -tlnp | grep -E '8088|9091|3300'`)
- Доступ извне — **только SSH-туннель**:
  ```bash
  ssh -L 3300:127.0.0.1:3300 -L 8088:127.0.0.1:8088 root@130.49.129.241
  # локально: http://localhost:3300 (Grafana), http://localhost:8088 (app)
  ```
- SSH: доступ root по ключу; хост-ключ `SHA256:g3xVlty76Op1vIbTuwy+8M+0ZUXx4XgtQX2+YrTtbLY`; `PasswordAuthentication` не трогаем без отдельного решения
- Файрвол: входящие только SSH (22). Наши порты НЕ открывать
- fail2ban: проверить/предложить (через конвейер)
- Grafana: admin-пароль из `.env`, anonymous → off
- `.env` на VPS: chmod 600, НЕ синхронизируется rsync'ом, НИКОГДА не в git/логи

---

## Параметры прода

| Параметр | Значение |
|----------|----------|
| SSH | root@130.49.129.241 |
| Директория | /root/monitoring/ |
| Compose project | monitoring |
| App | 127.0.0.1:8088 (/health, /metrics) |
| Prometheus | 127.0.0.1:9091 |
| Grafana | 127.0.0.1:3300 |
| Доступ | SSH-туннель (3300, 8088) |

### Ключевые переменные .env на VPS

```bash
METRIKA_OAUTH_TOKEN=...        # токен Яндекс.Метрики
WEBMASTER_OAUTH_TOKEN=...      # ключ Яндекс.Вебмастера
GRAFANA_ADMIN_PASSWORD=...     # пароль Grafana
```

---

## Процедура деплоя (шаги)

### 0. Pre-flight

```bash
# SSH доступность (при первом коннекте сверить отпечаток хоста)
ssh -o ConnectTimeout=10 root@130.49.129.241 "echo OK"

# Фиксация состояния (для контроля изоляции и отката)
ssh root@VPS "docker ps --format '{{.Names}} {{.Image}} {{.Status}}'"  # PREVIOUS_STATE
ssh root@VPS "docker images | grep monitoring"                          # наши образы

# Порты: свободны ИЛИ заняты НАШИМИ. Если чужие → СТОП
ssh root@VPS "ss -tlnp | grep -E ':(8088|9091|3300) '"

# Диск
ssh root@VPS "df -m /root | tail -1"   # > 1GB

# .env
ssh root@VPS "test -f /root/monitoring/.env && stat -c '%a' /root/monitoring/.env"  # 600

# Docker Compose
ssh root@VPS "docker compose version"
```

### 1. Sync (rsync)

```bash
rsync -avz --delete \
  --exclude='.git' --exclude='__pycache__' --exclude='.venv' \
  --exclude='.env' --exclude='*.pyc' --exclude='.pytest_cache' \
  --exclude='.mypy_cache' --exclude='.ruff_cache' --exclude='node_modules' \
  ./ root@130.49.129.241:/root/monitoring/

# Целостность
ssh root@VPS "test -f /root/monitoring/docker-compose.yml && ls /root/monitoring/collectors/"
```

**`.env` в exclude ВСЕГДА** — секреты не покидают VPS.

### 2. Build (на VPS)

```bash
ssh root@VPS "cd /root/monitoring && docker compose -p monitoring build --no-cache"
ssh root@VPS "docker images | grep monitoring"
```

### 3. Deploy

```bash
ssh root@VPS "cd /root/monitoring && docker compose -p monitoring up -d --force-recreate"

# Healthcheck (максимум 60 сек)
ssh root@VPS "docker compose -p monitoring ps"

# Логи
ssh root@VPS "cd /root/monitoring && docker compose -p monitoring logs --tail=50"
# искать: Traceback, error, fatal, ECONNREFUSED, OOM

# БЕЗОПАСНОСТЬ: только 127.0.0.1!
ssh root@VPS "ss -tlnp | grep -E ':(8088|9091|3300) '"
# если 0.0.0.0 или [::] → КРИТИЧНО → фикс + откат
```

### 4. Verify

```bash
# App
ssh root@VPS "curl -sf http://127.0.0.1:8088/health"
ssh root@VPS "curl -s http://127.0.0.1:8088/metrics | grep -c '^monitoring_'"  # > 0

# Prometheus
ssh root@VPS "curl -sf http://127.0.0.1:9091/-/healthy"
ssh root@VPS "curl -s 'http://127.0.0.1:9091/api/v1/targets'"  # monitoring-app target UP

# Grafana
ssh root@VPS "curl -sf http://127.0.0.1:3300/api/health"
ssh root@VPS "curl -s -u admin:\$GRAFANA_ADMIN_PASSWORD http://127.0.0.1:3300/api/search?type=dash-db"
# anonymous off: curl без авторизации → 401

# Данные
ssh root@VPS "curl -s 'http://127.0.0.1:9091/api/v1/query?query=monitoring_collector_success'"

# ИЗОЛЯЦИЯ: сверка с PREVIOUS_STATE — чужие контейнеры не изменились
ssh root@VPS "docker ps --format '{{.Names}} {{.Status}}'"
```

### 5. Smoke + отчёт

Автосмок на VPS (см. PIPELINE_PROD.js → SMOKE), затем пользователю:

```
📋 Открой туннель: ssh -L 3300:127.0.0.1:3300 -L 8088:127.0.0.1:8088 root@130.49.129.241
□ Grafana http://localhost:3300 — логин + дашборд
□ Дашборд по сайту — панели с данными
□ http://localhost:8088/health — {"status":"ok"}
```

---

## Откат (Rollback)

Триггеры: healthcheck fail, критичные ошибки логов, биндинг на 0.0.0.0, verify NO-GO, команда пользователя («откат», «rollback»).

```bash
# 1. Остановить ТОЛЬКО наш стек
ssh root@VPS "cd /root/monitoring && docker compose -p monitoring down"

# 2. Если был предыдущий образ — откат на него
ssh root@VPS "docker images | grep monitoring-app"        # по дате
# retag/правка compose → docker compose -p monitoring up -d

# 3. Верификация отката + сверка: чужие контейнеры в исходном состоянии
ssh root@VPS "docker ps --format '{{.Names}} {{.Status}}'"
```

**Важно:** откат никогда не затрагивает существующее приложение — оно и не останавливалось.

---

## Очистка (только наше!)

```bash
# Наши старые образы (оставить текущий + 4 предыдущих)
ssh root@VPS "docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' | grep monitoring"
# удалить конкретные image ID через docker rmi <id>

# ЗАПРЕЩЕНО: docker image prune -a / system prune / volume prune
```

---

## Типичные проблемы

| Проблема | Симптом | Решение |
|----------|---------|---------|
| Порт занят | `address already in use` | `ss -tlnp | grep <port>`; если чужой → сменить порт НАМ |
| Контейнер падает | restart в `docker ps` | `docker compose -p monitoring logs app` → Traceback? |
| 429 в логах | Метрика/Вебмастер лимиты | Увеличить интервалы сбора, backoff |
| 401/403 от API | Неверный токен | Проверить .env на VPS |
| Нет данных в Grafana | Панели пустые | Prometheus targets → target down? Интервалы сбора? |
| Grafana 401 | anonymous off | Это ПРАВИЛЬНО — залогиниться admin |
| Туннель не работает | localhost:3300 не открывается | SSH-сессия жива? `-L` опции переданы? |
| Чужой контейнер изменился | Сверка docker ps | КРИТИЧНО: сообщить пользователю немедленно |

---

## Чек-лист прод-деплоя

### Перед
- [ ] SSH работает, хост-ключ сверен
- [ ] PREVIOUS_STATE зафиксирован
- [ ] Порты свободны/наши
- [ ] Диск > 1GB, .env (600) на месте
- [ ] Локально: `pytest && ruff check . && mypy .` зелёные

### Во время
- [ ] rsync без .env
- [ ] build --no-cache успешен
- [ ] up --force-recreate, все healthy
- [ ] Все сокеты на 127.0.0.1

### После
- [ ] /health, /metrics, Prometheus targets, Grafana — ОК
- [ ] Чужие контейнеры не изменились
- [ ] Smoke пройден, отчёт выведен
- [ ] Подсказка про SSH-туннель дана пользователю

---

## Ссылки

- Базовый скилл: `SKILL_DEVOPS.md`
- Спецификация: `PIPELINE_PROD.js`
- Правила проекта: `AGENTS.md` (разделы 0.1 Изоляция, 0.2 Безопасность)

---

*Скилл создан для безопасного продакшн-деплоя с абсолютной изоляцией от существующего приложения и доступом без домена (SSH-туннель).*
