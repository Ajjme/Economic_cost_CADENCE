"""Vectorized annual replacement and provisional repair calculations."""

from typing import Iterable, Optional

import polars as pl

from cadence.economics.contracts import (
    CostStream,
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
    OFFICIAL_MATERIAL_CLASSES,
)

ASSET_COLUMNS = (
    "asset_id",
    "official_current_material_id",
    "roof_area_sqft",
    "replacement_value_usd",
)
GROWTH_COLUMNS = (
    "asset_id",
    "year",
    "official_material_id",
    "material_growth_factor",
    "labor_growth_factor",
)
SOURCE_COST_COLUMNS = (
    "asset_id",
    "year",
    "official_material_id",
    "source_material_usd_per_sqft",
    "source_labor_usd_per_sqft",
)
DAMAGE_COLUMNS = (
    "asset_id",
    "year",
    "official_material_id",
    "expected_damage_ratio",
)
EXTERNAL_COST_COLUMNS = (
    "asset_id",
    "year",
    "official_material_id",
    "disposal_cost_usd",
    "carbon_cost_usd",
    "expected_loss_of_use_usd",
)
GEOGRAPHIC_COST_KEY = (
    "zcta5",
    "year",
    "official_material_id",
    "roof_scenario_id",
)
GEOGRAPHIC_SOURCE_COST_COLUMNS = (
    *GEOGRAPHIC_COST_KEY,
    "roof_area_sqft",
    "source_material_usd_per_sqft",
    "source_labor_usd_per_sqft",
)


def build_geographic_installed_costs(
    annual_source_costs: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
) -> pl.DataFrame:
    """Build national installed unit and representative-roof costs by ZCTA."""
    _require_columns(
        annual_source_costs,
        GEOGRAPHIC_SOURCE_COST_COLUMNS,
        "annual_source_costs",
    )
    _validate_geographic_source_costs(annual_source_costs, config)

    return (
        annual_source_costs.with_columns(
            (
                pl.col("source_material_usd_per_sqft")
                + pl.col("source_labor_usd_per_sqft")
            ).alias("installed_cost_usd_per_sqft")
        )
        .with_columns(
            (
                pl.col("installed_cost_usd_per_sqft")
                * pl.col("roof_area_sqft")
            ).alias("representative_roof_installed_cost_usd"),
            pl.lit("real_2026_usd").alias("dollar_basis"),
        )
        .sort(GEOGRAPHIC_COST_KEY)
    )


