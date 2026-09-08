"""Валидация Grafana-дашбордов ЭПИК-3: структура JSON и корректность PromQL."""

import json
import re
from pathlib import Path
from typing import Any

from prometheus_client import Counter, Gauge

from app import metrics
from app.webmaster_export import METRIC_NAMES

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

DASHBOARD_NAMES = (
    "site-overview.json",
    "all-sites.json",
    "site-business.json",
    "webmaster.json",
)

# Персайтные дашборды бизнес-метрик (ЧТЗ_Персайтные_дашборды_бизнес-метрик):
# файл → домен. Метрики приложения сайта (не monitoring_*) фиксируются
# структурным тестом; каждый expr обязан фильтроваться по site="<домен>".
PER_SITE_DASHBOARDS: dict[str, str] = {
    "site-zabor-analytics.json": "zabor-i-naves.ru",
}

# Белый список собираем из реестра приложения (app/metrics.py) и имён
# рендера из БД (app/webmaster_export.py — ADR-011), чтобы тест не
# разошёлся с реальными метриками.
KNOWN_METRICS: frozenset[str] = (
    frozenset(str(m._name) for m in vars(metrics).values() if isinstance(m, (Counter, Gauge)))
    | METRIC_NAMES
)

METRIC_RE = re.compile(r"monitoring_[a-z_]+")


def load_dashboard(name: str) -> dict[str, Any]:
    path = DASHBOARDS_DIR / name
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def panel_exprs(dashboard: dict[str, Any]) -> list[str]:
    return [
        str(target["expr"])
        for panel in dashboard.get("panels", [])
        for target in panel.get("targets", [])
        if target.get("expr")
    ]


def variable_queries(dashboard: dict[str, Any]) -> list[str]:
    queries = []
    for var in dashboard.get("templating", {}).get("list", []):
        query: Any = var.get("query")
        if isinstance(query, str):
            queries.append(query)
        elif isinstance(query, dict):
            queries.append(str(query.get("query", "")))
    return queries


def test_dashboards_valid_json_with_required_fields() -> None:
    uids = set()
    for name in DASHBOARD_NAMES + tuple(PER_SITE_DASHBOARDS):
        dashboard = load_dashboard(name)
        assert dashboard["uid"], f"{name}: пустой uid"
        assert dashboard["uid"] not in uids
        uids.add(dashboard["uid"])
        assert dashboard["title"], f"{name}: пустой title"
        assert isinstance(dashboard["schemaVersion"], int)
        if name in DASHBOARD_NAMES:
            assert dashboard["templating"]["list"], f"{name}: нет переменных"


def test_all_expr_metrics_are_known() -> None:
    for name in DASHBOARD_NAMES:
        dashboard = load_dashboard(name)
        for expr in panel_exprs(dashboard) + variable_queries(dashboard):
            for metric in METRIC_RE.findall(expr):
                assert metric in KNOWN_METRICS, f"{name}: неизвестная метрика {metric!r} в {expr!r}"


def test_exprs_filter_by_site_variable() -> None:
    for name in DASHBOARD_NAMES:
        for expr in panel_exprs(load_dashboard(name)):
            assert 'site="$site"' in expr or 'site=~"$site"' in expr, (
                f"{name}: expr без фильтра site: {expr!r}"
            )


def test_no_rate_or_increase_on_project_metrics() -> None:
    # Метрики проекта monitoring_* — gauge: rate()/increase() недопустимы.
    # sum_over_time по gauge суммирует семплы скрейпов — завышение (ADR-008 D3).
    # node_* (node_exporter, ЭПИК-6) — counter: rate() для них разрешён (ADR-005).
    # Счётчики приложения сайта (персайтные дашборды) — counter: rate() разрешён.
    for name in DASHBOARD_NAMES + tuple(PER_SITE_DASHBOARDS):
        for expr in panel_exprs(load_dashboard(name)):
            for forbidden in ("rate(", "increase(", "sum_over_time("):
                if forbidden in expr:
                    assert "monitoring_" not in expr, f"{name}: {expr!r}"


