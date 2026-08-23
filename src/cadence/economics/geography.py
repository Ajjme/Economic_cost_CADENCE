"""Validated ID-only geography dimensions for national economics runs."""

from typing import Iterable

import polars as pl

ZCTA_DIMENSION_COLUMNS = (
    "zcta5",
    "state_code",
    "county_fips",
    "cbsa_code",
    "labor_market_id",
    "crosswalk_method",
    "source_vintage",
)


def build_zcta_dimension(crosswalk: pl.DataFrame) -> pl.DataFrame:
    """Validate and normalize one dominant geography assignment per Census ZCTA."""
    _require_columns(crosswalk, ZCTA_DIMENSION_COLUMNS, "crosswalk")
    dimension = crosswalk.select(ZCTA_DIMENSION_COLUMNS)

    if dimension["zcta5"].n_unique() != dimension.height:
        raise ValueError("crosswalk must contain exactly one row per zcta5")

    invalid = dimension.filter(
        pl.col("zcta5").is_null()
        | (pl.col("zcta5").str.len_chars() != 5)
        | ~pl.col("zcta5").str.contains(r"^\d{5}$")
        | pl.col("state_code").is_null()
        | (pl.col("state_code").str.len_chars() != 2)
        | pl.col("county_fips").is_null()
        | (pl.col("county_fips").str.len_chars() != 5)
        | ~pl.col("county_fips").str.contains(r"^\d{5}$")
        | pl.col("labor_market_id").is_null()
        | (pl.col("labor_market_id").str.len_chars() == 0)
        | pl.col("crosswalk_method").is_null()
        | (pl.col("crosswalk_method").str.len_chars() == 0)
        | pl.col("source_vintage").is_null()
        | (pl.col("source_vintage").str.len_chars() == 0)
    )
    if invalid.height:
        raise ValueError("crosswalk contains an invalid or unresolved ZCTA assignment")

    return dimension.sort("zcta5")


def _require_columns(frame: pl.DataFrame, columns: Iterable[str], name: str) -> None:
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")