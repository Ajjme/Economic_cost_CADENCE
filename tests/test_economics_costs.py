import polars as pl
import pytest
from typing import List, Optional

from cadence.economics.contracts import EconomicsRunConfig
from cadence.economics.costs import (
    build_annual_roof_option_costs,
    build_replacement_value_sanity_checks,
)


def _config(enabled: Optional[List[str]] = None) -> EconomicsRunConfig:
    return EconomicsRunConfig.model_validate(
        {
            "enabled_cost_streams": enabled or ["material", "labor"],
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
            "default_roof_shape": "gable",
            "default_roof_deck_attachment": "8d_6in_12in",
            "default_roof_wall_connection": "strap",
        }
    )


def _assets() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "asset_id": ["A-1"],
            "official_current_material_id": ["OFFICIAL_ASPHALT"],
            "roof_area_sqft": [1_000.0],
            "replacement_value_usd": [12_000.0],
        }
    )


def _growth() -> pl.DataFrame:
    return pl.DataFrame(
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
            "material_growth_factor": [1.2] * 75,
            "labor_growth_factor": [1.1] * 75,
        }
    )


def test_builds_three_options_for_each_of_twenty_five_years() -> None:
    result = build_annual_roof_option_costs(_assets(), _growth(), _config())

    assert result.height == 75
    assert result["year"].min() == 2026
    assert result["year"].max() == 2050


def test_applies_user_value_to_current_roof_and_override_to_alternative() -> None:
    result = build_annual_roof_option_costs(_assets(), _growth(), _config())
    year = result.filter(pl.col("year") == 2026)
    asphalt = year.filter(pl.col("official_material_id") == "OFFICIAL_ASPHALT").row(
        0, named=True
    )
    metal = year.filter(pl.col("official_material_id") == "OFFICIAL_METAL").row(
        0, named=True
    )

    assert asphalt["installed_growth_factor"] == pytest.approx(1.16)
    assert asphalt["installed_capex_usd"] == pytest.approx(13_920.0)
    assert asphalt["operational_cost_source"] == "asset_replacement_value"
    assert metal["installed_capex_usd"] == pytest.approx(15.0 * 1.17 * 1_000.0)
    assert metal["operational_cost_source"] == "class_installed_override"


def test_adds_only_enabled_external_streams_and_keeps_loss_separate() -> None:
    external = pl.DataFrame(
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
            "disposal_cost_usd": [100.0] * 75,
            "carbon_cost_usd": [20.0] * 75,
            "expected_loss_of_use_usd": [50.0] * 75,
        }
    )
    result = build_annual_roof_option_costs(
        _assets(),
        _growth(),
        _config(["material", "labor", "disposal", "loss_of_use"]),
        external_costs=external,
    )
    row = result.row(0, named=True)

    assert row["replacement_economic_cost_usd"] == pytest.approx(
        row["installed_capex_usd"] + 100.0
    )
    assert row["enabled_economic_total_usd"] == pytest.approx(
        row["replacement_economic_cost_usd"] + 50.0
    )


def test_repair_uses_only_operational_installed_cost_and_damage() -> None:
    damage = _growth().select("asset_id", "year", "official_material_id").with_columns(
        pl.lit(0.25).alias("expected_damage_ratio")
    )
    result = build_annual_roof_option_costs(
        _assets(), _growth(), _config(), annual_damage=damage
    )
    row = result.row(0, named=True)

    assert row["provisional_repair_cost_usd"] == pytest.approx(
        row["installed_capex_usd"] * 0.25
    )
    assert row["repair_cost_incomplete"] is False


def test_rejects_damage_ratio_above_one() -> None:
    damage = _growth().select("asset_id", "year", "official_material_id").with_columns(
        pl.lit(1.1).alias("expected_damage_ratio")
    )

    with pytest.raises(ValueError, match="between 0 and 1"):
        build_annual_roof_option_costs(
            _assets(), _growth(), _config(), annual_damage=damage
        )


def test_sanity_check_flags_large_source_variance() -> None:
    source = _growth().select("asset_id", "year", "official_material_id").with_columns(
        pl.lit(4.0).alias("source_material_usd_per_sqft"),
        pl.lit(4.0).alias("source_labor_usd_per_sqft"),
    )
    costs = build_annual_roof_option_costs(
        _assets(), _growth(), _config(), source_costs=source
    )

    checks = build_replacement_value_sanity_checks(costs, tolerance_percent=20.0)

    assert checks.height == 25
    assert checks.row(0, named=True)["source_outside_tolerance"] is True