def test_threshold_steps_sorted_ascending() -> None:
    # Регрессия BUG-001: Grafana требует возрастающие значения шагов порогов

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            thresholds = obj.get("thresholds")
            if isinstance(thresholds, dict) and isinstance(thresholds.get("steps"), list):
                values = [step["value"] for step in thresholds["steps"]]
                numeric = [value for value in values if value is not None]
                assert numeric == sorted(numeric), f"пороги не по возрастанию: {values}"
            for child in obj.values():
                walk(child)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    for name in DASHBOARD_NAMES + tuple(PER_SITE_DASHBOARDS):
        walk(load_dashboard(name))


def test_panels_use_provisioned_prometheus_datasource() -> None:
    for name in DASHBOARD_NAMES + tuple(PER_SITE_DASHBOARDS):
        for panel in load_dashboard(name)["panels"]:
            assert panel["datasource"]["uid"] == "prometheus", f"{name}: панель без datasource"
            for target in panel.get("targets", []):
                if target.get("expr"):
                    ds = target["datasource"]["uid"]
                    assert ds == "prometheus", f"{name}: target без datasource"


def test_per_site_dashboards_filter_by_site_label() -> None:
    """Персайтные дашборды: каждый expr фильтруется по site="<домен>" (ЧТЗ)."""
    for name, site in PER_SITE_DASHBOARDS.items():
        dashboard = load_dashboard(name)
        exprs = panel_exprs(dashboard)
        assert exprs, f"{name}: нет expr"
        for expr in exprs:
            assert f'site="{site}"' in expr, f"{name}: expr без фильтра site: {expr!r}"


def test_site_zabor_analytics_structure() -> None:
    """Персайтный дашборд zabor: 9 панелей; статистика — абсолютные счётчики.

    increase() обнуляется после подключения (наработка счётчиков ДО старта
    скрейпа Prometheus невидима — ЧТЗ_Дашборд_User_Analytics_zabor_Абсолютные_Значения),
    поэтому increase в expr дашборда запрещён; rate() на live-панелях разрешён.
    """
    dashboard = load_dashboard("site-zabor-analytics.json")
    assert dashboard["uid"] == "site-zabor-analytics"
    assert len(dashboard["panels"]) == 9
    by_type = {panel["type"] for panel in dashboard["panels"]}
    assert "piechart" in by_type
    assert "barchart" in by_type
    exprs = "\n".join(panel_exprs(dashboard))
    for metric in (
        "analytics_events_total",
        "page_views_total",
        "calculator_events_total",
        "conversion_funnel_total",
    ):
        assert metric in exprs, metric
    assert "increase(" not in exprs, (
        "site-zabor-analytics: increase() обнуляется после подключения — "
        "использовать абсолютные значения счётчиков"
    )


def test_site_overview_structure() -> None:
    dashboard = load_dashboard("site-overview.json")
    assert len(dashboard["panels"]) == 12
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert titles == {
        "Доступность",
        "HTTP-код",
        "Дней до истечения SSL",
        "Здоровье коллекторов",
        "Время ответа",
        "Визиты (нарастающий итог дня)",
        "Посетители (нарастающий итог дня)",
        "Длительность сбора коллекторов",
        "CPU, %",
        "RAM, %",
        "Диск /, %",
        "Load average (1m)",
    }
    var = dashboard["templating"]["list"][0]
    assert var["name"] == "site"
    assert var["type"] == "query"
    assert var["multi"] is False
    assert var["includeAll"] is False
    assert "label_values" in str(var["query"]["query"])


def test_site_overview_no_webmaster_panels() -> None:
    """Секция Вебмастера перенесена в единый дашборд (ЧТЗ_Перенос_Вебмастер_из_Обзора)."""
    for expr in panel_exprs(load_dashboard("site-overview.json")):
        assert "monitoring_search" not in expr, expr


