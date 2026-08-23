"""Material reference lookups for the CADENCE economics pipeline."""

from pathlib import Path

import polars as pl

from cadence.economics.contracts import END_YEAR, OFFICIAL_MATERIAL_CLASSES, START_YEAR

ASSET_GEOGRAPHY_COLUMNS = ("asset_id", "zip_code", "cbsa_code", "state_code")
EXPANDED_PRICE_CLASSES = {
    "asphalt_3_tab": "OFFICIAL_ASPHALT",
    "asphalt_architectural": "OFFICIAL_ASPHALT",
    "metal_corrugated_panel": "OFFICIAL_METAL",
    "tile_proxy": "OFFICIAL_TILE",
}
GEOGRAPHIC_MISSING_MEMBERS = {
    "OFFICIAL_ASPHALT": ["MAT_ASPHALT_PREMIUM_ARCH"],
    "OFFICIAL_METAL": ["MAT_METAL_STANDING_SEAM", "MAT_METAL_TILE_SHINGLE"],
    "OFFICIAL_TILE": ["MAT_TILE_CLAY", "MAT_TILE_CONCRETE"],
}


def build_geographic_material_costs(
    zcta_dimension: pl.DataFrame,
    expanded_price_path: Path,
    escalation_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Project modeled county material prices to annual ZCTA class costs."""
    _require_columns(zcta_dimension, ("zcta5", "county_fips"), "zcta_dimension")
    geography = zcta_dimension.select("zcta5", "county_fips")
    if geography["zcta5"].n_unique() != geography.height:
        raise ValueError("zcta_dimension must contain unique zcta5 values")

    expanded = pl.read_csv(
        expanded_price_path,
        schema_overrides={
            "county_fips": pl.String,
            "source_county_fips": pl.String,
            "source_zip_code": pl.String,
        },
    ).filter(pl.col("material_class").is_in(EXPANDED_PRICE_CLASSES))
    duplicate_prices = expanded.group_by("county_fips", "material_class").len().filter(
        pl.col("len") != 1
    )
    if duplicate_prices.height:
        raise ValueError("expanded material prices must be unique by county and class")

    class_prices = (
        expanded.with_columns(
            pl.col("material_class")
            .replace_strict(EXPANDED_PRICE_CLASSES)
            .alias("official_material_id")
        )
        .group_by("county_fips", "official_material_id")
        .agg(
            pl.col("median_price_per_square").mean().alias(
                "source_material_2026_usd_per_square"
            ),
            pl.col("material_class").sort().alias("contributing_price_classes"),
            pl.col("source_zip_code").drop_nulls().unique().sort().alias(
                "material_source_zip_codes"
            ),
            pl.col("source_county_fips").drop_nulls().unique().sort().alias(
                "material_source_county_fips"
            ),
            pl.col("estimate_method").unique().sort().alias(
                "material_price_methods"
            ),
            pl.col("ratio_was_capped").fill_null(False).any().alias(
                "material_gdp_ratio_was_capped"
            ),
            pl.col("assumption_version").drop_nulls().first().alias(
                "material_assumption_version"
            ),
        )
        .with_columns(
            (pl.col("source_material_2026_usd_per_square") / 100.0).alias(
                "source_material_2026_usd_per_sqft"
            ),
            pl.lit("county_modeled").alias("material_source_level"),
            pl.col("county_fips").alias("material_source_id"),
            pl.lit(1).alias("maximum_fallback_rank"),
            pl.when(pl.col("official_material_id") == "OFFICIAL_TILE")
            .then(pl.lit("modeled_tile_proxy"))
            .otherwise(pl.lit("modeled_county_price"))
            .alias("material_price_status"),
            pl.when(pl.col("official_material_id") == "OFFICIAL_TILE")
            .then(pl.lit("low"))
            .otherwise(pl.lit("medium"))
            .alias("material_price_confidence"),
            pl.col("official_material_id")
            .replace_strict(GEOGRAPHIC_MISSING_MEMBERS)
            .alias("missing_member_ids"),
        )
    )
    county_zcta_prices = geography.join(
        class_prices,
        on="county_fips",
        how="inner",
        validate="m:m",
    )
    covered_counties = class_prices.select("county_fips").unique()
    fallback_geography = geography.join(
        covered_counties,
        on="county_fips",
        how="anti",
    )
    if fallback_geography.height:
        _require_columns(zcta_dimension, ("state_code",), "zcta_dimension")
        fallback_geography = fallback_geography.join(
            zcta_dimension.select("zcta5", "state_code"),
            on="zcta5",
            how="left",
            validate="1:1",
        )
    fallback_prices = _build_state_national_material_fallbacks(
        fallback_geography,
        expanded_price_path.parent,
    )
    zcta_prices = pl.concat(
        [county_zcta_prices, fallback_prices],
        how="diagonal_relaxed",
    )
    expected_rows = geography.height * len(OFFICIAL_MATERIAL_CLASSES)
    if (
        zcta_prices.height != expected_rows
        or zcta_prices.filter(
            pl.col("source_material_2026_usd_per_sqft").is_null()
        ).height
    ):
        raise ValueError("expanded material prices do not cover every ZCTA county")

    growth = build_material_growth_lookup(
        escalation_path,
        mapping_path,
        class_map_path,
    )
    return (
        zcta_prices.join(
            growth,
            on="official_material_id",
            how="left",
            validate="m:m",
        )
        .with_columns(
            (
                pl.col("source_material_2026_usd_per_sqft")
                * pl.col("material_growth_factor")
            ).alias("source_material_usd_per_sqft")
        )
        .sort("zcta5", "year", "official_material_id")
    )


def _build_state_national_material_fallbacks(
    fallback_geography: pl.DataFrame,
    price_root: Path,
) -> pl.DataFrame:
    if fallback_geography.is_empty():
        return pl.DataFrame()
    _require_columns(
        fallback_geography,
        ("zcta5", "county_fips", "state_code"),
        "fallback_geography",
    )
    state = _read_price_file(
        price_root / "home_depot_material_price_by_state.csv"
    ).rename({"state": "state_code"})
    national = _read_price_file(
        price_root / "home_depot_material_price_national.csv"
    )
    state = _add_tile_proxy(state).join(
        fallback_geography,
        on="state_code",
        how="inner",
    ).with_columns(
        pl.lit(2).alias("fallback_rank"),
        pl.lit("state").alias("material_source_level"),
        pl.col("state_code").alias("material_source_id"),
    )
    national = _add_tile_proxy(national).join(
        fallback_geography,
        how="cross",
    ).with_columns(
        pl.lit(3).alias("fallback_rank"),
        pl.lit("national").alias("material_source_level"),
        pl.lit("US").alias("material_source_id"),
    )
    selected = (
        pl.concat([state, national], how="diagonal_relaxed")
        .filter(pl.col("material_class").is_in(EXPANDED_PRICE_CLASSES))
        .sort("zcta5", "material_class", "fallback_rank")
        .unique(
            ["zcta5", "material_class"],
            keep="first",
            maintain_order=True,
        )
        .with_columns(
            pl.col("material_class")
            .replace_strict(EXPANDED_PRICE_CLASSES)
            .alias("official_material_id")
        )
    )
    result = (
        selected.group_by(
            "zcta5", "county_fips", "state_code", "official_material_id"
        )
        .agg(
            pl.col("median_price_per_square").mean().alias(
                "source_material_2026_usd_per_square"
            ),
            pl.col("material_class").sort().alias("contributing_price_classes"),
            pl.col("material_source_level").unique().sort().alias(
                "material_source_levels"
            ),
            pl.col("material_source_id").unique().sort().alias(
                "material_source_ids"
            ),
            pl.col("fallback_rank").max().alias("maximum_fallback_rank"),
            pl.col("estimate_method").unique().sort().alias(
                "material_price_methods"
            ),
            pl.col("scrape_date").drop_nulls().first(),
            pl.col("retailer").drop_nulls().first(),
        )
        .with_columns(
            (pl.col("source_material_2026_usd_per_square") / 100.0).alias(
                "source_material_2026_usd_per_sqft"
            ),
            pl.when(pl.col("material_source_levels").list.len() == 1)
            .then(pl.col("material_source_levels").list.first())
            .otherwise(pl.lit("mixed_state_national"))
            .alias("material_source_level"),
            pl.when(pl.col("material_source_ids").list.len() == 1)
            .then(pl.col("material_source_ids").list.first())
            .otherwise(pl.lit("mixed"))
            .alias("material_source_id"),
            pl.lit([], dtype=pl.List(pl.String)).alias(
                "material_source_zip_codes"
            ),
            pl.lit([], dtype=pl.List(pl.String)).alias(
                "material_source_county_fips"
            ),
            pl.lit(False).alias("material_gdp_ratio_was_capped"),
            pl.lit("state-national-fallback-v1.0.0").alias(
                "material_assumption_version"
            ),
            pl.col("official_material_id")
            .replace_strict(GEOGRAPHIC_MISSING_MEMBERS)
            .alias("missing_member_ids"),
        )
        .with_columns(
            pl.when(pl.col("official_material_id") == "OFFICIAL_TILE")
            .then(
                pl.concat_str(
                    pl.lit("modeled_tile_proxy_"),
                    pl.col("material_source_level"),
                )
            )
            .otherwise(
                pl.concat_str(
                    pl.lit("observed_"),
                    pl.col("material_source_level"),
                    pl.lit("_fallback"),
                )
            )
            .alias("material_price_status"),
            pl.when(
                (pl.col("official_material_id") == "OFFICIAL_TILE")
                | (pl.col("maximum_fallback_rank") >= 3)
            )
            .then(pl.lit("low"))
            .otherwise(pl.lit("medium"))
            .alias("material_price_confidence"),
        )
        .drop("material_source_levels", "material_source_ids")
    )
    expected_rows = fallback_geography.height * len(OFFICIAL_MATERIAL_CLASSES)
    if result.height != expected_rows:
        raise ValueError("state/national material fallback does not resolve all classes")
    return result


def _add_tile_proxy(prices: pl.DataFrame) -> pl.DataFrame:
    tile = prices.filter(
        pl.col("material_class") == "metal_corrugated_panel"
    ).with_columns(
        pl.lit("tile_proxy").alias("material_class"),
        (pl.col("median_price_per_square") * 1.2).alias(
            "median_price_per_square"
        ),
        pl.lit("derived_tile_1.2_from_selected_metal").alias("estimate_method"),
    )
    observed = prices.with_columns(
        pl.lit("observed_exact_material").alias("estimate_method")
    )
    return pl.concat([observed, tile], how="diagonal_relaxed")


def build_material_price_lookup(
    assets: pl.DataFrame,
    price_root: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Resolve exact subtype prices by geography, then consolidate official classes."""
    _require_columns(assets, ASSET_GEOGRAPHY_COLUMNS, "assets")
    asset_geography = assets.select(ASSET_GEOGRAPHY_COLUMNS).with_columns(
        pl.col("zip_code").cast(pl.String).str.zfill(5),
        pl.col("cbsa_code").cast(pl.String).str.zfill(5),
        pl.col("state_code").cast(pl.String).str.to_uppercase(),
    )
    source_map = _domain_source_map(mapping_path, "material_price", "material_class")
    core_members = _core_members(class_map_path)

    zip_prices = _read_price_file(price_root / "home_depot_material_price_by_zip.csv").join(
        asset_geography,
        on="zip_code",
        how="inner",
    ).with_columns(
        pl.lit(1).alias("fallback_rank"),
        pl.lit("zip").alias("price_geography_level"),
        pl.col("zip_code").alias("price_geography_id"),
    )
    cbsa_prices = _read_price_file(
        price_root / "home_depot_material_price_by_cbsa.csv"
    ).join(asset_geography, on="cbsa_code", how="inner").with_columns(
        pl.lit(2).alias("fallback_rank"),
        pl.lit("cbsa").alias("price_geography_level"),
        pl.col("cbsa_code").alias("price_geography_id"),
    )
    state_prices = _read_price_file(
        price_root / "home_depot_material_price_by_state.csv"
    ).rename({"state": "state_code"}).join(
        asset_geography, on="state_code", how="inner"
    ).with_columns(
        pl.lit(3).alias("fallback_rank"),
        pl.lit("state").alias("price_geography_level"),
        pl.col("state_code").alias("price_geography_id"),
    )
    national_prices = _read_price_file(
        price_root / "home_depot_material_price_national.csv"
    ).join(asset_geography.select("asset_id"), how="cross").with_columns(
        pl.lit(4).alias("fallback_rank"),
        pl.lit("national").alias("price_geography_level"),
        pl.lit("US").alias("price_geography_id"),
    )
    selected = (
        pl.concat(
            [zip_prices, cbsa_prices, state_prices, national_prices],
            how="diagonal_relaxed",
        )
        .join(source_map, left_on="material_class", right_on="source_value", how="inner")
        .sort(["asset_id", "canonical_id", "fallback_rank"])
        .unique(subset=["asset_id", "canonical_id"], keep="first", maintain_order=True)
        .select(
            "asset_id",
            "canonical_id",
            "median_price_per_square",
            "scrape_date",
            "retailer",
            "fallback_rank",
            "price_geography_level",
            "price_geography_id",
        )
    )
    member_matrix = (
        asset_geography.select("asset_id")
        .join(core_members, how="cross")
        .join(selected, on=["asset_id", "canonical_id"], how="left", validate="1:1")
    )
    return (
        member_matrix.group_by("asset_id", "official_material_id")
        .agg(
            pl.col("median_price_per_square").mean().alias(
                "source_material_2026_usd_per_square"
            ),
            pl.col("canonical_id")
            .filter(pl.col("median_price_per_square").is_not_null())
            .sort()
            .alias("contributing_canonical_ids"),
            pl.col("canonical_id")
            .filter(pl.col("median_price_per_square").is_null())
            .sort()
            .alias("missing_member_ids"),
            pl.col("price_geography_level")
            .filter(pl.col("median_price_per_square").is_not_null())
            .alias("member_price_geography_levels"),
            pl.col("fallback_rank").max().alias("maximum_fallback_rank"),
            pl.col("scrape_date").drop_nulls().first(),
            pl.col("retailer").drop_nulls().first(),
        )
        .with_columns(
            (
                pl.col("source_material_2026_usd_per_square") / 100.0
            ).alias("source_material_2026_usd_per_sqft"),
            pl.col("missing_member_ids").list.len().alias("missing_member_count"),
            pl.col("contributing_canonical_ids").list.len().alias("contributor_count"),
        )
        .with_columns(
            pl.when(pl.col("contributor_count") == 0)
            .then(pl.lit("blocked_without_override"))
            .when(pl.col("missing_member_count") > 0)
            .then(pl.lit("partial_source_coverage"))
            .otherwise(pl.lit("complete_source_coverage"))
            .alias("material_price_status"),
            pl.lit("v1.0.0").alias("mapping_version"),
        )
        .sort(["asset_id", "official_material_id"])
    )


def build_material_growth_lookup(
    escalation_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Build annual cumulative real material growth factors by official class."""
    source_map = _domain_source_map(
        mapping_path, "material_escalation", "material_class"
    )
    core_members = _core_members(class_map_path)
    rates = pl.read_csv(escalation_path).join(
        source_map, left_on="material_class", right_on="source_value", how="inner"
    )
    classes = (
        core_members.join(rates, on="canonical_id", how="left", validate="1:1")
        .group_by("official_material_id")
        .agg(
            pl.col("annual_escalation_factor").mean(),
            pl.col("canonical_id")
            .filter(pl.col("annual_escalation_factor").is_null())
            .sort()
            .alias("missing_growth_member_ids"),
            pl.col("series_id").drop_nulls().unique().sort().alias("series_ids"),
        )
    )
    years = pl.DataFrame(
        {"year": pl.int_range(START_YEAR, END_YEAR + 1, eager=True)}
    )
    return classes.join(years, how="cross").with_columns(
        pl.col("annual_escalation_factor")
        .pow(pl.col("year") - START_YEAR)
        .alias("material_growth_factor"),
        pl.col("missing_growth_member_ids").list.len().gt(0).alias(
            "material_growth_incomplete"
        ),
    ).sort(["official_material_id", "year"])


def build_material_mass_lookup(
    mass_path: Path,
    mapping_path: Path,
    class_map_path: Path,
) -> pl.DataFrame:
    """Apply approved shared values and consolidate class mass in pounds per sqft."""
    source_map = _domain_source_map(mapping_path, "material_mass", "asset_class")
    core_members = _core_members(class_map_path)
    mapped = source_map.join(
        pl.read_csv(mass_path), left_on="source_value", right_on="asset_class", how="left"
    )
    result = core_members.join(mapped, on="canonical_id", how="left", validate="1:1")
    if result.filter(pl.col("weight_per_square_lbs").is_null()).height:
        raise ValueError("material mass mapping does not resolve every core member")
    return result.group_by("official_material_id").agg(
        pl.col("weight_per_square_lbs").mean(),
        pl.col("weight_per_sqft_lbs").mean(),
        pl.col("canonical_id").sort().alias("contributing_canonical_ids"),
        pl.col("relationship")
        .is_in(["shared_bucket", "candidate_shared_bucket"])
        .any()
        .alias("shared_mass_value_applied"),
    ).sort("official_material_id")


def _read_price_file(path: Path) -> pl.DataFrame:
    schema = {
        "zip_code": pl.String,
        "cbsa_code": pl.String,
        "state": pl.String,
    }
    frame = pl.read_csv(path, schema_overrides=schema)
    expressions = []
    if "zip_code" in frame.columns:
        expressions.append(pl.col("zip_code").str.zfill(5))
    if "cbsa_code" in frame.columns:
        expressions.append(pl.col("cbsa_code").str.zfill(5))
    return frame.with_columns(expressions)


def _domain_source_map(
    mapping_path: Path,
    domain: str,
    source_column: str,
) -> pl.DataFrame:
    mapping = pl.read_csv(mapping_path)
    selected = mapping.filter(
        (pl.col("mapping_domain") == "roof_material")
        & (pl.col("source_dataset_id") == domain)
        & (pl.col("source_column") == source_column)
        & pl.col("mapping_status").str.starts_with("approved")
    ).select("source_value", "canonical_id", "relationship", "confidence")
    if selected["canonical_id"].n_unique() != selected.height:
        raise ValueError(f"{domain} source map contains duplicate canonical IDs")
    return selected


def _core_members(class_map_path: Path) -> pl.DataFrame:
    members = pl.read_csv(class_map_path).filter(
        (pl.col("mapping_version") == "v1.0.0")
        & pl.col("aggregation_eligible")
    ).select(
        "canonical_id",
        pl.col("official_class_id").alias("official_material_id"),
    )
    found_classes = set(members["official_material_id"].to_list())
    if found_classes != set(OFFICIAL_MATERIAL_CLASSES):
        raise ValueError("class map must contain exactly three official material classes")
    return members


def _require_columns(frame: pl.DataFrame, columns: tuple, name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")