def build_annual_roof_option_costs(
    assets: pl.DataFrame,
    annual_growth: pl.DataFrame,
    config: EconomicsRunConfig,
    source_costs: Optional[pl.DataFrame] = None,
    annual_damage: Optional[pl.DataFrame] = None,
    external_costs: Optional[pl.DataFrame] = None,
) -> pl.DataFrame:
    """Build one annual cost row for each asset and official material option."""
    _require_columns(assets, ASSET_COLUMNS, "assets")
    _require_columns(annual_growth, GROWTH_COLUMNS, "annual_growth")
    _validate_assets(assets)
    _validate_growth(annual_growth, assets, config)

    options = pl.DataFrame(
        {"official_material_id": list(OFFICIAL_MATERIAL_CLASSES)}
    )
    years = pl.DataFrame(
        {"year": pl.int_range(config.start_year, config.end_year + 1, eager=True)}
    )
    override_rows = [
        {
            "official_material_id": material_id,
            "override_2026_installed_usd_per_sqft": override.installed_usd_per_sqft,
            "override_material_share": override.material_share,
            "override_labor_share": override.labor_share,
        }
        for material_id, override in config.installed_cost_overrides.items()
    ]
    result = (
        assets.select(ASSET_COLUMNS)
        .join(years, how="cross")
        .join(options, how="cross")
        .join(
            annual_growth.select(GROWTH_COLUMNS),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        )
        .join(pl.DataFrame(override_rows), on="official_material_id", validate="m:1")
        .with_columns(
            (
                pl.col("override_material_share") * pl.col("material_growth_factor")
                + pl.col("override_labor_share") * pl.col("labor_growth_factor")
            ).alias("installed_growth_factor"),
            (
                pl.col("official_material_id")
                == pl.col("official_current_material_id")
            ).alias("is_current_roof_option"),
        )
        .with_columns(
            (
                pl.col("override_2026_installed_usd_per_sqft")
                * pl.col("installed_growth_factor")
            ).alias("class_override_installed_usd_per_sqft"),
            (
                pl.col("replacement_value_usd")
                * pl.col("installed_growth_factor")
            ).alias("projected_user_replacement_value_usd"),
        )
        .with_columns(
            pl.when(pl.col("is_current_roof_option"))
            .then(
                pl.col("projected_user_replacement_value_usd")
                / pl.col("roof_area_sqft")
            )
            .otherwise(pl.col("class_override_installed_usd_per_sqft"))
            .alias("operational_installed_usd_per_sqft"),
            pl.when(pl.col("is_current_roof_option"))
            .then(pl.lit("asset_replacement_value"))
            .otherwise(pl.lit("class_installed_override"))
            .alias("operational_cost_source"),
        )
        .with_columns(
            (
                pl.col("operational_installed_usd_per_sqft")
                * pl.col("roof_area_sqft")
            ).alias("installed_capex_usd")
        )
    )

    if source_costs is not None:
        _require_columns(source_costs, SOURCE_COST_COLUMNS, "source_costs")
        result = result.join(
            source_costs.select(SOURCE_COST_COLUMNS),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        ).with_columns(
            (
                pl.col("source_material_usd_per_sqft")
                + pl.col("source_labor_usd_per_sqft")
            ).alias("source_installed_usd_per_sqft")
        )
    else:
        result = result.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("source_material_usd_per_sqft"),
            pl.lit(None, dtype=pl.Float64).alias("source_labor_usd_per_sqft"),
            pl.lit(None, dtype=pl.Float64).alias("source_installed_usd_per_sqft"),
        )

    if external_costs is not None:
        _require_columns(external_costs, EXTERNAL_COST_COLUMNS, "external_costs")
        result = result.join(
            external_costs.select(EXTERNAL_COST_COLUMNS),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        )
    else:
        result = result.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("disposal_cost_usd"),
            pl.lit(None, dtype=pl.Float64).alias("carbon_cost_usd"),
            pl.lit(None, dtype=pl.Float64).alias("expected_loss_of_use_usd"),
        )

    enabled = config.enabled_cost_streams
    result = result.with_columns(
        _enabled_cost(CostStream.DISPOSAL, "disposal_cost_usd", enabled).alias(
            "enabled_disposal_cost_usd"
        ),
        _enabled_cost(CostStream.CARBON, "carbon_cost_usd", enabled).alias(
            "enabled_carbon_cost_usd"
        ),
        _enabled_cost(CostStream.LOSS_OF_USE, "expected_loss_of_use_usd", enabled).alias(
            "enabled_loss_of_use_usd"
        ),
    ).with_columns(
        (
            pl.col("installed_capex_usd")
            + pl.col("enabled_disposal_cost_usd")
            + pl.col("enabled_carbon_cost_usd")
        ).alias("replacement_economic_cost_usd"),
    ).with_columns(
        (
            pl.col("replacement_economic_cost_usd")
            + pl.col("enabled_loss_of_use_usd")
        ).alias("enabled_economic_total_usd")
    )

    if annual_damage is not None:
        _require_columns(annual_damage, DAMAGE_COLUMNS, "annual_damage")
        invalid_damage = annual_damage.filter(
            pl.col("expected_damage_ratio").is_not_null()
            & ~pl.col("expected_damage_ratio").is_between(0.0, 1.0, closed="both")
        )
        if invalid_damage.height:
            raise ValueError("expected_damage_ratio must be between 0 and 1")
        result = result.join(
            annual_damage.select(DAMAGE_COLUMNS),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        )
    else:
        result = result.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("expected_damage_ratio")
        )

    return result.with_columns(
        (
            pl.col("installed_capex_usd") * pl.col("expected_damage_ratio")
        ).alias("provisional_repair_cost_usd"),
        pl.col("expected_damage_ratio").is_null().alias("repair_cost_incomplete"),
        pl.col("expected_loss_of_use_usd").is_null().alias("loss_of_use_incomplete"),
        pl.lit("real_2026_usd").alias("dollar_basis"),
        pl.lit(True).alias("tear_off_labor_excluded"),
    ).sort(["asset_id", "year", "official_material_id"])


def build_replacement_value_sanity_checks(
    annual_costs: pl.DataFrame,
    tolerance_percent: float,
) -> pl.DataFrame:
    """Compare the preferred asset value with source and class estimates."""
    current = annual_costs.filter(pl.col("is_current_roof_option"))
    source_total = pl.col("source_installed_usd_per_sqft") * pl.col("roof_area_sqft")
    override_total = (
        pl.col("class_override_installed_usd_per_sqft") * pl.col("roof_area_sqft")
    )
    return current.select(
        "asset_id",
        "year",
        "official_current_material_id",
        pl.col("projected_user_replacement_value_usd").alias("user_value_usd"),
        source_total.alias("source_computed_value_usd"),
        override_total.alias("class_override_value_usd"),
        (pl.col("projected_user_replacement_value_usd") - source_total).alias(
            "source_variance_usd"
        ),
        (
            100.0
            * (pl.col("projected_user_replacement_value_usd") - source_total)
            / source_total
        ).alias("source_variance_percent"),
        (
            100.0
            * (pl.col("projected_user_replacement_value_usd") - override_total)
            / override_total
        ).alias("override_variance_percent"),
    ).with_columns(
        (pl.col("source_variance_percent").abs() > tolerance_percent).alias(
            "source_outside_tolerance"
        ),
        pl.col("source_computed_value_usd").is_null().alias(
            "source_comparison_incomplete"
        ),
    )


