"""Валидация Grafana-дашбордов ЭПИК-3: структура JSON и корректность PromQL."""

import json
import re
from pathlib import Path
from typing import Any

from prometheus_client import Counter, Gauge

from app import metrics

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"

DASHBOARD_NAMES = ("site-overview.json", "all-sites.json")

# Белый список собираем из реестра приложения (app/metrics.py),
# чтобы тест не разошёлся с реальными метриками.
KNOWN_METRICS: frozenset[str] = frozenset(
    str(m._name) for m in vars(metrics).values() if isinstance(m, (Counter, Gauge))
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
    for name in DASHBOARD_NAMES:
        dashboard = load_dashboard(name)
        assert dashboard["uid"], f"{name}: пустой uid"
        assert dashboard["uid"] not in uids
        uids.add(dashboard["uid"])
        assert dashboard["title"], f"{name}: пустой title"
        assert isinstance(dashboard["schemaVersion"], int)
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


def test_no_rate_or_increase() -> None:
    # Все метрики проекта — gauge; rate()/increase() недопустимы
    for name in DASHBOARD_NAMES:
        for expr in panel_exprs(load_dashboard(name)):
            assert "rate(" not in expr and "increase(" not in expr, f"{name}: {expr!r}"


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

    for name in DASHBOARD_NAMES:
        walk(load_dashboard(name))


def test_panels_use_provisioned_prometheus_datasource() -> None:
    for name in DASHBOARD_NAMES:
        for panel in load_dashboard(name)["panels"]:
            assert panel["datasource"]["uid"] == "prometheus", f"{name}: панель без datasource"
            for target in panel.get("targets", []):
                assert target["datasource"]["uid"] == "prometheus", f"{name}: target без datasource"


def test_site_overview_structure() -> None:
    dashboard = load_dashboard("site-overview.json")
    assert len(dashboard["panels"]) == 8
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
    }
    var = dashboard["templating"]["list"][0]
    assert var["name"] == "site"
    assert var["type"] == "query"
    assert var["multi"] is False
    assert var["includeAll"] is False
    assert "label_values" in str(var["query"]["query"])


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
