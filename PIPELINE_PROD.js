/**
 * PIPELINE_PROD.js — Конвейер продакшн-деплоя «Мониторинг сайтов» на VPS
 *
 * Это НЕ исполняемый файл. Это ФОРМАЛЬНАЯ СПЕЦИФИКАЦИЯ конвейера
 * продакшн-деплоя, которую AI-агент (opencode) обязан выполнять пошагово.
 *
 * Отличия от основного конвейера (PIPELINE.js):
 *   - PIPELINE.js    → разработка (локально, docker compose)
 *   - PIPELINE_PROD  → деплой на VPS 130.49.129.241, /root/monitoring/
 *
 * ГЛАВНЫЙ ПРИНЦИП: ИЗОЛЯЦИЯ ОТ СУЩЕСТВУЮЩЕГО ПРИЛОЖЕНИЯ.
 * На сервере уже работает продакшн-приложение. Его НЕЛЬЗЯ трогать.
 *
 * ВТОРОЙ ПРИНЦИП: БЕЗОПАСНОСТЬ БЕЗ ДОМЕНА.
 * Все наши сервисы биндятся на 127.0.0.1. Доступ — SSH-туннель.
 * Порт 3300 (Grafana), 8088 (app), 9091 (Prometheus) наружу НЕ открываются.
 *
 * Триггерные фразы пользователя:
 *   «деплой на прод», «задеплой», «деплой в прод», «выложить на прод»,
 *   «push to prod», «deploy to production», «запусти прод деплой»,
 *   «деплой на сервер»
 *
 * Базовый скилл: SKILL_DEVOPS.md
 * Прод-скилл: SKILL_DEVOPS_PROD.md
 */

// ═══════════════════════════════════════════════════════════════════════
// КОНСТАНТЫ ПРОДА
// ═══════════════════════════════════════════════════════════════════════

const PROD_CONSTANTS = {
  VPS_HOST: "root@130.49.129.241",
  SSH_HOST_KEY: "SHA256:g3xVlty76Op1vIbTuwy+8M+0ZUXx4XgtQX2+YrTtbLY", // проверять при первом коннекте
  APP_DIR: "/root/monitoring",
  COMPOSE_PROJECT: "monitoring",
  DOCKER_NETWORK: "monitoring-net",
  PORTS: { app: 8088, prometheus: 9091, grafana: 3300 }, // ВСЕ на 127.0.0.1!
  RSYNC_EXCLUDES: ".git,__pycache__,.venv,.env,*.pyc,.pytest_cache,.mypy_cache,node_modules",
};

// ═══════════════════════════════════════════════════════════════════════
// 7 ЖЕЛЕЗНЫХ ПРАВИЛ ПРОД ДЕПЛОЯ
// ═══════════════════════════════════════════════════════════════════════