def _enabled_cost(
    stream: CostStream,
    column: str,
    enabled: Iterable[CostStream],
) -> pl.Expr:
    if stream in enabled:
        return pl.col(column)
    return pl.lit(0.0)


def _require_columns(frame: pl.DataFrame, columns: Iterable[str], name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")


def _validate_assets(assets: pl.DataFrame) -> None:
    if assets["asset_id"].n_unique() != assets.height:
        raise ValueError("assets must contain unique asset_id values")
    if assets.filter(pl.col("roof_area_sqft") <= 0).height:
        raise ValueError("roof_area_sqft must be positive")
    invalid_materials = assets.filter(
        ~pl.col("official_current_material_id").is_in(OFFICIAL_MATERIAL_CLASSES)
    )
    if invalid_materials.height:
        raise ValueError("official_current_material_id contains an unsupported class")


def _validate_growth(
    growth: pl.DataFrame,
    assets: pl.DataFrame,
    config: EconomicsRunConfig,
) -> None:
    expected_rows = assets.height * (config.end_year - config.start_year + 1) * len(
        OFFICIAL_MATERIAL_CLASSES
    )
    keys = ["asset_id", "year", "official_material_id"]
    if growth.height != expected_rows or growth.select(keys).unique().height != expected_rows:
        raise ValueError("annual_growth must contain one row per asset, year, and class")
    if growth.filter(
        pl.col("material_growth_factor").is_null()
        | pl.col("labor_growth_factor").is_null()
        | (pl.col("material_growth_factor") <= 0)
        | (pl.col("labor_growth_factor") <= 0)
    ).height:
        raise ValueError("annual growth factors must be non-null and positive")


def _validate_geographic_source_costs(
    source_costs: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
) -> None:
    zcta_count = source_costs["zcta5"].n_unique()
    year_count = config.end_year - config.start_year + 1
    scenario_ids = [scenario.roof_scenario_id for scenario in config.roof_scenarios]
    expected_rows = (
        zcta_count
        * year_count
        * len(OFFICIAL_MATERIAL_CLASSES)
        * len(scenario_ids)
    )
    if (
        source_costs.height != expected_rows
        or source_costs.select(GEOGRAPHIC_COST_KEY).unique().height != expected_rows
    ):
        raise ValueError(
            "annual_source_costs must contain one row per ZCTA, year, class, and scenario"
        )

    invalid_dimensions = source_costs.filter(
        ~pl.col("year").is_between(config.start_year, config.end_year, closed="both")
        | ~pl.col("official_material_id").is_in(OFFICIAL_MATERIAL_CLASSES)
        | ~pl.col("roof_scenario_id").is_in(scenario_ids)
        | pl.col("zcta5").is_null()
        | (pl.col("zcta5").str.len_chars() != 5)
    )
    if invalid_dimensions.height:
        raise ValueError("annual_source_costs contains an unsupported dimension value")

    scenario_areas = pl.DataFrame(
        {
            "roof_scenario_id": scenario_ids,
            "configured_roof_area_sqft": [
                scenario.roof_area_sqft for scenario in config.roof_scenarios
            ],
        }
    )
    invalid_areas = (
        source_costs.select("roof_scenario_id", "roof_area_sqft")
        .join(scenario_areas, on="roof_scenario_id", validate="m:1")
        .filter(
            (pl.col("roof_area_sqft") - pl.col("configured_roof_area_sqft")).abs()
            > 1e-9
        )
    )
    if invalid_areas.height:
        raise ValueError("roof_area_sqft must match the configured roof scenario")

    invalid_costs = source_costs.filter(
        pl.col("source_material_usd_per_sqft").is_null()
        | pl.col("source_labor_usd_per_sqft").is_null()
        | (pl.col("source_material_usd_per_sqft") < 0)
        | (pl.col("source_labor_usd_per_sqft") < 0)
    )
    if invalid_costs.height:
        raise ValueError("geographic source costs must be non-null and nonnegative")