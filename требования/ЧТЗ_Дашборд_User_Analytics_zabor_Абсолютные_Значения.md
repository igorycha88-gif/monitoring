# ЧТЗ: Переработка дашборда «User Analytics — zabor-i-naves.ru» (абсолютные значения вместо нулевых rate/increase)

> Маршрут 1: Аналитик → Разработчик → Тестировщик → DevOps.
> Постдеплойный рестарт №2 (после фикса ключа site-metrics). Дата: 2026-08-24.

## 1. Проблема

После подключения метрик сайта (ключ site-metrics исправлен, job `site-content`
скрейпит `analytics_events_total` и др.) дашборд «User Analytics —
zabor-i-naves.ru» визуально пуст («no data»/нули) и неинформативен.

## 2. Диагностика (прод, 2026-08-24)

- Данные ЕСТЬ и текут: `analytics_events_total` — 10 серий (job site-content),
  свежие семплы; Grafana proxy возвращает серии по всем expr панелей.
- ВСЕ значения = **0**: панели используют только `rate(...[5m])` /
  `increase(...[1h|24h])`. Счётчики сайта пришли с накопленными значениями
  (page_view=1351, calculator_open=1146 и т.д. — наработка ДО подключения
  мониторинга), Prometheus видит только прирост ПОСЛЕ старта скрейпа.
  При нулевом приросте круговые/гейдж-панели выглядят «пустыми».
- Доп. фактор: вкладка браузера, открытая до 16:29 (старт скрейпа),
  показывает «No data» до обновления (F5) — не баг, сообщить пользователю.

## 3. Решение

Переработать дашборд: сохранить live-панели на `rate()`, добавить/заменить
статистические панели на **абсолютные значения счётчиков** (информативны
сразу после подключения). PromQL-инварианты проекта сохраняются: каждый
expr фильтруется `site="zabor-i-naves.ru"`, datasource uid `prometheus`,
`rate/increase` только на счётчиках сайта (не `monitoring_*`).

### Структура (9 панелей)

Ряд «Живая активность» (существующие, без изменений):
1. События аналитики (в минуту) — timeseries, `sum by (event_name) (rate(analytics_events_total{site="zabor-i-naves.ru"}[5m]) * 60)`
2. Просмотры страниц (в минуту) — timeseries, `rate(page_views_total{site="zabor-i-naves.ru"}[5m]) * 60`
3. Действия в калькуляторе (в минуту) — timeseries, `rate(calculator_events_total{site="zabor-i-naves.ru"}[5m]) * 60`

Ряд «Суммарная активность с запуска сайта» (новые/заменённые):
4. Распределение событий — piechart: `sum by (event_name) (analytics_events_total{site="zabor-i-naves.ru"})`
5. Воронка конверсии — barchart: `sum by (step) (conversion_funnel_total{site="zabor-i-naves.ru"})`
6. Просмотры страниц (всего) — stat: `sum(page_views_total{site="zabor-i-naves.ru"})`
7. Расчёты в калькуляторе (всего) — stat: `sum(calculator_events_total{site="zabor-i-naves.ru"})`
8. Заявки (всего) — stat: `sum(conversion_funnel_total{site="zabor-i-naves.ru",step="contact_form_submit"})`
9. Конверсия (просмотр → заявка) — stat:
   `sum(conversion_funnel_total{site="zabor-i-naves.ru",step="contact_form_submit"}) / sum(conversion_funnel_total{site="zabor-i-naves.ru",step="page_view"})` (деление на 0 — `NaN` отображается как «No data» на пустом сайте; допустимо, единица 100%)

Удаляемые панели (increase-версии, всегда 0 после подключения):
«Распределение событий за последний час», «Воронка конверсии [1h]»,
«Просмотры за 24ч», «Расчёты в калькуляторе за 24ч», «Заявки за 24ч»,
«Конверсия [24h]» — заменяются п.4–9.

## 4. Изменяемые файлы

1. `grafana/dashboards/site-zabor-analytics.json` — переработка панелей.
2. `tests/test_dashboards.py` — обновить `test_site_zabor_analytics_structure`:
   - 9 панелей, piechart присутствует (как раньше);
   - метрики `analytics_events_total`, `page_views_total`,
     `calculator_events_total`, `conversion_funnel_total` используются;
   - НОВОЕ: в expr дашборда нет `increase(` (lock дизайна: абсолютные
     значения вместо обнуляющихся окон).

## 5. Критерии приёмки

- [ ] `pytest && ruff check . && mypy .` — зелёные
- [ ] Тест структуры дашборда обновлён и проходит
- [ ] На проде (после деплоя) дашборд «User Analytics — zabor-i-naves.ru»:
      п.4–9 показывают ненулевые значения СРАЗУ (1351/1146/… по факту сайта)
- [ ] Live-панели п.1–3 рендерятся (нулевые линии — норма до появления
      живого трафика)
- [ ] Все expr содержат `site="zabor-i-naves.ru"`, datasource `prometheus`
- [ ] `verify_prod.py` → ИТОГО: N OK, 0 FAIL
- [ ] Изоляция: smarttraffic-* не затронуты

## 6. Риски

- Отсутствие данных в панелях live-активности — ожидаемо до роста трафика
  (нулевые линии, не «No data»).
- Абсолютные счётчики обнуляются при рестарте приложения сайта — панель
  покажет просадку; это семантика counter'ов сайта, не мониторинга.

## 7. Маршрутизация

Исполнитель: Разработчик (dashboard JSON + тесты). Далее: Тестировщик →
DevOps (полная пересборка стека, Правило 6).
