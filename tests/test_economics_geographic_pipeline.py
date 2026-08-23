from pathlib import Path

import polars as pl
import pytest

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.geographic_pipeline import run_geographic_economics_pipeline


def _crosswalk() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "zcta5": ["02108", "29401"],
            "state_code": ["MA", "SC"],
            "county_fips": ["25025", "45019"],
            "cbsa_code": ["14460", "16700"],
            "labor_market_id": ["0014460", "0016700"],
            "crosswalk_method": ["largest_overlap", "largest_overlap"],
            "source_vintage": ["2020", "2020"],
        }
    )


def _source_costs(config: GeographicEconomicsRunConfig) -> pl.DataFrame:
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
                            "labor_source_level": "msa",
                        }
                    )
    return pl.DataFrame(rows)


def test_publishes_repeatable_year_partitioned_geographic_costs(
    tmp_path: Path,
) -> None:
    config = GeographicEconomicsRunConfig()
    source_costs = _source_costs(config)

    manifest = run_geographic_economics_pipeline(
        _crosswalk(), source_costs, config, tmp_path
    )
    repeated = run_geographic_economics_pipeline(
        _crosswalk().reverse(), source_costs.reverse(), config, tmp_path
    )

    assert manifest["run_id"] == repeated["run_id"]
    assert manifest["zcta_count"] == 2
    assert manifest["annual_installed_cost_row_count"] == 2 * 25 * 3 * 3
    run_root = (
        tmp_path
        / "schema_version=v1.0.0"
        / f"run_id={manifest['run_id']}"
    )
    assert len(list((run_root / "annual_installed_costs").glob("year=*"))) == 25
    assert (run_root / "zcta_dimension.parquet").exists()
    assert (run_root / "run_metadata.json").exists()


def test_rejects_source_costs_with_missing_zcta() -> None:
    config = GeographicEconomicsRunConfig()
    source_costs = _source_costs(config).filter(pl.col("zcta5") == "02108")

    try:
        run_geographic_economics_pipeline(
            _crosswalk(), source_costs, config, Path("unused")
        )
    except ValueError as error:
        assert "identical zcta5 values" in str(error)
    else:
        raise AssertionError("expected missing ZCTA validation to fail")


def test_rejects_removal_geography_before_writing_output(tmp_path: Path) -> None:
    config = GeographicEconomicsRunConfig()
    invalid_removal = pl.DataFrame({"zcta5": ["99999"]})

    with pytest.raises(ValueError, match="annual_removal_costs"):
        run_geographic_economics_pipeline(
            _crosswalk(),
            _source_costs(config),
            config,
            tmp_path,
            annual_removal_costs=invalid_removal,
        )

    assert not tmp_path.exists() or not any(tmp_path.iterdir())