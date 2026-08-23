from pathlib import Path

import polars as pl
import pytest

from cadence.economics.materials import (
    build_geographic_material_costs,
    build_material_growth_lookup,
    build_material_mass_lookup,
    build_material_price_lookup,
)

MAPPING_ROOT = Path("Data_Catalogs/Mapping")


def test_material_prices_apply_geography_fallback_before_consolidation() -> None:
    assets = pl.DataFrame(
        {
            "asset_id": ["A-1"],
            "zip_code": ["77001"],
            "cbsa_code": ["26420"],
            "state_code": ["TX"],
        }
    )

    lookup = build_material_price_lookup(
        assets,
        Path("Data/Materials/Start_Year_2026"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    asphalt = lookup.filter(
        pl.col("official_material_id") == "OFFICIAL_ASPHALT"
    ).row(0, named=True)
    metal = lookup.filter(pl.col("official_material_id") == "OFFICIAL_METAL").row(
        0, named=True
    )
    tile = lookup.filter(pl.col("official_material_id") == "OFFICIAL_TILE").row(
        0, named=True
    )

    assert asphalt["source_material_2026_usd_per_sqft"] == pytest.approx(
        (116.92169216921693 + 128.92289228922894) / 2 / 100
    )
    assert asphalt["missing_member_ids"] == ["MAT_ASPHALT_PREMIUM_ARCH"]
    assert metal["maximum_fallback_rank"] == 4
    assert metal["member_price_geography_levels"] == ["national"]
    assert tile["source_material_2026_usd_per_sqft"] is None
    assert tile["material_price_status"] == "blocked_without_override"


def test_material_growth_starts_at_one_and_preserves_missing_metal_member() -> None:
    lookup = build_material_growth_lookup(
        Path("Data/Materials/Escalation/material_projection_rates.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    metal_2026 = lookup.filter(
        (pl.col("official_material_id") == "OFFICIAL_METAL")
        & (pl.col("year") == 2026)
    ).row(0, named=True)
    metal_2027 = lookup.filter(
        (pl.col("official_material_id") == "OFFICIAL_METAL")
        & (pl.col("year") == 2027)
    ).row(0, named=True)

    assert metal_2026["material_growth_factor"] == 1.0
    assert metal_2027["material_growth_factor"] == pytest.approx(
        metal_2027["annual_escalation_factor"]
    )
    assert metal_2027["missing_growth_member_ids"] == ["MAT_METAL_ROOF_TILE"]


def test_material_mass_applies_shared_values_before_class_average() -> None:
    lookup = build_material_mass_lookup(
        Path("Data/Material_Mass/roofing_lbs.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    values = {
        row["official_material_id"]: row
        for row in lookup.iter_rows(named=True)
    }

    assert values["OFFICIAL_ASPHALT"]["weight_per_sqft_lbs"] == pytest.approx(2.25)
    assert values["OFFICIAL_METAL"]["weight_per_sqft_lbs"] == pytest.approx(1.1)
    assert values["OFFICIAL_TILE"]["weight_per_sqft_lbs"] == pytest.approx(8.5)
    assert values["OFFICIAL_METAL"]["shared_mass_value_applied"] is True


def test_builds_geographic_material_costs_from_county_expansion() -> None:
    zcta_dimension = pl.DataFrame(
        {"zcta5": ["36003"], "county_fips": ["01001"]}
    )

    result = build_geographic_material_costs(
        zcta_dimension,
        Path(
            "Data/Materials/Start_Year_2026/home_depot_material_price_expanded.csv"
        ),
        Path("Data/Materials/Escalation/material_projection_rates.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    )
    asphalt = result.filter(
        (pl.col("year") == 2026)
        & (pl.col("official_material_id") == "OFFICIAL_ASPHALT")
    ).row(0, named=True)
    tile = result.filter(
        (pl.col("year") == 2026)
        & (pl.col("official_material_id") == "OFFICIAL_TILE")
    ).row(0, named=True)

    assert result.height == 75
    assert asphalt["source_material_usd_per_sqft"] == pytest.approx(
        (70.15301530153015 + 77.35373537353736) / 2 / 100
    )
    assert asphalt["material_source_level"] == "county_modeled"
    assert tile["source_material_usd_per_sqft"] == pytest.approx(
        124.5323076923077 / 100
    )
    assert tile["material_price_status"] == "modeled_tile_proxy"
    assert tile["material_price_confidence"] == "low"
    assert tile["missing_member_ids"] == ["MAT_TILE_CLAY", "MAT_TILE_CONCRETE"]


def test_geographic_material_costs_fall_back_to_state_then_national() -> None:
    zcta_dimension = pl.DataFrame(
        {
            "zcta5": ["90001", "99501"],
            "county_fips": ["99998", "99999"],
            "state_code": ["CA", "AK"],
        }
    )

    result = build_geographic_material_costs(
        zcta_dimension,
        Path(
            "Data/Materials/Start_Year_2026/home_depot_material_price_expanded.csv"
        ),
        Path("Data/Materials/Escalation/material_projection_rates.csv"),
        MAPPING_ROOT / "master_mapping_reference_draft.csv",
        MAPPING_ROOT / "official_material_class_map_v1.csv",
    ).filter(pl.col("year") == 2026)
    california_asphalt = result.filter(
        (pl.col("zcta5") == "90001")
        & (pl.col("official_material_id") == "OFFICIAL_ASPHALT")
    ).row(0, named=True)
    alaska_metal = result.filter(
        (pl.col("zcta5") == "99501")
        & (pl.col("official_material_id") == "OFFICIAL_METAL")
    ).row(0, named=True)
    alaska_tile = result.filter(
        (pl.col("zcta5") == "99501")
        & (pl.col("official_material_id") == "OFFICIAL_TILE")
    ).row(0, named=True)

    assert result.height == 6
    assert california_asphalt["material_source_level"] == "state"
    assert california_asphalt["material_source_id"] == "CA"
    assert california_asphalt["maximum_fallback_rank"] == 2
    assert alaska_metal["material_source_level"] == "national"
    assert alaska_metal["material_source_id"] == "US"
    assert alaska_metal["maximum_fallback_rank"] == 3
    assert alaska_tile["source_material_usd_per_sqft"] == pytest.approx(
        alaska_metal["source_material_usd_per_sqft"] * 1.2
    )
    assert alaska_tile["material_price_status"] == (
        "modeled_tile_proxy_national"
    )
    assert alaska_tile["material_price_confidence"] == "low"