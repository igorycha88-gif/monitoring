"""Проверка контура алертов (ЭПИК-7: правила Prometheus + Alertmanager → Telegram).

Валидирует структуру и содержимое конфигов согласно ADR-006 и ЧТЗ ЭПИК-7:
правила алертов, wiring Prometheus/Alertmanager в compose, секреты.
"""

from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALERTS_YML = PROJECT_ROOT / "prometheus" / "alerts.yml"
PROMETHEUS_YML = PROJECT_ROOT / "prometheus" / "prometheus.yml"
ALERTMANAGER_YML = PROJECT_ROOT / "prometheus" / "alertmanager.yml"
ENTRYPOINT_SH = PROJECT_ROOT / "prometheus" / "alertmanager-entrypoint.sh"
COMPOSE_YML = PROJECT_ROOT / "docker-compose.yml"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# (alert, for, severity, нормализованный expr) — строго по ADR-006 §1
EXPECTED_ALERTS: dict[str, dict[str, str]] = {
    "SiteDown": {
        "for": "3m",
        "severity": "critical",
        "expr": "monitoring_uptime_status == 0",
    },
    "SiteSslExpiringSoon": {
        "for": "1h",
        "severity": "warning",
        "expr": "monitoring_ssl_days_left < 14",
    },
    "SiteSslExpiringCritical": {
        "for": "1h",
        "severity": "critical",
        "expr": "monitoring_ssl_days_left < 3",
    },
    "SiteTrafficDrop": {
        "for": "2h",
        "severity": "warning",
        "expr": (
            'monitoring_site_visits_total{source="metrika"} < 0.5 * '
            '(monitoring_site_visits_total{source="metrika"} offset 24h) '
            'and on(site) (monitoring_site_visits_total{source="metrika"} '
            "offset 24h) > 10"
        ),
    },
    "CollectorFailing": {
        "for": "15m",
        "severity": "warning",
        "expr": "monitoring_collector_success == 0",
    },
    "NodeExporterDown": {
        "for": "5m",
        "severity": "warning",
        "expr": 'up{job="node-exporter"} == 0',
    },
    "MonitoringAppDown": {
        "for": "5m",
        "severity": "critical",
        "expr": "absent(monitoring_uptime_status)",
    },
    "SiteMetricsEndpointDown": {
        "for": "5m",
        "severity": "warning",
        "expr": "monitoring_site_metrics_up == 0",
    },
    "SiteNoTraffic": {
        "for": "3h",
        "severity": "warning",
        "expr": "business_page_views_24h == 0",
    },
}


def load_alerts() -> dict[str, dict[str, Any]]:
    data: dict[str, Any] = yaml.safe_load(ALERTS_YML.read_text(encoding="utf-8"))
    rules = [r for group in data["groups"] for r in group["rules"]]
    return {r["alert"]: r for r in rules}


def normalize(expr: str) -> str:
    return " ".join(expr.split())


class TestAlertRules:
    def test_all_alerts_present_no_extras(self) -> None:
        assert set(load_alerts()) == set(EXPECTED_ALERTS)

    def test_expr_for_severity_match_adr006(self) -> None:
        alerts = load_alerts()
        for name, expected in EXPECTED_ALERTS.items():
            rule = alerts[name]
            assert normalize(rule["expr"]) == normalize(expected["expr"]), name
            assert rule["for"] == expected["for"], name
            assert rule["labels"]["severity"] == expected["severity"], name

    def test_annotations_in_russian_with_labels(self) -> None:
        for name, rule in load_alerts().items():
            assert rule["annotations"].get("summary"), name
            assert rule["annotations"].get("description"), name
        site_alerts = ("SiteDown", "SiteSslExpiringSoon", "NodeExporterDown")
        for name in site_alerts:
            assert "{{ $labels.site }}" in load_alerts()[name]["annotations"]["summary"], name
        assert "{{ $labels.source }}" in load_alerts()["CollectorFailing"]["annotations"]["summary"]

    def test_no_rate_on_monitoring_metrics(self) -> None:
        """monitoring_* — gauge: rate()/increase() запрещены (ARCHITECTURE.md)."""
        for name, rule in load_alerts().items():
            assert "rate(" not in rule["expr"], name
            assert "increase(" not in rule["expr"], name

    def test_traffic_drop_compares_offset_24h(self) -> None:
        expr = load_alerts()["SiteTrafficDrop"]["expr"]
        assert "offset 24h" in expr
        assert "> 10" in expr  # guard от шума на молодых сайтах/ночью


class TestPrometheusWiring:
    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = yaml.safe_load(PROMETHEUS_YML.read_text(encoding="utf-8"))
        return data

    def test_rule_files_mounted(self) -> None:
        assert "/etc/prometheus/alerts.yml" in self.load()["rule_files"]

    def test_alertmanager_target(self) -> None:
        managers = self.load()["alerting"]["alertmanagers"]
        targets = [t for m in managers for t in m["static_configs"][0]["targets"]]
        assert "alertmanager:9093" in targets


