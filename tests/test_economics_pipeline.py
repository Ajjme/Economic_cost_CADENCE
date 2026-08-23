from pathlib import Path

import polars as pl

from cadence.economics.contracts import EconomicsRunConfig
from cadence.economics.pipeline import run_economics_pipeline


def test_runs_repeatable_repository_economics_pipeline(tmp_path: Path) -> None:
    assets = pl.DataFrame(
        {
            "asset_id": ["A-1"],
            "official_current_material_id": ["OFFICIAL_ASPHALT"],
            "roof_area_sqft": [1_000.0],
            "replacement_value_usd": [12_000.0],
            "zip_code": ["77001"],
            "cbsa_code": ["26420"],
            "state_code": ["TX"],
            "county_fips": ["48201"],
            "labor_market_id": ["0010180"],
            "roof_shape": [None],
            "roof_deck_attachment": [None],
            "roof_wall_connection": [None],
        }
    )
    config = EconomicsRunConfig.model_validate(
        {
            "enabled_cost_streams": [
                "material", "labor", "disposal", "carbon", "loss_of_use"
            ],
            "installed_cost_overrides": {
                "OFFICIAL_ASPHALT": {
                    "installed_usd_per_sqft": 10.0,
                    "material_share": 0.6,
                    "labor_share": 0.4,
                },
                "OFFICIAL_METAL": {
                    "installed_usd_per_sqft": 15.0,
                    "material_share": 0.7,
                    "labor_share": 0.3,
                },
                "OFFICIAL_TILE": {
                    "installed_usd_per_sqft": 20.0,
                    "material_share": 0.5,
                    "labor_share": 0.5,
                },
            },
            "default_roof_shape": "flat",
            "default_roof_deck_attachment": "6d_6in_12in",
            "default_roof_wall_connection": "strap",
        }
    )
    hazard = pl.DataFrame(
        {
            "asset_id": ["A-1"] * 75,
            "year": [year for year in range(2026, 2051) for _ in range(3)],
            "official_material_id": [
                material
                for _ in range(25)
                for material in (
                    "OFFICIAL_ASPHALT",
                    "OFFICIAL_METAL",
                    "OFFICIAL_TILE",
                )
            ],
            "expected_damage_ratio": [0.1] * 75,
            "expected_loss_of_use_days": [2.0] * 75,
        }
    )

    manifest = run_economics_pipeline(
        assets, config, Path.cwd(), tmp_path, annual_hazard_economics=hazard
    )
    repeated = run_economics_pipeline(
        assets, config, Path.cwd(), tmp_path, annual_hazard_economics=hazard
    )

    assert manifest["annual_option_row_count"] == 75
    assert manifest["sanity_check_row_count"] == 25
    assert manifest["run_id"] == repeated["run_id"]
    run_root = (
        tmp_path
        / "schema_version=v0.1.0"
        / f"run_id={manifest['run_id']}"
    )
    assert len(list((run_root / "annual_roof_option_costs").glob("year=*"))) == 25
    assert (run_root / "replacement_value_sanity_checks.parquet").exists()
    assert (run_root / "run_metadata.json").exists()