def test_site_overview_server_panels() -> None:
    # ЭПИК-6: серверные панели используют node_exporter-метрики с фильтром site
    dashboard = load_dashboard("site-overview.json")
    by_title = {panel["title"]: panel for panel in dashboard["panels"]}
    cpu = by_title["CPU, %"]["targets"][0]["expr"]
    assert "node_cpu_seconds_total" in cpu and 'site="$site"' in cpu
    ram = by_title["RAM, %"]["targets"][0]["expr"]
    assert "node_memory_MemAvailable_bytes" in ram and "node_memory_MemTotal_bytes" in ram
    disk = by_title["Диск /, %"]["targets"][0]["expr"]
    assert "node_filesystem_avail_bytes" in disk and "node_filesystem_size_bytes" in disk
    load_avg = by_title["Load average (1m)"]["targets"][0]["expr"]
    assert load_avg == 'node_load1{site="$site"}'


def test_all_sites_structure() -> None:
    dashboard = load_dashboard("all-sites.json")
    tables = [panel for panel in dashboard["panels"] if panel["type"] == "table"]
    assert len(tables) == 1
    assert len(tables[0]["targets"]) == 6
    # Таблица: instant-запросы в табличном формате
    for target in tables[0]["targets"]:
        assert target["format"] == "table"
        assert target["instant"] is True
    # Спарклайны: повторяемая по site компактная панель
    repeated = [panel for panel in dashboard["panels"] if panel.get("repeat") == "site"]
    assert len(repeated) == 1
    assert repeated[0]["type"] == "timeseries"
    var = dashboard["templating"]["list"][0]
    assert var["name"] == "site"
    assert var["multi"] is True
    assert var["includeAll"] is True


def test_site_business_structure() -> None:
    """ЭПИК-9 (ADR-007 D7) + ADR-012: дашборд «Бизнес сайта» — 16 панелей."""
    dashboard = load_dashboard("site-business.json")
    assert dashboard["uid"] == "site-business"
    assert len(dashboard["panels"]) == 16
    titles = {panel["title"] for panel in dashboard["panels"]}
    assert titles == {
        "Активные сессии (30 мин)",
        "Посетители (24ч)",
        "Лиды (заявки/клики)",
        "Конверсия в лиды (24ч)",
        "Отказы (24ч)",
        "Эндпоинты метрик",
        "Просмотры страниц",
        "Сессии и вовлечённость",
        "События по типам (24ч)",
        "Клики по услугам (топ-10, 24ч)",
        "Источники трафика (24ч)",
        "Гео посетителей (топ-10, 24ч)",
        "Задержка эндпоинтов метрик",
        "Длительность сессии (24ч)",
        "Клики по телефону (12ч)",
        "Клики по телефону (время клика)",
    }
    var = dashboard["templating"]["list"][0]
    assert var["name"] == "site"
    assert var["multi"] is False
    assert var["includeAll"] is False


def test_site_business_phone_click_panels() -> None:
    """ADR-012 + ЧТЗ_Фикс_Клики_по_телефону: панели кликов по телефону.

    Сайты гетерогенны: эвакуация.online отдаёт business_phone_clicks_12h /
    business_phone_clicks_event, zabor-i-naves.ru — счётчик
    analytics_events_total{event_name="phone_click"}. Основная ветка
    приоритетна (`or`), fallback считает клики increase()-ом по счётчику.
    Обе ветки с дедуплицирующей агрегацией и фильтром по переменной site.
    """
    dashboard = load_dashboard("site-business.json")
    by_title = {panel["title"]: panel for panel in dashboard["panels"]}
    stat = by_title["Клики по телефону (12ч)"]
    assert stat["type"] == "stat"
    assert stat["targets"][0]["expr"] == (
        'max by (site) (business_phone_clicks_12h{site="$site"})\n'
        "or\n"
        'sum by (site) (increase(analytics_events_total'
        '{event_name="phone_click", site="$site"}[12h]))'
    )
    graph = by_title["Клики по телефону (время клика)"]
    assert graph["type"] == "timeseries"
    assert graph["targets"][0]["expr"] == (
        'max by (site) (business_phone_clicks_event{site="$site"})\n'
        "or\n"
        "sum by (site) (increase(analytics_events_total"
        '{event_name="phone_click", site="$site"}[$__rate_interval]))'
    )
    # Fallback на случай пропуска основной ветки при копировании expr
    for expr in (stat["targets"][0]["expr"], graph["targets"][0]["expr"]):
        assert "analytics_events_total" in expr, (
            "site-business: панель телефона без fallback на счётчик сайта"
        )
    # Бары в момент клика: drawStyle=bars с полной заливкой
    custom = graph["fieldConfig"]["defaults"]["custom"]
    assert custom["drawStyle"] == "bars"
    assert custom["fillOpacity"] == 100


