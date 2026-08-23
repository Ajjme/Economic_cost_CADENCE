from pathlib import Path

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.reporting import build_geographic_report_tables


def test_builds_compact_report_tables_from_repository_sources() -> None:
    dimension = pl.DataFrame(
        {
            "zcta5": ["79601"],
            "county_fips": ["01001"],
            "state_code": ["TX"],
            "labor_market_id": ["0010180"],
        }
    )

    tables = build_geographic_report_tables(
        dimension,
        GeographicEconomicsRunConfig(),
        Path.cwd(),
    )

    assert tables["trends"].height == 25 * 3 * 3
    assert tables["components"].height == 3 * 3
    assert tables["map_values"].height == 3
    assert tables["quality"].height == 3
    assert tables["trends"]["median"].min() > 0