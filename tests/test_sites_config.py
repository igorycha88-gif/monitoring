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
    }