class TestAlertmanagerConfig:
    def load(self) -> dict[str, Any]:
        data: dict[str, Any] = yaml.safe_load(ALERTMANAGER_YML.read_text(encoding="utf-8"))
        return data

    def test_route_antispam_settings(self) -> None:
        route = self.load()["route"]
        assert route["receiver"] == "telegram"
        assert route["group_by"] == ["alertname", "site"]
        assert route["group_wait"] == "30s"
        assert route["repeat_interval"] == "12h"

    def test_telegram_receiver_placeholders(self) -> None:
        receiver = next(r for r in self.load()["receivers"] if r["name"] == "telegram")
        tg = receiver["telegram_configs"][0]
        assert tg["bot_token"] == "__TELEGRAM_BOT_TOKEN__"
        assert str(tg["chat_id"]) == "__TELEGRAM_CHAT_ID__"
        assert tg["send_resolved"] is True

    def test_inhibit_rules_exist(self) -> None:
        inhibits = self.load()["inhibit_rules"]
        assert len(inhibits) >= 2
        assert all("equal" in rule for rule in inhibits)

    def test_no_real_secrets_in_template(self) -> None:
        """Секреты только в .env — в шаблоне лишь плейсхолдеры."""
        content = ALERTMANAGER_YML.read_text(encoding="utf-8")
        assert "bot_token" in content
        assert ":AA" not in content  # формат реальных токенов Telegram


class TestEntrypoint:
    def test_substitutes_both_placeholders(self) -> None:
        content = ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "__TELEGRAM_BOT_TOKEN__" in content
        assert "__TELEGRAM_CHAT_ID__" in content
        assert "exec alertmanager" in content

    def test_fallback_when_secrets_missing(self) -> None:
        """Пустые env → заглушки, контейнер стартует (ADR-006 §3).

        chat_id=0 отклоняется alertmanager ("missing chat_id") —
        fallback обязан быть ненулевым.
        """
        content = ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "NOT_CONFIGURED" in content
        assert 'CHAT_ID="1"' in content
        assert 'CHAT_ID="0"' not in content


class TestPrometheusEntrypoint:
    """ЭПИК-9 (ADR-007 D3): подстановка ключа в конфиг Prometheus."""

    PROMETHEUS_ENTRYPOINT_SH = PROJECT_ROOT / "prometheus" / "prometheus-entrypoint.sh"

    def test_substitutes_key_placeholder(self) -> None:
        content = self.PROMETHEUS_ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "__SITE_METRICS_API_KEY__" in content
        assert "SITE_METRICS_API_KEY" in content
        assert "exec prometheus" in content

    def test_fallback_when_key_missing(self) -> None:
        content = self.PROMETHEUS_ENTRYPOINT_SH.read_text(encoding="utf-8")
        assert "NOT_CONFIGURED" in content
        assert "--storage.tsdb.retention.time=90d" in content


class TestComposeWiring:
    def load(self) -> dict[str, Any]:
        data: Any = yaml.safe_load(COMPOSE_YML.read_text(encoding="utf-8"))
        services: dict[str, Any] = data["services"]
        return services

    def test_prometheus_mounts_alerts(self) -> None:
        volumes = self.load()["prometheus"]["volumes"]
        assert any(v.startswith("./prometheus/alerts.yml:") for v in volumes)

    def test_alertmanager_service(self) -> None:
        svc = self.load()["alertmanager"]
        assert svc["container_name"] == "monitoring-alertmanager"
        assert svc["image"] == "prom/alertmanager:v0.27.0"
        assert svc["ports"] == ["127.0.0.1:9093:9093"]
        assert "monitoring-net" in svc["networks"]
        assert "healthcheck" in svc

    def test_alertmanager_gets_secrets_from_env(self) -> None:
        env = self.load()["alertmanager"]["environment"]
        assert "TELEGRAM_BOT_TOKEN" in env
        assert "TELEGRAM_CHAT_ID" in env

    def test_alertmanager_mounts_template_and_entrypoint(self) -> None:
        volumes = self.load()["alertmanager"]["volumes"]
        assert any("alertmanager.yml" in v and v.endswith(":ro") for v in volumes)
        assert any("alertmanager-entrypoint.sh" in v for v in volumes)

    def test_prometheus_depends_on_alertmanager(self) -> None:
        assert "alertmanager" in self.load()["prometheus"]["depends_on"]

    def test_prometheus_gets_site_metrics_key_from_env(self) -> None:
        """ЭПИК-9: ключ уходит в контейнер env'ом, не в конфиге."""
        prometheus = self.load()["prometheus"]
        assert "SITE_METRICS_API_KEY" in prometheus["environment"]
        assert prometheus["entrypoint"] == [
            "/bin/sh",
            "/etc/prometheus/prometheus-entrypoint.sh",
        ]
        volumes = prometheus["volumes"]
        assert any("prometheus.yml" in v and v.endswith(".tmpl:ro") for v in volumes)
        assert any("prometheus-entrypoint.sh" in v for v in volumes)


class TestEnvExample:
    def test_telegram_vars_documented_empty(self) -> None:
        content = ENV_EXAMPLE.read_text(encoding="utf-8")
        assert "TELEGRAM_BOT_TOKEN=\n" in content or "TELEGRAM_BOT_TOKEN=\r" in content
        assert "TELEGRAM_CHAT_ID=\n" in content or "TELEGRAM_CHAT_ID=\r" in content
        # инструкция по созданию бота рядом с переменными
        assert "BotFather" in content
