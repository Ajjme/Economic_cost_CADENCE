import polars as pl
import pytest

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.costs import build_geographic_installed_costs


def _source_costs() -> pl.DataFrame:
    config = GeographicEconomicsRunConfig()
    rows = []
    for zcta5 in ("02108", "29401"):
        for year in range(config.start_year, config.end_year + 1):
            for material_id in (
                "OFFICIAL_ASPHALT",
                "OFFICIAL_METAL",
                "OFFICIAL_TILE",
            ):
                for scenario in config.roof_scenarios:
                    rows.append(
                        {
                            "zcta5": zcta5,
                            "year": year,
                            "official_material_id": material_id,
                            "roof_scenario_id": scenario.roof_scenario_id,
                            "roof_area_sqft": scenario.roof_area_sqft,
                            "source_material_usd_per_sqft": 4.0,
                            "source_labor_usd_per_sqft": 6.0,
                            "material_source_level": "county_modeled",
                        }
                    )
    return pl.DataFrame(rows)


def test_builds_unit_and_representative_roof_costs() -> None:
    result = build_geographic_installed_costs(
        _source_costs(), GeographicEconomicsRunConfig()
    )
    medium = result.filter(
        (pl.col("zcta5") == "02108")
        & (pl.col("year") == 2026)
        & (pl.col("official_material_id") == "OFFICIAL_ASPHALT")
        & (pl.col("roof_scenario_id") == "medium")
    ).row(0, named=True)

    assert result.height == 2 * 25 * 3 * 3
    assert medium["installed_cost_usd_per_sqft"] == pytest.approx(10.0)
    assert medium["representative_roof_installed_cost_usd"] == pytest.approx(
        22_500.0
    )
    assert medium["material_source_level"] == "county_modeled"
    assert medium["dollar_basis"] == "real_2026_usd"


def test_rejects_an_incomplete_geographic_cost_grid() -> None:
    source = _source_costs().slice(1)

    with pytest.raises(ValueError, match="one row per ZCTA, year, class, and scenario"):
        build_geographic_installed_costs(source, GeographicEconomicsRunConfig())


def test_rejects_roof_area_that_disagrees_with_scenario() -> None:
    source = _source_costs().with_columns(
        pl.when(pl.col("roof_scenario_id") == "small")
        .then(pl.lit(1_300.0))
        .otherwise(pl.col("roof_area_sqft"))
        .alias("roof_area_sqft")
    )

    with pytest.raises(ValueError, match="must match the configured roof scenario"):
        build_geographic_installed_costs(source, GeographicEconomicsRunConfig())