const PROD_RULES = {

  PR1_ISOLATION: `
    ПРАВИЛО 1: ИЗОЛЯЦИЯ (АБСОЛЮТНАЯ)
    На сервере работает СУЩЕСТВУЮЩЕЕ приложение. ЗАПРЕЩЕНО:
    - Останавливать/перезапускать/удалять ЧУЖИЕ контейнеры
    - Менять чужой nginx, файрвол, .env, cron
    - Занимать занятые порты
    - docker system prune / volume prune глобально
    Разрешено ТОЛЬКО: /root/monitoring/, контейнеры monitoring-*,
    сеть monitoring-net, порты 8088/9091/3300 на 127.0.0.1.`,

  PR2_LOCALHOST_ONLY: `
    ПРАВИЛО 2: ТОЛЬКО 127.0.0.1
    ВСЕ сервисы мониторинга биндятся на 127.0.0.1 (не 0.0.0.0!).
    Проверка после деплоя: ss -tlnp | grep -E '8088|9091|3300'
    → все слушающие сокеты должны быть на 127.0.0.1, НЕ на 0.0.0.0/*/::.
    Доступ извне — только SSH-туннель или одобренный VPN.`,

  PR3_BACKUP_STATE: `
    ПРАВИЛО 3: ФИКСАЦИЯ СОСТОЯНИЯ ПЕРЕД ДЕПЛОЕМ
    Перед ЛЮБЫМ изменением зафиксировать:
    - docker ps (полный список контейнеров)
    - Образы monitoring-* (docker images | grep monitoring)
    - Свободное место на диске
    Это страховка для отката.`,

  PR4_VERIFY_EVERY_STEP: `
    ПРАВИЛО 4: ВЕРИФИКАЦИЯ КАЖДОГО ШАГА
    Каждый этап завершается проверкой:
    - rsync → файлы на месте (spot-check)
    - build → образы собраны
    - up → контейнеры healthy
    - verify → /health, /metrics, Prometheus targets, Grafana API
    Провал любого шага → СТОП + ОТКАТ.`,

  PR5_AUTO_ROLLBACK: `
    ПРАВИЛО 5: АВТОМАТИЧЕСКИЙ ОТКАТ
    При провале healthcheck/verify:
    1. docker compose -p monitoring down (только НАШИ контейнеры!)
    2. Запуск предыдущего образа monitoring-app (если был)
    3. Верификация отката
    Откат НЕ требует подтверждения — это автоматическая защита.
    Существующее приложение при откате НЕ затрагивается.`,

  PR6_FULL_REBUILD: `
    ПРАВИЛО 6: ПОЛНАЯ ПЕРЕСБОРКА НАШЕГО СТЕКА
    Каждый деплой: docker compose -p monitoring build --no-cache
    ЗАПРЕЩЕНО: пересобирать только app, не трогая prometheus/grafana конфиги.
    Конфиги Prometheus и дашборды Grafana версионируются в репозитории
    и провижинятся автоматически при пересоздании контейнеров.`,

  PR7_NO_SECRETS_LEAK: `
    ПРАВИЛО 7: СЕКРЕТЫ НЕ СИНХРОНИЗИРУЮТСЯ
    .env НИКОГДА не попадает на сервер через rsync (в exclude).
    .env создаётся/обновляется МАНУАЛЬНО на VPS (chmod 600).
    Токены Метрики/Вебмастера, пароль Grafana — только там.
    Пароли из чата НЕ записывать в логи и файлы репозитория.`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 0: PRE-FLIGHT
// ═══════════════════════════════════════════════════════════════════════

const PREFLIGHT_STAGE = {
  role: "DEVOPS",
  icon: "🔍",
  name: "PRE-FLIGHT CHECK",

  steps: [
    {
      id: "PF1",
      name: "Проверка SSH доступности",
      action: `ssh -o ConnectTimeout=10 -o BatchMode=yes root@130.49.129.241 "echo OK"
        При первом коннекте сверить отпечаток хост-ключа с SHA256:g3xVlty76Op1vIbTuwy+8M+0ZUXx4XgtQX2+YrTtbLY.
        Если хост недоступен → деплой НЕ начинается.`,
      critical: true,
    },
    {
      id: "PF2",
      name: "Фиксация текущего состояния сервера",
      action: `Зафиксировать для контроля изоляции:
        - docker ps --format '{{.Names}} {{.Image}} {{.Status}}' (ВСЕ контейнеры)
        - docker images | grep monitoring (наши образы — для отката)
        Это PREVIOUS_STATE. После деплоя сверить: чужие контейнеры не изменились.`,
      critical: true,
    },
    {
      id: "PF3",
      name: "Проверка портов",
      action: `ssh root@VPS "ss -tlnp | grep -E ':(8088|9091|3300) '"
        Возможные результаты:
        - Пусто → порты свободны, ОК
        - Занято НАШИМИ контейнерами (monitoring-*) → ОК (будет force-recreate)
        - Занято ЧУЖИМ процессом → СТОП! Деплой НЕ начинается,
          сообщить пользователю, предложить другие порты`,
      critical: true,
    },
    {
      id: "PF4",
      name: "Проверка диска",
      action: `ssh root@VPS "df -m /root | tail -1 | awk '{print \$4}'"
        Если свободно < 1GB → предупреждение.
        Если < 500MB → деплой НЕ начинается.`,
      critical: true,
    },
    {
      id: "PF5",
      name: "Проверка .env на VPS",
      action: `ssh root@VPS "test -f /root/monitoring/.env && stat -c '%a' /root/monitoring/.env"
        .env должен существовать с правами 600.
        Если отсутствует → СТОП, попросить пользователя создать .env на VPS
        (токены Метрики/Вебмастера, GRAFANA_ADMIN_PASSWORD).`,
      critical: true,
    },
    {
      id: "PF6",
      name: "Проверка Docker и Compose",
      action: `ssh root@VPS "docker --version && docker compose version"
        Если compose plugin отсутствует → СТОП + сообщить пользователю
        (НЕ устанавливать пакеты без согласования — правило изоляции!).`,
      critical: true,
    },
  ],

  output: `Pre-flight report:
    ✅/❌ SSH: доступен (хост-ключ сверен)
    ✅/❌ Порты 8088/9091/3300: свободны или наши
    ✅/❌ Диск: X MB free
    ✅/❌ .env: на месте (600)
    ✅/❌ Docker Compose: доступен
    📸 Состояние сервера зафиксировано (PREVIOUS_STATE)`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 1: СИНХРОНИЗАЦИЯ КОДА (Sync)
// ═══════════════════════════════════════════════════════════════════════

const SYNC_STAGE = {
  role: "DEVOPS",
  icon: "🚚",
  name: "SYNC",

  steps: [
    {
      id: "SY1",
      name: "Подготовка директории на VPS",
      action: `ssh root@VPS "mkdir -p /root/monitoring"
        Директория только наша. Ничего чужого не трогаем.`,
      critical: true,
    },
    {
      id: "SY2",
      name: "rsync проекта",
      action: `rsync -avz --delete \\
          --exclude='.git' --exclude='__pycache__' --exclude='.venv' \\
          --exclude='.env' --exclude='*.pyc' --exclude='.pytest_cache' \\
          --exclude='.mypy_cache' --exclude='node_modules' --exclude='.ruff_cache' \\
          ./ root@130.49.129.241:/root/monitoring/
        ВАЖНО: --exclude='.env' ОБЯЗАТЕЛЕН (секреты не синхронизируются!)
        ВАЖНО: --delete аккуратно — применимо только внутри /root/monitoring/`,
      critical: true,
    },
    {
      id: "SY3",
      name: "Проверка целостности",
      action: `Spot-check ключевых файлов на VPS:
        ssh root@VPS "test -f /root/monitoring/docker-compose.yml && \\
                      test -f /root/monitoring/Dockerfile && \\
                      ls /root/monitoring/collectors/ | head -5"
        Если файлов нет → СТОП, диагностика rsync.`,
      critical: true,
    },
  ],

  output: `Sync report:
    📦 Код синхронизирован в /root/monitoring/
    🔒 .env НЕ синхронизирован (правильно)
    ✅ Целостность проверена`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 2: СБОРКА (Build)
// ═══════════════════════════════════════════════════════════════════════

const BUILD_STAGE = {
  role: "DEVOPS",
  icon: "🏗️",
  name: "BUILD",

  steps: [
    {
      id: "BD1",
      name: "Полная сборка на VPS",
      action: `ssh root@VPS "cd /root/monitoring && docker compose -p monitoring build --no-cache"
        Собираются ВСЕ сервисы с Dockerfile (monitoring-app).
        Prometheus/Grafana используют готовые образы + провижининг конфигов.
        Таймаут: 15 минут.`,
      critical: true,
    },
    {
      id: "BD2",
      name: "Проверка сборки",
      action: `ssh root@VPS "docker images | grep monitoring"
        Образ monitoring-app должен быть свежим (минуту назад).
        Если сборка провалилась → СТОП + полный лог ошибки пользователю.`,
      critical: true,
    },
  ],

  output: `Build report:
    🏗️ monitoring-app: собран (без кеша)
    ✅ Готов к запуску`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 3: ДЕПЛОЙ (Deploy)
// ═══════════════════════════════════════════════════════════════════════

const DEPLOY_STAGE = {
  role: "DEVOPS",
  icon: "🚀",
  name: "DEPLOY",

  steps: [
    {
      id: "DP1",
      name: "Запуск стека",
      action: `ssh root@VPS "cd /root/monitoring && docker compose -p monitoring up -d --force-recreate"
        Пересоздаются ТОЛЬКО контейнеры проекта monitoring
        (compose изоляция по project name).
        Чужие контейнеры НЕ затрагиваются.`,
      critical: true,
    },
    {
      id: "DP2",
      name: "Ожидание healthcheck",
      action: `Опрос каждые 10 сек, таймаут 60 сек:
        ssh root@VPS "docker compose -p monitoring ps"
        Условие: monitoring-app, prometheus, grafana → healthy (или running + отвечают).
        Если таймаут → ОТКАТ.`,
      critical: true,
      rollback_trigger: true,
    },
    {
      id: "DP3",
      name: "Проверка логов",
      action: `ssh root@VPS "cd /root/monitoring && docker compose -p monitoring logs --tail=50"
        Искать: Traceback, error, fatal, ECONNREFUSED, OOM.
        Если найдены критичные → ОТКАТ.`,
      critical: true,
      rollback_trigger: true,
    },
    {
      id: "DP4",
      name: "Проверка биндинга на 127.0.0.1",
      action: `ssh root@VPS "ss -tlnp | grep -E ':(8088|9091|3300) '"
        ВСЕ строки должны содержать 127.0.0.1:PORT.
        Если найден 0.0.0.0 или [::] → КРИТИЧНО (безопасность!) → фикс + ОТКАТ.`,
      critical: true,
      rollback_trigger: true,
    },
  ],

  output: `Deploy report:
    🚀 monitoring-app: Up (healthy) на 127.0.0.1:8088
    🚀 prometheus: Up (healthy) на 127.0.0.1:9091
    🚀 grafana: Up (healthy) на 127.0.0.1:3300
    🔒 Все сокеты на 127.0.0.1
    ✅ Чужие контейнеры не тронуты`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 4: ВЕРИФИКАЦИЯ (Verify)
// ═══════════════════════════════════════════════════════════════════════

const VERIFY_STAGE = {
  role: "DEVOPS",
  icon: "✅",
  name: "VERIFY",

  steps: [
    {
      id: "VF1",
      name: "App health + метрики",
      action: `ssh root@VPS:
        curl -sf http://127.0.0.1:8088/health → {"status":"ok"}
        curl -s http://127.0.0.1:8088/metrics | grep -c '^monitoring_'
        → должно быть > 0 (наши кастомные метрики присутствуют)`,
      critical: true,
    },
    {
      id: "VF2",
      name: "Prometheus",
      action: `ssh root@VPS:
        curl -sf http://127.0.0.1:9091/-/healthy → OK
        curl -s http://127.0.0.1:9091/api/v1/targets | python3 -c "
          import sys,json; d=json.load(sys.stdin);
          print([t['scrapeUrl'] for t in d['data']['activeTargets']])"
        → target monitoring-app:8088/metrics должен быть UP`,
      critical: true,
    },
    {
      id: "VF3",
      name: "Grafana",
      action: `ssh root@VPS:
        curl -sf http://127.0.0.1:3300/api/health → {"database":"ok"}
        Проверить загрузку дашбордов:
        curl -s -u admin:"$GRAFANA_ADMIN_PASSWORD" http://127.0.0.1:3300/api/search?type=dash-db
        → дашборды из grafana/dashboards/ присутствуют.
        Проверить что anonymous-доступ отключён (401 без авторизации).`,
      critical: true,
    },
    {
      id: "VF4",
      name: "Появление данных",
      action: `Подождать 1-2 цикла скрейпа, затем:
        curl -s 'http://127.0.0.1:9091/api/v1/query?query=up' → проверить значение 1
        curl -s 'http://127.0.0.1:9091/api/v1/query?query=monitoring_collector_success' → наличие данных
        Если данных нет дольше 5 минут → WARN (возможно, токены/лимиты API).`,
      critical: false,
    },
    {
      id: "VF5",
      name: "Изоляция существующего приложения",
      action: `Сверить с PREVIOUS_STATE:
        docker ps --format '{{.Names}} {{.Status}}'
        → ВСЕ чужие контейнеры в том же состоянии (Up, тот же uptime-класс)
        → Появились/пересоздались только monitoring-*
        Если чужой контейнер изменился → КРИТИЧНО, сообщить пользователю немедленно.`,
      critical: true,
    },
    {
      id: "VF6",
      name: "Файрвол",
      action: `ssh root@VPS:
        Проверить что наши порты НЕ открыты наружу:
        ss -tlnp | grep -E ':(8088|9091|3300) ' → только 127.0.0.1
        Проверить fail2ban (если установлен): fail2ban-client status
        Если файрвол не настроен → WARN + предложить задачу через конвейер.`,
      critical: false,
    },
  ],

  output: `Verification report:
    ✅ App /health → 200, /metrics содержит monitoring_*
    ✅ Prometheus: healthy, target UP
    ✅ Grafana: healthy, дашборды загружены, anonymous off
    ⚠️/✅ Данные в Prometheus появляются
    ✅ Чужие контейнеры: НЕ затронуты
    ⚠️/✅ Файрвол/fail2ban`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП 5: SMOKE-ТЕСТ
// ═══════════════════════════════════════════════════════════════════════

const SMOKE_STAGE = {
  role: "DEVOPS",
  icon: "🧪",
  name: "SMOKE TEST",

  description: `
    Проверка работоспособности ПОСЛЕ деплоя.
    Выполняется на VPS (curl) + подсказка пользователю для проверки через туннель.`,

  steps: [
    {
      id: "SM1",
      name: "Автосмок на VPS",
      action: `ssh root@VPS:
        # 1. Health
        curl -sf http://127.0.0.1:8088/health || echo "FAIL: /health"
        # 2. Метрики коллекторов
        curl -s http://127.0.0.1:8088/metrics | grep monitoring_collector | head -10
        # 3. Данные в Prometheus
        curl -s 'http://127.0.0.1:9091/api/v1/query?query=monitoring_collector_success' | grep -o '"value":[^}]*' | head -5`,
      critical: true,
    },
    {
      id: "SM2",
      name: "Чеклист для пользователя (через SSH-туннель)",
      action: `Вывести пользователю:
        📋 Открой туннель: ssh -L 3300:127.0.0.1:3300 -L 8088:127.0.0.1:8088 root@130.49.129.241
        □ Grafana: http://localhost:3300 → логин, дашборд открывается
        □ Дашборд по сайту: панели с данными (трафик, uptime)
        □ App: http://localhost:8088/health → {"status":"ok"}
        □ Выбрать сайт в dropdown дашборда → данные меняются
        Задать вопрос: «Всё отображается?» (Да/Нет)
        Если «Нет» → постдеплойный рестарт конвейера.`,
      manual: true,
    },
  ],

  verdict_rules: `
    GO:      Все critical пройдены + пользователь подтвердил отображение
    COND:    critical пройдены, данные ещё наполняются (метрики копятся)
    NO-GO:   critical провален → АВТОМАТИЧЕСКИЙ ОТКАТ`,
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП ФИНАЛИЗАЦИИ
// ═══════════════════════════════════════════════════════════════════════

const FINALIZE_STAGE = {
  role: "DEVOPS",
  icon: "📋",
  name: "FINALIZE",

  steps: [
    {
      id: "FN1",
      name: "Очистка старых образов (только наших)",
      action: `ssh root@VPS:
        docker images --format '{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}' | grep monitoring
        Удалить образы monitoring-app старше 5 деплоев назад (оставить текущий + 4).
        ЗАПРЕЩЕНО: docker image prune -a / system prune (затронет чужое!)`,
      critical: false,
    },
    {
      id: "FN2",
      name: "Deployment Report",
      action: `Вывести итоговый отчёт:
        ═══════════════════════════════════════════
        🎉 PRODUCTION DEPLOYMENT SUCCESSFUL
        ═══════════════════════════════════════════
        📦 Сервисы: monitoring-app (8088), prometheus (9091), grafana (3300)
        🔒 Биндинг: 127.0.0.1 (доступ по SSH-туннелю)
        🛡️  Существующее приложение: НЕ затронуто
        📊 Данные: собираются (Метрика/Вебмастер/uptime/servers)
        🌐 Доступ: ssh -L 3300:127.0.0.1:3300 -L 8088:127.0.0.1:8088 root@130.49.129.241
        ═══════════════════════════════════════════`,
      critical: true,
    },
  ],
};

// ═══════════════════════════════════════════════════════════════════════
// ЭТАП ОТКАТА (Rollback)
// ═══════════════════════════════════════════════════════════════════════

const ROLLBACK_STAGE = {
  role: "DEVOPS",
  icon: "🔄",
  name: "ROLLBACK",

  trigger: `
    Автоматический откат запускается при:
    - Healthcheck контейнеров провалился (DP2)
    - Критичные ошибки в логах (DP3)
    - Сервис забиндился на 0.0.0.0 (DP4)
    - Verify NO-GO (VF1-VF3, VF5)

    Ручной откат: «откат», «rollback», «верни предыдущую версию»`,

  steps: [
    {
      id: "RB1",
      name: "Остановка НАШИХ контейнеров",
      action: `ssh root@VPS "cd /root/monitoring && docker compose -p monitoring down"
        Останавливаются ТОЛЬКО контейнеры проекта monitoring.
        Чужое приложение продолжает работать — оно и не останавливалось.`,
      critical: true,
    },
    {
      id: "RB2",
      name: "Восстановление предыдущего образа",
      action: `Если существовал предыдущий рабочий образ monitoring-app:
        1. Найти: ssh root@VPS "docker images | grep monitoring-app" (по дате)
        2. retag/указать в compose или docker run из предыдущего образа
        3. Запустить: docker compose -p monitoring up -d
        Если предыдущего образа нет (первый деплой) → просто сообщить:
        откат = стек остановлен, сервер в состоянии до деплоя.`,
      critical: true,
    },
    {
      id: "RB3",
      name: "Верификация отката",
      action: `curl -sf http://127.0.0.1:8088/health (если стек поднят)
        docker ps → наши контейнеры в состоянии до деплоя
        docker ps → чужие контейнеры не изменились (сверка с PREVIOUS_STATE)`,
      critical: true,
    },
    {
      id: "RB4",
      name: "Отчёт об откате",
      action: `Вывести:
        🔄 ОТКАТ ВЫПОЛНЕН
        Причина: [какой шаг провалился]
        Состояние: [стек остановлен / предыдущая версия]
        Существующее приложение: работает
        Следующий шаг: диагностика проблемы через конвейер (Этап 1 Аналитик)`,
      critical: true,
    },
  ],
};
