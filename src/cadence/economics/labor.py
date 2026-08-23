"""Vectorized installation labor costs for official roof material classes."""

from pathlib import Path

import polars as pl

from cadence.economics.contracts import (
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
    OFFICIAL_MATERIAL_CLASSES,
)

LABOR_ASSET_COLUMNS = (
    "asset_id",
    "roof_area_sqft",
    "labor_market_id",
    "roof_shape",
    "roof_deck_attachment",
    "roof_wall_connection",
)


def build_geographic_annual_labor_costs(
    zcta_dimension: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    productivity_path: Path,
    wage_projection_path: Path,
    wage_base_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Calculate annual installation labor by ZCTA and roof scenario."""
    _require_columns(
        zcta_dimension,
        ("zcta5", "labor_market_id"),
        "zcta_dimension",
    )
    geography = zcta_dimension.select("zcta5", "labor_market_id")
    if geography["zcta5"].n_unique() != geography.height:
        raise ValueError("zcta_dimension must contain unique zcta5 values")

    scenarios = pl.DataFrame(
        [scenario.model_dump() for scenario in config.roof_scenarios]
    )
    dimensions = geography.select("labor_market_id").unique().join(
        scenarios,
        how="cross",
    )
    productivity = _load_class_productivity(
        productivity_path,
        mapping_path,
        class_map_path,
    )
    dimension_productivity = dimensions.join(
        productivity,
        on=[
            "roof_shape",
            "roof_deck_attachment",
            "roof_wall_connection",
            "size_bucket",
        ],
        how="left",
        validate="m:m",
    )
    if dimension_productivity.filter(pl.col("occupation_code").is_null()).height:
        raise ValueError(
            "labor productivity could not be resolved for one or more roof scenarios"
        )

    costs = dimension_productivity.join(
        _load_annual_wages(wage_projection_path, wage_base_path),
        on=["labor_market_id", "occupation_code"],
        how="left",
        validate="m:m",
    )
    missing_wages = costs.filter(pl.col("hourly_wage_usd").is_null())
    if missing_wages.height:
        missing_ids = missing_wages["labor_market_id"].drop_nulls().unique().to_list()
        raise ValueError(
            "constrained wage projections are missing for labor_market_id values: "
            + ", ".join(sorted(missing_ids))
        )

    return (
        costs.with_columns(
            (
                pl.col("base_person_hours_per_sqft") * pl.col("hourly_wage_usd")
            ).alias("variable_labor_usd_per_sqft"),
            (
                pl.col("startup_person_hours")
                * pl.col("hourly_wage_usd")
                / pl.col("roof_area_sqft")
            ).alias("startup_labor_usd_per_sqft"),
        )
        .group_by(
            "labor_market_id",
            "roof_scenario_id",
            "roof_area_sqft",
            "year",
            "official_material_id",
        )
        .agg(
            pl.col("variable_labor_usd_per_sqft").sum(),
            pl.col("startup_labor_usd_per_sqft").sum(),
            pl.col("hourly_wage_usd").mul(pl.col("occupation_labor_share")).sum().alias(
                "weighted_hourly_wage_usd"
            ),
            pl.col("wage_is_imputed").fill_null(True).any(),
            pl.col("wage_source_level").drop_nulls().unique().sort().alias(
                "wage_source_levels"
            ),
            pl.col("wage_source_area").drop_nulls().unique().sort().alias(
                "wage_source_areas"
            ),
            pl.col("labor_projection_version").drop_nulls().first(),
            pl.lit(True).alias("labor_productivity_provisional"),
        )
        .with_columns(
            (
                pl.col("variable_labor_usd_per_sqft")
                + pl.col("startup_labor_usd_per_sqft")
            ).alias("source_labor_usd_per_sqft"),
            pl.lit("labor_market").alias("labor_source_level"),
            pl.col("labor_market_id").alias("labor_source_id"),
        )
        .join(
            geography,
            on="labor_market_id",
            how="inner",
            validate="m:m",
        )
        .sort("zcta5", "year", "official_material_id", "roof_scenario_id")
    )


def build_annual_labor_costs(
    assets: pl.DataFrame,
    config: EconomicsRunConfig,
    productivity_path: Path,
    wage_projection_path: Path,
    wage_base_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Calculate annual occupation-weighted installation labor for every option."""
    _require_columns(assets, LABOR_ASSET_COLUMNS, "assets")
    resolved_assets = _resolve_asset_labor_dimensions(assets, config)
    productivity = _load_class_productivity(
        productivity_path, mapping_path, class_map_path
    )
    asset_productivity = (
        resolved_assets.join(
            productivity,
            on=[
                "roof_shape",
                "roof_deck_attachment",
                "roof_wall_connection",
                "size_bucket",
            ],
            how="left",
            validate="m:m",
        )
    )
    unresolved = asset_productivity.filter(pl.col("occupation_code").is_null())
    if unresolved.height:
        raise ValueError("labor productivity could not be resolved for one or more assets")

    costs = (
        asset_productivity.join(
            _load_annual_wages(wage_projection_path, wage_base_path),
            on=["labor_market_id", "occupation_code"],
            how="left",
            validate="m:m",
        )
    )
    missing_wages = costs.filter(pl.col("hourly_wage_usd").is_null())
    if missing_wages.height:
        missing_ids = missing_wages["labor_market_id"].drop_nulls().unique().to_list()
        raise ValueError(
            "constrained wage projections are missing for labor_market_id values: "
            + ", ".join(sorted(missing_ids))
        )
    return (
        costs.with_columns(
            (
                pl.col("base_person_hours_per_sqft") * pl.col("hourly_wage_usd")
            ).alias("variable_labor_usd_per_sqft"),
            (
                pl.col("startup_person_hours")
                * pl.col("hourly_wage_usd")
                / pl.col("roof_area_sqft")
            ).alias("startup_labor_usd_per_sqft"),
        )
        .group_by("asset_id", "year", "official_material_id")
        .agg(
            pl.col("variable_labor_usd_per_sqft").sum(),
            pl.col("startup_labor_usd_per_sqft").sum(),
            pl.col("hourly_wage_usd").mul(pl.col("occupation_labor_share")).sum().alias(
                "weighted_hourly_wage_usd"
            ),
            pl.col("wage_is_imputed").fill_null(True).any(),
            pl.col("wage_source_level").drop_nulls().unique().sort().alias(
                "wage_source_levels"
            ),
            pl.col("labor_projection_version").drop_nulls().first(),
            pl.col("roof_shape_default_applied").first(),
            pl.col("roof_deck_attachment_default_applied").first(),
            pl.col("roof_wall_connection_default_applied").first(),
            pl.lit(True).alias("labor_productivity_provisional"),
        )
        .with_columns(
            (
                pl.col("variable_labor_usd_per_sqft")
                + pl.col("startup_labor_usd_per_sqft")
            ).alias("source_labor_usd_per_sqft")
        )
        .with_columns(
            (
                pl.col("source_labor_usd_per_sqft")
                / pl.col("source_labor_usd_per_sqft")
                .filter(pl.col("year") == 2026)
                .first()
                .over(["asset_id", "official_material_id"])
            ).alias("labor_growth_factor")
        )
        .sort(["asset_id", "year", "official_material_id"])
    )


def _resolve_asset_labor_dimensions(
    assets: pl.DataFrame, config: EconomicsRunConfig
) -> pl.DataFrame:
    return assets.select(LABOR_ASSET_COLUMNS).with_columns(
        pl.col("roof_shape").is_null().alias("roof_shape_default_applied"),
        pl.col("roof_deck_attachment")
        .is_null()
        .alias("roof_deck_attachment_default_applied"),
        pl.col("roof_wall_connection")
        .is_null()
        .alias("roof_wall_connection_default_applied"),
        pl.col("roof_shape").fill_null(config.default_roof_shape),
        pl.col("roof_deck_attachment").fill_null(
            config.default_roof_deck_attachment
        ),
        pl.col("roof_wall_connection").fill_null(
            config.default_roof_wall_connection
        ),
        pl.when(pl.col("roof_area_sqft") < 1500)
        .then(pl.lit("small"))
        .when(pl.col("roof_area_sqft") <= 3000)
        .then(pl.lit("medium"))
        .otherwise(pl.lit("large"))
        .alias("size_bucket"),
    )


def _load_class_productivity(
    productivity_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    mapping = pl.read_csv(mapping_path).filter(
        (pl.col("mapping_domain") == "roof_material")
        & (pl.col("source_dataset_id") == "labor_productivity")
        & (pl.col("source_column") == "roof_type")
        & (pl.col("mapping_status") == "approved_exact")
    ).select(
        pl.col("source_value").alias("roof_type"),
        "canonical_id",
    )
    members = pl.read_csv(class_map_path).filter(
        (pl.col("mapping_version") == "v1.0.0")
        & pl.col("aggregation_eligible")
    ).select(
        "canonical_id",
        pl.col("official_class_id").alias("official_material_id"),
    )
    productivity = (
        pl.read_csv(productivity_path)
        .join(mapping, on="roof_type", how="inner", validate="m:1")
        .join(members, on="canonical_id", how="inner", validate="m:1")
    )
    keys = [
        "official_material_id",
        "roof_shape",
        "roof_deck_attachment",
        "roof_wall_connection",
        "size_bucket",
        "occupation_code",
    ]
    consolidated = productivity.group_by(keys).agg(
        pl.col("base_person_hours_per_sqft").mean(),
        pl.col("startup_person_hours").mean(),
        pl.col("occupation_labor_share").mean(),
        pl.col("canonical_id").n_unique().alias("contributor_count"),
    )
    found_classes = set(consolidated["official_material_id"].unique().to_list())
    if found_classes != set(OFFICIAL_MATERIAL_CLASSES):
        raise ValueError("labor productivity does not resolve all official classes")
    return consolidated


def _load_annual_wages(
    wage_projection_path: Path,
    wage_base_path: Path,
) -> pl.DataFrame:
    wages = pl.read_parquet(wage_projection_path).select(
        pl.col("AREA").alias("labor_market_id"),
        pl.col("OCC_CODE").alias("occupation_code"),
        pl.col("PROJECTION_YEAR").alias("year"),
        pl.col("H_MEDIAN_CONSTRAINED_PROJECTED_WAGE").alias("hourly_wage_usd"),
        pl.col("MODEL_VERSION").alias("labor_projection_version"),
    )
    base_provenance = (
        pl.read_parquet(wage_base_path)
        .filter(pl.col("WAGE_METRIC") == "H_MEDIAN")
        .select(
            pl.col("AREA").alias("labor_market_id"),
            pl.col("OCC_CODE").alias("occupation_code"),
            pl.col("SOURCE_LEVEL").alias("wage_source_level"),
            pl.col("SOURCE_AREA").alias("wage_source_area"),
            pl.col("IS_IMPUTED").alias("wage_is_imputed"),
        )
        .unique(["labor_market_id", "occupation_code"])
    )
    return wages.join(
        base_provenance,
        on=["labor_market_id", "occupation_code"],
        how="left",
        validate="m:1",
    )


def _require_columns(frame: pl.DataFrame, columns: tuple, name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")