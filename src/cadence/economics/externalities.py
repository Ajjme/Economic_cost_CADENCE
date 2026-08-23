"""Removal, carbon, and expected loss-of-use cost streams."""

from pathlib import Path
from typing import Optional

import polars as pl

from cadence.economics.contracts import (
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
    OFFICIAL_MATERIAL_CLASSES,
)

EXTERNALITY_ASSET_COLUMNS = (
    "asset_id",
    "official_current_material_id",
    "roof_area_sqft",
    "state_code",
    "county_fips",
)

EREF_REGIONS = {
    "AK": "Pacific", "AZ": "Pacific", "CA": "Pacific", "HI": "Pacific",
    "ID": "Pacific", "NV": "Pacific", "OR": "Pacific", "WA": "Pacific",
    "CT": "Northeast", "DE": "Northeast", "DC": "Northeast", "ME": "Northeast",
    "MD": "Northeast", "MA": "Northeast", "NH": "Northeast", "NJ": "Northeast",
    "NY": "Northeast", "PA": "Northeast", "RI": "Northeast", "VT": "Northeast",
    "VA": "Northeast", "WV": "Northeast",
    "CO": "Mountains/Plains", "MT": "Mountains/Plains", "ND": "Mountains/Plains",
    "SD": "Mountains/Plains", "UT": "Mountains/Plains", "WY": "Mountains/Plains",
    "IL": "Midwest", "IN": "Midwest", "IA": "Midwest", "KS": "Midwest",
    "MI": "Midwest", "MN": "Midwest", "MO": "Midwest", "NE": "Midwest",
    "OH": "Midwest", "WI": "Midwest",
    "AL": "Southeast", "FL": "Southeast", "GA": "Southeast", "KY": "Southeast",
    "MS": "Southeast", "NC": "Southeast", "SC": "Southeast", "TN": "Southeast",
    "AR": "South Central", "LA": "South Central", "NM": "South Central",
    "OK": "South Central", "TX": "South Central",
}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def build_geographic_annual_removal_costs(
    zcta_dimension: pl.DataFrame,
    material_mass: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    disposal_path: Path,
    carbon_path: Path,
    scghg_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Build disposal and landfill-carbon costs by removed roof class."""
    _require_columns(
        zcta_dimension,
        ("zcta5", "state_code"),
        "zcta_dimension",
    )
    _require_columns(
        material_mass,
        ("official_material_id", "weight_per_sqft_lbs"),
        "material_mass",
    )
    geography = zcta_dimension.select("zcta5", "state_code")
    if geography["zcta5"].n_unique() != geography.height:
        raise ValueError("zcta_dimension must contain unique zcta5 values")

    scenarios = pl.DataFrame(
        [
            {
                "roof_scenario_id": scenario.roof_scenario_id,
                "roof_area_sqft": scenario.roof_area_sqft,
            }
            for scenario in config.roof_scenarios
        ]
    )
    base = (
        geography.join(scenarios, how="cross")
        .join(
            material_mass.select(
                pl.col("official_material_id").alias(
                    "removed_official_material_id"
                ),
                "weight_per_sqft_lbs",
            ),
            how="cross",
        )
        .with_columns(
            (pl.col("roof_area_sqft") * pl.col("weight_per_sqft_lbs")).alias(
                "removed_roof_mass_lbs"
            )
        )
    )
    disposal = _attach_disposal_cost(base, disposal_path)
    carbon_factors = _build_landfill_carbon_factors(
        carbon_path,
        mapping_path,
        class_map_path,
    ).rename({"official_material_id": "removed_official_material_id"})
    scghg_column = f"scc_co2_{config.scghg_discount_rate:.1f}pct"
    scghg = pl.read_csv(scghg_path).select(
        pl.col("emission_year").alias("year"),
        pl.col(scghg_column).alias("scghg_usd_per_metric_ton"),
    ).filter(pl.col("year").is_between(config.start_year, config.end_year))
    result = (
        disposal.join(
            carbon_factors,
            on="removed_official_material_id",
            how="left",
            validate="m:1",
        )
        .join(scghg, how="cross")
        .with_columns(
            (
                pl.col("removed_roof_mass_lbs")
                * pl.col("landfill_kg_co2e_per_lb")
            ).alias("landfill_kg_co2e"),
            (
                pl.col("weight_per_sqft_lbs")
                / 2000.0
                * pl.col("disposal_fee_usd_per_short_ton")
            ).alias("disposal_cost_usd_per_sqft"),
        )
        .with_columns(
            (
                pl.col("landfill_kg_co2e")
                / 1000.0
                * pl.col("scghg_usd_per_metric_ton")
            ).alias("carbon_cost_usd"),
            (
                pl.col("weight_per_sqft_lbs")
                * pl.col("landfill_kg_co2e_per_lb")
                / 1000.0
                * pl.col("scghg_usd_per_metric_ton")
            ).alias("carbon_cost_usd_per_sqft"),
        )
        .with_columns(
            (
                pl.col("disposal_cost_usd_per_sqft")
                + pl.col("carbon_cost_usd_per_sqft")
            ).alias("removal_external_cost_usd_per_sqft"),
            (pl.col("disposal_cost_usd") + pl.col("carbon_cost_usd")).alias(
                "representative_roof_removal_external_cost_usd"
            ),
            pl.lit("landfill").alias("eol_pathway"),
            pl.lit("fixed_2026_real_proxy").alias("disposal_projection_method"),
            pl.lit(True).alias("tear_off_labor_excluded"),
            pl.lit("real_2026_usd").alias("dollar_basis"),
        )
    )
    expected_rows = (
        geography.height
        * len(config.roof_scenarios)
        * len(OFFICIAL_MATERIAL_CLASSES)
        * (config.end_year - config.start_year + 1)
    )
    keys = [
        "zcta5",
        "year",
        "removed_official_material_id",
        "roof_scenario_id",
    ]
    if result.height != expected_rows or result.select(keys).unique().height != expected_rows:
        raise ValueError("removal costs must contain one row per ZCTA, year, class, and scenario")
    return result.sort(keys)


def build_annual_external_costs(
    assets: pl.DataFrame,
    material_mass: pl.DataFrame,
    config: EconomicsRunConfig,
    disposal_path: Path,
    carbon_path: Path,
    scghg_path: Path,
    mapping_path: Path,
    class_map_path: Path,
    housing_path: Path,
    annual_loss_of_use: Optional[pl.DataFrame] = None,
) -> pl.DataFrame:
    """Build annual removal and expected downtime costs for each asset."""
    _require_columns(assets, EXTERNALITY_ASSET_COLUMNS, "assets")
    base = assets.select(EXTERNALITY_ASSET_COLUMNS).join(
        material_mass.select(
            pl.col("official_material_id").alias("official_current_material_id"),
            "weight_per_sqft_lbs",
        ),
        on="official_current_material_id",
        how="left",
        validate="m:1",
    )
    if base.filter(pl.col("weight_per_sqft_lbs").is_null()).height:
        raise ValueError("current roof mass could not be resolved")
    base = base.with_columns(
        (pl.col("roof_area_sqft") * pl.col("weight_per_sqft_lbs")).alias(
            "removed_roof_mass_lbs"
        )
    )
    disposal = _attach_disposal_cost(base, disposal_path)
    carbon_factors = _build_landfill_carbon_factors(
        carbon_path, mapping_path, class_map_path
    )
    scghg_column = f"scc_co2_{config.scghg_discount_rate:.1f}pct"
    scghg = pl.read_csv(scghg_path).select(
        pl.col("emission_year").alias("year"),
        pl.col(scghg_column).alias("scghg_usd_per_metric_ton"),
    ).filter(pl.col("year").is_between(config.start_year, config.end_year))
    result = (
        disposal.join(
            carbon_factors,
            left_on="official_current_material_id",
            right_on="official_material_id",
            how="left",
            validate="m:1",
        )
        .join(scghg, how="cross")
        .join(
            pl.DataFrame(
                {"official_material_id": list(OFFICIAL_MATERIAL_CLASSES)}
            ),
            how="cross",
        )
        .with_columns(
            (
                pl.col("removed_roof_mass_lbs")
                * pl.col("landfill_kg_co2e_per_lb")
            ).alias("landfill_kg_co2e"),
        )
        .with_columns(
            (
                pl.col("landfill_kg_co2e")
                / 1000.0
                * pl.col("scghg_usd_per_metric_ton")
            ).alias("carbon_cost_usd")
        )
    )
    housing = pl.read_csv(
        housing_path, schema_overrides={"county_fips": pl.String}
    ).select(
        pl.col("county_fips").str.zfill(5),
        pl.col("relocation_cost_per_day").alias("housing_cost_usd_per_day"),
        pl.col("primary_bedroom").alias("housing_bedroom_assumption"),
    ).unique("county_fips")
    result = result.join(housing, on="county_fips", how="left", validate="m:1")
    if annual_loss_of_use is not None:
        _require_columns(
            annual_loss_of_use,
            (
                "asset_id",
                "year",
                "official_material_id",
                "expected_loss_of_use_days",
            ),
            "annual_loss_of_use",
        )
        result = result.join(
            annual_loss_of_use.select(
                "asset_id",
                "year",
                "official_material_id",
                "expected_loss_of_use_days",
            ),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        )
    else:
        result = result.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("expected_loss_of_use_days")
        )
    return result.with_columns(
        (
            pl.col("expected_loss_of_use_days")
            * pl.col("housing_cost_usd_per_day")
        ).alias("expected_loss_of_use_usd"),
        pl.lit("landfill").alias("eol_pathway"),
        pl.lit("fixed_2026_real_proxy").alias("disposal_projection_method"),
        pl.lit("fixed_2026_real_proxy").alias("housing_projection_method"),
        pl.lit(True).alias("fixed_two_bedroom_assumption"),
    ).select(
        "asset_id",
        "year",
        "official_material_id",
        "removed_roof_mass_lbs",
        "disposal_fee_usd_per_short_ton",
        "disposal_geography_level",
        "disposal_cost_usd",
        "landfill_kg_co2e_per_lb",
        "landfill_kg_co2e",
        "scghg_usd_per_metric_ton",
        "carbon_cost_usd",
        "expected_loss_of_use_days",
        "housing_cost_usd_per_day",
        "expected_loss_of_use_usd",
        "eol_pathway",
        "carbon_factor_provisional",
        "disposal_projection_method",
        "housing_projection_method",
        "fixed_two_bedroom_assumption",
    ).sort(["asset_id", "year", "official_material_id"])


def _attach_disposal_cost(base: pl.DataFrame, disposal_path: Path) -> pl.DataFrame:
    fees = pl.read_csv(disposal_path).rename(
        {
            "Region/State": "disposal_geography",
            "Average tipping Fee": "disposal_fee_usd_per_short_ton",
        }
    )
    state_crosswalk = pl.DataFrame(
        {
            "state_code": list(STATE_NAMES),
            "state_name": list(STATE_NAMES.values()),
            "eref_region": [EREF_REGIONS[state] for state in STATE_NAMES],
        }
    )
    state_fees = fees.select(
        pl.col("disposal_geography").alias("state_name"),
        pl.col("disposal_fee_usd_per_short_ton").alias("state_fee"),
    )
    region_fees = fees.select(
        pl.col("disposal_geography").alias("eref_region"),
        pl.col("disposal_fee_usd_per_short_ton").alias("region_fee"),
    )
    national_fee = fees.filter(
        pl.col("disposal_geography") == "National Average"
    )["disposal_fee_usd_per_short_ton"].item()
    return (
        base.join(state_crosswalk, on="state_code", how="left", validate="m:1")
        .join(state_fees, on="state_name", how="left", validate="m:1")
        .join(region_fees, on="eref_region", how="left", validate="m:1")
        .with_columns(
            pl.coalesce("state_fee", "region_fee", pl.lit(national_fee)).alias(
                "disposal_fee_usd_per_short_ton"
            ),
            pl.when(pl.col("state_fee").is_not_null())
            .then(pl.lit("state"))
            .when(pl.col("region_fee").is_not_null())
            .then(pl.lit("region"))
            .otherwise(pl.lit("national"))
            .alias("disposal_geography_level"),
        )
        .with_columns(
            (
                pl.col("removed_roof_mass_lbs")
                / 2000.0
                * pl.col("disposal_fee_usd_per_short_ton")
            ).alias("disposal_cost_usd")
        )
    )


def _build_landfill_carbon_factors(
    carbon_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    mapping = pl.read_csv(mapping_path).filter(
        (pl.col("mapping_domain") == "roof_material")
        & (pl.col("source_dataset_id") == "carbon_eol")
        & (pl.col("source_column") == "Roofing Material Type")
    ).select(
        pl.col("source_value").alias("Roofing Material Type"),
        "canonical_id",
        "mapping_status",
    )
    members = pl.read_csv(class_map_path).filter(
        (pl.col("mapping_version") == "v1.0.0")
        & pl.col("aggregation_eligible")
    ).select(
        "canonical_id",
        pl.col("official_class_id").alias("official_material_id"),
    )
    factors = pl.read_csv(
        carbon_path,
        null_values=["NA — not published in Table 9", ""],
    ).select("Roofing Material Type", "Landfilling EF (kg CO2e/lb)")
    resolved = members.join(mapping, on="canonical_id", how="left", validate="1:1").join(
        factors, on="Roofing Material Type", how="left", validate="m:1"
    )
    if resolved.filter(pl.col("Landfilling EF (kg CO2e/lb)").is_null()).height:
        raise ValueError("landfill carbon mapping does not resolve every core member")
    return resolved.group_by("official_material_id").agg(
        pl.col("Landfilling EF (kg CO2e/lb)").mean().alias(
            "landfill_kg_co2e_per_lb"
        ),
        (~pl.col("mapping_status").is_in(["approved_exact", "approved_shared_bucket"]))
        .any()
        .alias("carbon_factor_provisional"),
    )


def _require_columns(frame: pl.DataFrame, columns: tuple, name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")