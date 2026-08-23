from pathlib import Path

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.geographic_sources import (
    build_geographic_annual_source_costs,
)


def test_builds_complete_source_grid_from_repository_data() -> None:
    zcta_dimension = pl.DataFrame(
        {
            "zcta5": ["79601"],
            "county_fips": ["01001"],
            "labor_market_id": ["0010180"],
        }
    )

    result = build_geographic_annual_source_costs(
        zcta_dimension,
        GeographicEconomicsRunConfig(),
        Path.cwd(),
    )
    tile = result.filter(
        (pl.col("year") == 2026)
        & (pl.col("official_material_id") == "OFFICIAL_TILE")
        & (pl.col("roof_scenario_id") == "medium")
    ).row(0, named=True)

    assert result.height == 225
    assert result.select(
        "zcta5", "year", "official_material_id", "roof_scenario_id"
    ).unique().height == 225
    assert result["source_material_usd_per_sqft"].is_null().sum() == 0
    assert result["source_labor_usd_per_sqft"].is_null().sum() == 0
    assert tile["material_price_status"] == "modeled_tile_proxy"
    assert tile["material_source_level"] == "county_modeled"
    assert tile["labor_source_level"] == "labor_market"


def test_repository_source_grid_uses_national_material_fallback() -> None:
    zcta_dimension = pl.DataFrame(
        {
            "zcta5": ["99501"],
            "county_fips": ["99999"],
            "state_code": ["AK"],
            "labor_market_id": ["0200006"],
        }
    )

    result = build_geographic_annual_source_costs(
        zcta_dimension,
        GeographicEconomicsRunConfig(),
        Path.cwd(),
    )
    tile = result.filter(
        (pl.col("year") == 2026)
        & (pl.col("official_material_id") == "OFFICIAL_TILE")
        & (pl.col("roof_scenario_id") == "medium")
    ).row(0, named=True)

    assert result.height == 225
    assert result["source_material_usd_per_sqft"].null_count() == 0
    assert tile["material_source_level"] == "national"
    assert tile["maximum_fallback_rank"] == 3
    assert tile["material_price_status"] == "modeled_tile_proxy_national"