"""Экспорт дневной истории Вебмастера из SQLite в OpenMetrics (ADR-011 D4).

Разовая операция: файл отдаётся promtool'у для создания блоков TSDB
(живой Prometheus отбрасывает старые семплы на scrape — историю вносит
только импорт блоков):

    # внутри контейнера app:
    python scripts/export_webmaster_history.py --end 2026-08-18 \
        --out /tmp/webmaster_history.omtex

    # создание блоков + импорт (хост):
    docker run --rm -v "$PWD:/data" --entrypoint /bin/sh prom/prometheus:latest \
        -c 'promtool tsdb create-blocks-from openmetrics /data/webmaster_history.omtex /data/blocks'
    # затем блоки перенести в volume prometheus-data и перезапустить prometheus.

Правила (ADR-011 D4):
  - только daily-метрики (weekly-снапшоты рендерятся из БД актуальными);
  - --end включительно; вызывающий обязан передать дату СТРОГО раньше
    первого семпла TSDB (стык без дублей);
  - лейблы тождественны скрейп-сериям (job/instance): импорт становится
    продолжением тех же серий, sum() в дашбордах не задваивается;
  - дедупликация: разные query_id с одинаковым текстом запроса суммируются
    по (site, query, date);
  - timestamp = дата 00:00 UTC В СЕКУНДАХ (промtool этого образа умножает
    значение на 1000: миллисекунды уводят блоки в 58-е тыс. лет — фикс
    коммита 9ed7d15).
"""

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.config import get_settings
from app.logging import get_logger, setup_logging
from app.storage import DailyPoint, WebmasterStorage

logger = get_logger("scripts.export_webmaster_history")

METRIC_CLICKS = "monitoring_search_daily_clicks"
METRIC_SHOWS = "monitoring_search_daily_shows"

# Лейблы скрейп-серий приложения (prometheus/prometheus.yml: job monitoring-app)
DEFAULT_JOB = "monitoring-app"
DEFAULT_INSTANCE = "app:8088"


@dataclass(frozen=True)
class SeriesKey:
    """Идентификатор серии экспорта: текст запроса (query_id дедуплицируется)."""

    site: str
    query: str


def escape_label(value: str) -> str:
    """Экранирование значения лейбла для текстовой экспозиции."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def timestamp_seconds(day: str) -> int:
    """Дата YYYY-MM-DD → epoch-секунды (00:00 UTC). Промtool сам ×1000."""
    moment = datetime.fromisoformat(day).replace(tzinfo=UTC)
    return int(moment.timestamp())


def aggregate(points: list[DailyPoint]) -> dict[SeriesKey, dict[str, dict[str, float]]]:
    """Дедупликация точек: сумма значений по (site, query, date, метрика).

    Возвращает {серия: {метрика: {день: сумма}}}.
    """
    result: dict[SeriesKey, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for point in points:
        key = SeriesKey(site=point.site, query=point.query)
        if point.clicks is not None:
            bucket = result[key][METRIC_CLICKS]
            bucket[point.date] = bucket.get(point.date, 0.0) + point.clicks
        if point.shows is not None:
            bucket = result[key][METRIC_SHOWS]
            bucket[point.date] = bucket.get(point.date, 0.0) + point.shows
    return result


def render_openmetrics(
    aggregated: dict[SeriesKey, dict[str, dict[str, float]]],
    job: str = DEFAULT_JOB,
    instance: str = DEFAULT_INSTANCE,
) -> str:
    """Агрегаты → текст OpenMetrics (серии отсортированы, точки по возрастанию).

    Лейблы job/instance тождественны скрейп-сериям — импорт продолжит те же
    серии (без задвоения в sum(), ADR-011 D4).
    """
    lines: list[str] = [f"# TYPE {METRIC_CLICKS} gauge", f"# TYPE {METRIC_SHOWS} gauge"]
    for metric in (METRIC_CLICKS, METRIC_SHOWS):
        for key in sorted(aggregated, key=lambda item: (item.site, item.query)):
            days = aggregated[key].get(metric)
            if not days:
                continue
            labels = (
                f'job="{escape_label(job)}",instance="{escape_label(instance)}",'
                f'site="{escape_label(key.site)}",query="{escape_label(key.query)}"'
            )
            for day in sorted(days):
                value = days[day]
                rendered = f"{value:g}"
                lines.append(f"{metric}{{{labels}}} {rendered} {timestamp_seconds(day)}")
    lines.append("# EOF")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Точка входа: SQLite → OpenMetrics-файл. Возвращает код завершения."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--end", required=True, help="последний день включительно (YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="путь к файлу OpenMetrics")
    parser.add_argument("--db", default=None, help="путь к SQLite (по умолчанию из настроек)")
    parser.add_argument("--job", default=DEFAULT_JOB, help="лейбл job скрейп-серий")
    parser.add_argument("--instance", default=DEFAULT_INSTANCE, help="лейбл instance скрейп-серий")
    args = parser.parse_args(argv)

    settings = get_settings()
    setup_logging(settings.log_level, settings.log_format)
    try:
        datetime.fromisoformat(args.end)
    except ValueError:
        logger.error("export_failed", reason="bad_end_date", end=args.end)
        return 2
    db_path = args.db or settings.webmaster_db_path
    storage = WebmasterStorage(db_path)
    points = storage.daily_history(args.end)
    if not points:
        logger.error("export_failed", reason="no_data", end=args.end, db=db_path)
        return 1
    aggregated = aggregate(points)
    content = render_openmetrics(aggregated, job=args.job, instance=args.instance)
    Path(args.out).write_text(content, encoding="utf-8")
    dates = sorted({point.date for point in points})
    logger.info(
        "export_finished",
        out=args.out,
        days=len(dates),
        date_from=dates[0],
        date_to=dates[-1],
        series=len(aggregated),
        points=len(points),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