def test_site_business_panels_use_business_and_site_metrics() -> None:
    """Панели используют метрики сайта (business_*) и health-метрики ЭПИКа-9."""
    exprs = panel_exprs(load_dashboard("site-business.json"))
    joined = "\n".join(exprs)
    for metric in (
        "business_sessions_active",
        "business_page_views_24h",
        "business_page_views_1h",
        "business_unique_visitors_24h",
        "business_sessions_24h",
        "business_avg_session_duration_seconds_24h",
        "business_bounce_rate_24h",
        "business_leads_24h",
        "business_leads_1h",
        "business_conversion_rate_24h",
        "business_events_24h",
        "business_referral_sources_24h",
        "business_geo_visitors_24h",
        "business_service_clicks_24h",
        "business_phone_clicks_12h",
        "business_phone_clicks_event",
    ):
        assert metric in joined, metric
    assert "monitoring_site_metrics_up" in joined
    assert "monitoring_site_metrics_latency_seconds" in joined


def test_site_business_metrics_deduplicated() -> None:
    """Регрессия задвоения серий после смены таргета скрейпа (ADR-007 → ADR-010).

    Селекторы business_* / monitoring_site_metrics_* не должны возвращать
    серии с лейблами идентичности таргета (job/instance): при смене пути
    скрейпа (прямой → relay) старые и новые серии различаются только
    job/instance и задваиваются в каждой панели. Требуется агрегация
    max by (...)/min(...), устраняющая эти лейблы.
    """
    for expr in panel_exprs(load_dashboard("site-business.json")):
        if not ("business_" in expr or "monitoring_site_metrics_" in expr):
            continue
        assert "max by (" in expr or "min(" in expr, (
            f"site-business: expr без дедуплицирующей агрегации: {expr!r}"
        )


