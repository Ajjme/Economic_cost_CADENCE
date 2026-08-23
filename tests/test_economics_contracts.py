import pytest
from pydantic import ValidationError

from cadence.economics.contracts import (
    AssetEconomicsInput,
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
    InstalledCostOverride,
    RepresentativeRoofScenario,
)


def _override(cost: float = 10.0) -> dict:
    return {
        "installed_usd_per_sqft": cost,
        "material_share": 0.6,
        "labor_share": 0.4,
    }


def _config() -> dict:
    return {
        "installed_cost_overrides": {
            "OFFICIAL_ASPHALT": _override(8.0),
            "OFFICIAL_METAL": _override(12.0),
            "OFFICIAL_TILE": _override(15.0),
        },
        "default_roof_shape": "gable",
        "default_roof_deck_attachment": "8d_6in_12in",
        "default_roof_wall_connection": "strap",
    }


def test_accepts_locked_economics_config() -> None:
    config = EconomicsRunConfig.model_validate(_config())

    assert config.start_year == 2026
    assert config.end_year == 2050
    assert config.installed_cost_overrides["OFFICIAL_TILE"].installed_usd_per_sqft == 15.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("start_year", 2025),
        ("end_year", 2051),
        ("scghg_discount_rate", 3.0),
        ("enabled_cost_streams", ["material", "unknown"]),
    ],
)
def test_rejects_invalid_run_configuration(field: str, value: object) -> None:
    values = _config()
    values[field] = value

    with pytest.raises(ValidationError):
        EconomicsRunConfig.model_validate(values)


def test_requires_all_three_installed_cost_overrides() -> None:
    values = _config()
    del values["installed_cost_overrides"]["OFFICIAL_TILE"]

    with pytest.raises(ValidationError, match="exactly Asphalt, Metal, and Tile"):
        EconomicsRunConfig.model_validate(values)


def test_requires_override_shares_to_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="must sum to 1"):
        InstalledCostOverride(
            installed_usd_per_sqft=10.0,
            material_share=0.8,
            labor_share=0.3,
        )


def test_rejects_nonpositive_roof_area() -> None:
    with pytest.raises(ValidationError):
        AssetEconomicsInput(
            asset_id="A-1",
            current_roof_type="asphalt",
            roof_area_sqft=0.0,
            replacement_value_usd=20_000.0,
            zip_code="29401",
            cbsa_code="16700",
            state_code="SC",
            county_fips="45019",
            labor_market_id="16700",
        )


def test_accepts_default_geographic_run_config() -> None:
    config = GeographicEconomicsRunConfig()

    assert [scenario.roof_scenario_id for scenario in config.roof_scenarios] == [
        "small",
        "medium",
        "large",
    ]
    assert [scenario.roof_area_sqft for scenario in config.roof_scenarios] == [
        1_200.0,
        2_250.0,
        3_500.0,
    ]


def test_rejects_scenario_with_inconsistent_size_bucket() -> None:
    with pytest.raises(ValidationError, match="size_bucket must be 'small'"):
        RepresentativeRoofScenario(
            roof_scenario_id="bad-small",
            roof_area_sqft=1_200.0,
            size_bucket="medium",
            roof_shape="flat",
            roof_deck_attachment="6d_6in_12in",
            roof_wall_connection="strap",
        )


def test_rejects_duplicate_geographic_scenario_ids() -> None:
    scenario = {
        "roof_scenario_id": "standard",
        "roof_area_sqft": 2_000.0,
        "size_bucket": "medium",
        "roof_shape": "flat",
        "roof_deck_attachment": "6d_6in_12in",
        "roof_wall_connection": "strap",
    }

    with pytest.raises(ValidationError, match="unique roof_scenario_id"):
        GeographicEconomicsRunConfig(roof_scenarios=(scenario, scenario))