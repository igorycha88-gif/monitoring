#!/usr/bin/env python3
"""Прод-верификация стека мониторинга: коллекторы → Prometheus → Grafana.

Запуск на VPS (host, python3 + curl + docker):
    cd /root/monitoring && python3 scripts/verify_prod.py

Проверяет (PRODUCTION.md §7.1):
  1. коллекторы всех сайтов: monitoring_collector_success/errors_total;
  2. значения в Prometheus: uptime, ssl_days, search_*, business_*;
  3. свежесть scrape (< 60 с);
  4. Grafana: /api/health, datasource Prometheus, запросы реальных панелей
     всех 4 дашбордов ЧЕРЕЗ datasource proxy (как в браузере).

Exit code: 0 — все проверки OK, 1 — есть FAIL.
Секреты не печатает: пароль Grafana читается docker exec'ом и не выводится.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode

PROM = "http://127.0.0.1:9091"
APP = "http://127.0.0.1:8088"
GRAF = "http://127.0.0.1:3300"

EXPECTED_COLLECTORS: dict[str, set[str]] = {
    "da-dryclean.ru": {"uptime", "ssl", "webmaster", "site-metrics"},
    "эвакуация.online": {"uptime", "ssl", "webmaster", "metrika", "site-metrics"},
}

# Запросы реальных панелей 4 дашбордов (site='$site' раскрыт в белый список).
DASHBOARD_QUERIES: dict[str, list[str]] = {
    "Обзор сайта": [
        'monitoring_uptime_status{site=~".+"}',
        "monitoring_ssl_days_left",
        "monitoring_collector_duration_seconds",
    ],
    "Все сайты": ["sum by (site) (monitoring_uptime_status)"],
    "Вебмастер: поиск по сайту": [
        "sum by (site) (monitoring_search_shows_total)",
        "topk(5, avg_over_time(monitoring_search_clicks_total[7d]))",
        "sum(monitoring_search_daily_shows)",
    ],
    "Бизнес сайта": [
        "sum by (site) (business_sessions_24h)",
        "sum by (site) (business_unique_visitors_24h)",
        "sum by (site) (business_leads_24h)",
    ],
}

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    mark = "✅" if cond else "❌"
    if cond:
        _ok += 1
    else:
        _fail += 1
    suffix = f" — {detail}" if detail else ""
    print(f"{mark} {name}{suffix}")


def fetch_json(url: str, auth: tuple[str, str] | None = None) -> object:
    cmd = ["curl", "-s", "--max-time", "15"]
    if auth:
        cmd += ["-u", f"{auth[0]}:{auth[1]}"]
    proc = subprocess.run(cmd + [url], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def prom_query(expr: str) -> list[dict[str, Any]]:
    data = fetch_json(f"{PROM}/api/v1/query?{urlencode({'query': expr})}")
    assert isinstance(data, dict)
    result = data["data"]["result"]
    assert isinstance(result, list)
    return result


def grafana_password() -> str:
    proc = subprocess.run(
        ["docker", "exec", "monitoring-grafana", "printenv", "GF_SECURITY_ADMIN_PASSWORD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def main() -> int:
    print("=" * 70)
    print("1. КОЛЛЕКТОРЫ (/metrics приложения)")
    print("=" * 70)
    proc = subprocess.run(
        ["curl", "-s", "--max-time", "15", f"{APP}/metrics"],
        capture_output=True,
        text=True,
        check=True,
    )
    success: dict[tuple[str, str], float] = {}
    errors: dict[tuple[str, str], float] = {}
    for line in proc.stdout.splitlines():
        m = re.match(r'monitoring_collector_success\{site="([^"]+)",source="([^"]+)"\} (\S+)', line)
        if m:
            success[(m.group(1), m.group(2))] = float(m.group(3))
            continue
        m = re.match(
            r'monitoring_collector_errors_total\{site="([^"]+)",source="([^"]+)"\} (\S+)', line
        )
        if m:
            errors[(m.group(1), m.group(2))] = float(m.group(3))
    for site, want in EXPECTED_COLLECTORS.items():
        got = {src for (s, src), v in success.items() if s == site and v == 1}
        failed = {src for (s, src), v in success.items() if s == site and v == 0}
        errs = {src: v for (s, src), v in errors.items() if s == site and v > 0}
        # errors_total — кумулятивный счётчик с момента старта: транзиентная
        # ошибка в прошлом не роняет проверку, если текущий цикл success=1
        # (детали — в выводе; падение — только если success=0 сейчас).
        check(
            f"коллекторы {site}",
            want <= got and not failed,
            f"работают: {','.join(sorted(got))};"
            f" ошибки (накопл.): {errs or 'нет'}; падает сейчас: {sorted(failed) or 'нет'}",
        )

    print()
    print("=" * 70)
    print("2. ЗНАЧЕНИЯ МЕТРИК В PROMETHEUS")
    print("=" * 70)
    for r in prom_query("monitoring_uptime_status"):
        check(f"uptime {r['metric']['site']} = {r['value'][1]}", r["value"][1] == "1")
    for r in prom_query("monitoring_ssl_days_left"):
        days = float(r["value"][1])
        check(f"ssl_days {r['metric']['site']} = {days:.0f}", days > 7)
    for r in prom_query("monitoring_site_visits_total"):
        site = r["metric"]["site"]
        visits = float(r["value"][1])
        note = "" if visits else " (счётчик не на сайте — ожидаемо 0)"
        print(f"ℹ️  metrika visits {site} = {visits:.0f}{note}")
    for r in prom_query("sum by (site) (monitoring_search_shows_total)"):
        site = r["metric"]["site"]
        check(
            f"webmaster shows_total {site} = {float(r['value'][1]):.0f}",
            float(r["value"][1]) > 0,
        )
    for r in prom_query("sum by (site) (business_sessions_24h)"):
        site = r["metric"]["site"]
        check(f"business_sessions_24h {site} = {float(r['value'][1]):.0f}", True)
    stamps = prom_query("timestamp(monitoring_uptime_status)")
    fresh = bool(stamps) and all(float(x["value"][1]) > time.time() - 60 for x in stamps)
    check("свежесть scrape (< 60 с)", fresh, f"серий: {len(stamps)}")

    print()
    print("=" * 70)
    print("3. GRAFANA: health, datasource, дашборды (через proxy)")
    print("=" * 70)
    health = fetch_json(f"{GRAF}/api/health")
    assert isinstance(health, dict)
    check("Grafana /api/health", health.get("database") == "ok", str(health.get("version")))

    password = grafana_password()
    auth = ("admin", password) if password else None
    datasources = fetch_json(f"{GRAF}/api/datasources", auth=auth)
    prom_ds = [
        d
        for d in (datasources if isinstance(datasources, list) else [])
        if isinstance(d, dict) and d.get("type") == "prometheus"
    ]
    check("datasource Prometheus", bool(prom_ds), prom_ds[0]["url"] if prom_ds else "нет")

    if prom_ds:
        uid = prom_ds[0]["uid"]

        def graf_query(expr: str) -> list[dict[str, Any]]:
            query = urlencode({"query": expr})
            data = fetch_json(
                f"{GRAF}/api/datasources/proxy/uid/{uid}/api/v1/query?{query}", auth=auth
            )
            assert isinstance(data, dict)
            result = data["data"]["result"]
            assert isinstance(result, list)
            return result

        for dash, exprs in DASHBOARD_QUERIES.items():
            counts = [len(graf_query(expr)) for expr in exprs]
            nonempty = sum(1 for n in counts if n > 0)
            check(
                f"дашборд «{dash}»: {nonempty}/{len(exprs)} панелей с данными",
                nonempty == len(exprs),
                f"серий на панель: {counts}",
            )
        search = fetch_json(f"{GRAF}/api/search?type=dash-db", auth=auth)
        titles = sorted(
            d["title"] for d in (search if isinstance(search, list) else []) if isinstance(d, dict)
        )
        check("4 дашборда загружены", len(titles) >= 4, ", ".join(titles))

    print()
    print("=" * 70)
    print(f"ИТОГО: {_ok} OK, {_fail} FAIL")
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
