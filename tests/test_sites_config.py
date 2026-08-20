"""Тесты загрузчика и валидатора config/sites.yml."""

from pathlib import Path

import pytest

from app.sites import SiteConfig, SiteConfigError, load_sites

VALID = """
sites:
  - domain: example.com
    metrika_counter_id: 100
    webmaster_host_id: "https://example.com"
    node_exporter_url: "http://10.0.0.1:9100/metrics"
  - domain: example.org
"""


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "sites.yml"
    path.write_text(content, encoding="utf-8")
    return path


def test_load_valid_config(tmp_path: Path) -> None:
    sites = load_sites(write_config(tmp_path, VALID))
    assert len(sites) == 2
    assert sites[0].domain == "example.com"
    assert sites[0].metrika_counter_id == 100
    assert sites[0].webmaster_host_id == "https://example.com"
    assert sites[1].metrika_counter_id is None


def test_domain_normalized_to_lower(tmp_path: Path) -> None:
    sites = load_sites(write_config(tmp_path, "sites:\n  - domain: Example.COM\n"))
    assert sites[0].domain == "example.com"


def test_idn_domain_accepted(tmp_path: Path) -> None:
    """IDN-домен (эвакуация.online) — валиден, сохраняется как есть (unicode)."""
    sites = load_sites(
        write_config(
            tmp_path,
            "sites:\n"
            "  - domain: эвакуация.online\n"
            "    metrika_counter_id: 111456265\n"
            "    metrics_urls:\n"
            "      tracking: https://эвакуация.online/metrics/tracking\n",
        )
    )
    assert sites[0].domain == "эвакуация.online"
    assert sites[0].metrika_counter_id == 111456265
    assert sites[0].metrics_urls is not None
    assert sites[0].metrics_urls["tracking"] == "https://эвакуация.online/metrics/tracking"


def test_file_missing(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="не найден"):
        load_sites(tmp_path / "missing.yml")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="YAML"):
        load_sites(write_config(tmp_path, "sites: [ unclosed"))


def test_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="структура"):
        load_sites(write_config(tmp_path, "- 1\n- 2\n"))


def test_missing_sites_key(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="структура"):
        load_sites(write_config(tmp_path, "other: []\n"))


def test_domain_with_scheme_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="без схемы"):
        load_sites(write_config(tmp_path, "sites:\n  - domain: https://example.com\n"))


def test_empty_domain_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="не может быть пустым"):
        load_sites(write_config(tmp_path, 'sites:\n  - domain: "   "\n'))


def test_unknown_field_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="oops"):
        load_sites(write_config(tmp_path, "sites:\n  - domain: example.com\n    oops: 1\n"))


def test_duplicate_domains_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="Дублирующиеся"):
        load_sites(write_config(tmp_path, "sites:\n  - domain: a.com\n  - domain: a.com\n"))


def test_node_exporter_url_without_scheme_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="http:// или https://"):
        load_sites(
            write_config(
                tmp_path, 'sites:\n  - domain: a.com\n    node_exporter_url: "10.0.0.1:9100"\n'
            )
        )


def test_node_exporter_url_wrong_scheme_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="http:// или https://"):
        load_sites(
            write_config(
                tmp_path,
                'sites:\n  - domain: a.com\n    node_exporter_url: "ftp://10.0.0.1:9100"\n',
            )
        )


def test_node_exporter_url_empty_host_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="host"):
        load_sites(
            write_config(tmp_path, 'sites:\n  - domain: a.com\n    node_exporter_url: "http://"\n')
        )


def test_node_exporter_url_empty_string_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="пустым"):
        load_sites(
            write_config(tmp_path, 'sites:\n  - domain: a.com\n    node_exporter_url: "  "\n')
        )


def test_node_exporter_url_with_query_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="query"):
        load_sites(
            write_config(
                tmp_path,
                'sites:\n  - domain: a.com\n    node_exporter_url: "http://10.0.0.1:9100?x=1"\n',
            )
        )


def test_node_exporter_url_invalid_port_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="порт"):
        load_sites(
            write_config(
                tmp_path,
                'sites:\n  - domain: a.com\n    node_exporter_url: "http://10.0.0.1:abc"\n',
            )
        )


def test_node_exporter_url_valid_accepted(tmp_path: Path) -> None:
    sites = load_sites(
        write_config(
            tmp_path,
            'sites:\n  - domain: a.com\n    node_exporter_url: "http://10.0.0.1:9100/metrics"\n',
        )
    )
    assert sites[0].node_exporter_url == "http://10.0.0.1:9100/metrics"


def test_site_config_is_public_model() -> None:
    site = SiteConfig(domain="x.com")
    assert site.model_dump() == {
        "domain": "x.com",
        "metrika_counter_id": None,
        "webmaster_host_id": None,
        "node_exporter_url": None,
        "metrics_urls": None,
    }


VALID_METRICS_URLS = """
sites:
  - domain: example.com
    metrics_urls:
      tracking: https://example.com/metrics/tracking
      content: https://example.com/metrics/content
      node: https://example.com/metrics/node
      postgres: https://example.com/metrics/postgres
"""


def test_metrics_urls_valid_accepted(tmp_path: Path) -> None:
    sites = load_sites(write_config(tmp_path, VALID_METRICS_URLS))
    assert sites[0].metrics_urls is not None
    assert set(sites[0].metrics_urls) == {"tracking", "content", "node", "postgres"}
    assert sites[0].metrics_urls["tracking"] == "https://example.com/metrics/tracking"


def test_metrics_urls_partial_set_allowed(tmp_path: Path) -> None:
    sites = load_sites(
        write_config(
            tmp_path,
            "sites:\n  - domain: a.com\n    metrics_urls:\n      tracking: https://a.com/metrics/tracking\n",
        )
    )
    assert sites[0].metrics_urls == {"tracking": "https://a.com/metrics/tracking"}


def test_metrics_urls_unknown_kind_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="неизвестные kinds"):
        load_sites(
            write_config(
                tmp_path,
                "sites:\n  - domain: a.com\n    metrics_urls:\n      billing: https://a.com/metrics/billing\n",
            )
        )


def test_metrics_urls_http_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="https://"):
        load_sites(
            write_config(
                tmp_path,
                "sites:\n  - domain: a.com\n    metrics_urls:\n      tracking: http://a.com/metrics/tracking\n",
            )
        )


def test_metrics_urls_query_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="query"):
        load_sites(
            write_config(
                tmp_path,
                'sites:\n  - domain: a.com\n    metrics_urls:\n      node: "https://a.com/metrics/node?x=1"\n',
            )
        )


def test_metrics_urls_no_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="путь"):
        load_sites(
            write_config(
                tmp_path,
                'sites:\n  - domain: a.com\n    metrics_urls:\n      node: "https://a.com"\n',
            )
        )


def test_metrics_urls_empty_dict_rejected(tmp_path: Path) -> None:
    with pytest.raises(SiteConfigError, match="пустым"):
        load_sites(write_config(tmp_path, "sites:\n  - domain: a.com\n    metrics_urls: {}\n"))