def test_webmaster_unified_structure() -> None:
    """Единый дашборд Вебмастера: все панели на одной странице.

    Динамика (ADR-008): сайт = сумма по отслеживаемым запросам (per-query
    history). Топы: instant-таблицы с range-функцией avg_over_time[$period].
    Секция «за последнюю неделю» перенесена из «Обзора сайта»
    (ЧТЗ_Перенос_Вебмастер_из_Обзора).
    """
    dashboard = load_dashboard("webmaster.json")
    assert dashboard["uid"] == "webmaster"
    assert len(dashboard["panels"]) == 11
    by_title = {panel["title"]: panel for panel in dashboard["panels"]}
    assert set(by_title) == {
        "Показы по дням",
        "Клики по дням",
        "Топ-5 запросов по позиции (лучшие)",
        "Топ-5 запросов по кликам",
        "Топ-5 запросов по показам",
        "Поиск (Яндекс.Вебмастер, за последнюю неделю)",
        "Клики за неделю",
        "Показы за неделю",
        "",
        "Топ-15 поисковых запросов по кликам",
        "Позиция в поиске (меньше — лучше)",
    }

    # Динамика: timeseries, сайт = сумма по отслеживаемым запросам (ADR-008)
    assert by_title["Показы по дням"]["type"] == "timeseries"
    assert by_title["Клики по дням"]["type"] == "timeseries"
    assert (
        by_title["Показы по дням"]["targets"][0]["expr"]
        == 'sum(monitoring_search_daily_shows{site="$site"})'
    )
    assert (
        by_title["Клики по дням"]["targets"][0]["expr"]
        == 'sum(monitoring_search_daily_clicks{site="$site"})'
    )

    # Топы: instant-таблицы с [$period]
    for title in (
        "Топ-5 запросов по позиции (лучшие)",
        "Топ-5 запросов по кликам",
        "Топ-5 запросов по показам",
    ):
        assert by_title[title]["type"] == "table", title
        target = by_title[title]["targets"][0]
        assert target["format"] == "table", title
        assert target["instant"] is True, title
        assert "[$period]" in target["expr"], title
        assert "avg_over_time(" in target["expr"], title

    position = by_title["Топ-5 запросов по позиции (лучшие)"]["targets"][0]["expr"]
    assert position == (
        'bottomk(5, avg_over_time(monitoring_search_position{site="$site"}[$period]))'
    )
    clicks = by_title["Топ-5 запросов по кликам"]["targets"][0]["expr"]
    assert clicks == (
        'topk(5, avg_over_time(monitoring_search_clicks_total{site="$site"}[$period]))'
    )
    shows = by_title["Топ-5 запросов по показам"]["targets"][0]["expr"]
    assert shows == ('topk(5, avg_over_time(monitoring_search_shows_total{site="$site"}[$period]))')
    # Колонки таблиц человекочитаемы
    renames = by_title["Топ-5 запросов по кликам"]["transformations"][0]["options"]["renameByName"]
    assert renames == {"query": "Запрос", "Value": "Клики/нед (среднее за период)"}

    # Секция «за последнюю неделю» (перенос из «Обзора сайта»)
    row = by_title["Поиск (Яндекс.Вебмастер, за последнюю неделю)"]
    assert row["type"] == "row"
    stats = by_title["Клики за неделю"]
    assert stats["type"] == "stat"
    assert stats["targets"][0]["expr"] == 'sum(monitoring_search_clicks_total{site="$site"})'
    assert stats["targets"][0]["instant"] is True
    assert by_title["Показы за неделю"]["targets"][0]["expr"] == (
        'sum(monitoring_search_shows_total{site="$site"})'
    )
    text = by_title[""]
    assert text["type"] == "text"
    assert "Яндекс.Вебмастера" in text["options"]["content"]
    top15 = by_title["Топ-15 поисковых запросов по кликам"]
    assert top15["type"] == "table"
    top15_target = top15["targets"][0]
    assert top15_target["format"] == "table"
    assert top15_target["instant"] is True
    assert top15_target["expr"] == 'topk(15, monitoring_search_clicks_total{site="$site"})'
    assert top15["transformations"][0]["options"]["renameByName"] == {
        "query": "Запрос",
        "Value": "Клики",
    }
    position_panel = by_title["Позиция в поиске (меньше — лучше)"]
    assert position_panel["type"] == "timeseries"
    assert {target["expr"] for target in position_panel["targets"]} == {
        'avg(monitoring_search_position{site="$site"})',
        'min(monitoring_search_position{site="$site"})',
    }
    # Одна точка данных должна быть видна: showPoints != never
    position_custom = position_panel["fieldConfig"]["defaults"]["custom"]
    assert position_custom["showPoints"] in ("auto", "always")

    # Единый переключатель сайта для всех панелей
    variables = {var["name"]: var for var in dashboard["templating"]["list"]}
    site = variables["site"]
    assert site["multi"] is False
    assert site["includeAll"] is False
    assert "label_values(monitoring_search_clicks_total, site)" in str(site["query"])
    period = variables["period"]
    assert period["type"] == "custom"
    assert [(option["text"], option["value"]) for option in period["options"]] == [
        ("Неделя", "7d"),
        ("Месяц", "30d"),
        ("Год", "365d"),
    ]
    # Пометка о приближённости срезов месяц/год (ADR-008 D3)
    assert "приближение" in dashboard["description"]


def test_webmaster_old_dashboards_removed() -> None:
    """Старые дашборды Вебмастера удалены — единая страница вместо двух."""
    remaining = {
        "webmaster-dynamics.json",
        "webmaster-top-queries.json",
    } & {path.name for path in DASHBOARDS_DIR.glob("*.json")}
    assert not remaining, f"устаревшие дашборды не удалены: {remaining}"
