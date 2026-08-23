from pathlib import Path

import polars as pl
import pytest

from cadence.economics.contracts import (
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
)
from cadence.economics.externalities import (
    build_annual_external_costs,
    build_geographic_annual_removal_costs,
)
from cadence.economics.materials import build_material_mass_lookup

MAPPING_ROOT = Path("Data_Catalogs/Mapping")


def _config() -> EconomicsRunConfig:
    return EconomicsRunConfig.model_validate(
        {
            "installed_cost_overrides": {
                material: {
                    "installed_usd_per_sqft": 10.0,
                    "material_share": 0.5,
                    "labor_share": 0.5,
                }
                for material in (
                    "OFFICIAL_ASPHALT",
                    "OFFICIAL_METAL",
                    "OFFICIAL_TILE",
                )
            },
            "default_roof_shape": "flat",
            "default_roof_deck_attachment": "6d_6in_12in",
            "default_roof_wall_connection": "strap",
            "scghg_discount_rate": 2.0,
        }
    )


def test_calculates_disposal_carbon_and_loss_of_use_from_current_roof() -> None:
    mass = build_material_mass_lookup(
        Path("Data/Material_Mass/roofing_lbs.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    assets = pl.DataFrame(
        {
            "asset_id": ["A-1"],
            "official_current_material_id": ["OFFICIAL_ASPHALT"],
            "roof_area_sqft": [1_000.0],
            "state_code": ["SC"],
            "county_fips": ["45019"],
        }
    )
    loss_of_use = pl.DataFrame(
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
            "expected_loss_of_use_days": [2.0] * 75,
        }
    )

    result = build_annual_external_costs(
        assets,
        mass,
        _config(),
        Path("Data/Disposal/EREF_2024_Tipping_Fees_Parsed.csv"),
        Path("Data/Carbon/roofing_eol_emission_factors.csv"),
        Path("Data/Carbon/table_a5_1_scghg_unrounded_2020_2080.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
        Path("Data/Loss_of_Use/temporary_housing_relocation_costs.csv"),
        loss_of_use,
    )
    row = result.filter(pl.col("year") == 2026).row(0, named=True)

    assert result.height == 75
    assert row["removed_roof_mass_lbs"] == pytest.approx(2_250.0)
    assert row["disposal_fee_usd_per_short_ton"] == pytest.approx(56.2)
    assert row["disposal_cost_usd"] == pytest.approx(2_250 / 2_000 * 56.2)
    assert row["landfill_kg_co2e"] == pytest.approx(22.5)
    assert row["scghg_usd_per_metric_ton"] == pytest.approx(215.0)
    assert row["carbon_cost_usd"] == pytest.approx(22.5 / 1_000 * 215.0)
    assert row["expected_loss_of_use_usd"] == pytest.approx(
        2.0 * row["housing_cost_usd_per_day"]
    )


def test_missing_loss_of_use_remains_null() -> None:
    mass = build_material_mass_lookup(
        Path("Data/Material_Mass/roofing_lbs.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    assets = pl.DataFrame(
        {
            "asset_id": ["A-1"],
            "official_current_material_id": ["OFFICIAL_METAL"],
            "roof_area_sqft": [1_000.0],
            "state_code": ["SC"],
            "county_fips": ["45019"],
        }
    )

    result = build_annual_external_costs(
        assets,
        mass,
        _config(),
        Path("Data/Disposal/EREF_2024_Tipping_Fees_Parsed.csv"),
        Path("Data/Carbon/roofing_eol_emission_factors.csv"),
        Path("Data/Carbon/table_a5_1_scghg_unrounded_2020_2080.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
        Path("Data/Loss_of_Use/temporary_housing_relocation_costs.csv"),
    )

    assert result["expected_loss_of_use_usd"].null_count() == 75


def test_builds_separate_geographic_removal_cost_fact() -> None:
    mass = build_material_mass_lookup(
        Path("Data/Material_Mass/roofing_lbs.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    zcta_dimension = pl.DataFrame(
        {"zcta5": ["29401"], "state_code": ["SC"]}
    )

    result = build_geographic_annual_removal_costs(
        zcta_dimension,
        mass,
        GeographicEconomicsRunConfig(),
        Path("Data/Disposal/EREF_2024_Tipping_Fees_Parsed.csv"),
        Path("Data/Carbon/roofing_eol_emission_factors.csv"),
        Path("Data/Carbon/table_a5_1_scghg_unrounded_2020_2080.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    asphalt = result.filter(
        (pl.col("year") == 2026)
        & (pl.col("removed_official_material_id") == "OFFICIAL_ASPHALT")
        & (pl.col("roof_scenario_id") == "small")
    ).row(0, named=True)

    assert result.height == 225
    assert asphalt["removed_roof_mass_lbs"] == pytest.approx(1_200 * 2.25)
    assert asphalt["disposal_cost_usd_per_sqft"] == pytest.approx(
        2.25 / 2_000 * 56.2
    )
    assert asphalt["carbon_cost_usd_per_sqft"] == pytest.approx(
        2.25 * 0.01 / 1_000 * 215.0
    )
    assert asphalt["representative_roof_removal_external_cost_usd"] == pytest.approx(
        asphalt["removal_external_cost_usd_per_sqft"] * 1_200
    )
    assert asphalt["tear_off_labor_excluded"] is True