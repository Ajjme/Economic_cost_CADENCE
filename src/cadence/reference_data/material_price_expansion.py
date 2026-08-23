"""Expand sparse Home Depot roof prices into county-level estimates."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import numpy as np
import polars as pl

GENERATOR_VERSION = "v1.0.0"
ASSUMPTION_VERSION = "county-gdp-nearest-anchor-v1.0.0"
EARTH_RADIUS_KM = 6371.0088

SOURCE_ZIP_TO_COUNTY = {
    "27705": "37063",
    "32459": "12131",
    "77001": "48201",
    "94582": "06013",
    "98004": "53033",
}

PRICE_COLUMNS = (
    "median_price_per_square",
    "p25_price_per_square",
    "p75_price_per_square",
    "min_price_per_square",
    "max_price_per_square",
    "median_bulk_price_per_square",
)
SOURCE_MATERIAL_CLASSES = (
    "asphalt_3_tab",
    "asphalt_architectural",
    "metal_corrugated_panel",
)

DEFAULT_PRICE_PATH = Path(
    "Data/Materials/Start_Year_2026/home_depot_material_price_by_zip.csv"
)
DEFAULT_GDP_PATH = Path("Data/Regional_Socioeconomic/GDP/county_gdp.csv")
DEFAULT_GEOMETRY_PATH = Path("Data/Climate_Zones/ClimateZones.shp")
DEFAULT_OUTPUT_PATH = Path(
    "Data/Materials/Start_Year_2026/home_depot_material_price_expanded.csv"
)


def normalize_county_geoid(value: object) -> str:
    """Normalize the climate-zone GEOID convention to a five-digit county FIPS."""
    text = str(value).strip()
    if text.startswith("G"):
        text = text[1:]
    if not text.isdigit() or len(text) > 5:
        raise ValueError(f"invalid county GEOID: {value!r}")
    return text.zfill(5)


def load_source_prices(path: Path) -> pl.DataFrame:
    """Load and validate observed ZIP-level material prices."""
    frame = pl.read_csv(path, schema_overrides={"zip_code": pl.String}).with_columns(
        pl.col("zip_code").str.zfill(5)
    )
    required = {
        "scrape_date",
        "retailer",
        "material_class",
        "zip_code",
        "product_count",
        "store_count",
        "median_bulk_discount_pct",
        *PRICE_COLUMNS,
    }
    _require_columns(frame, required, "source prices")
    frame = frame.filter(pl.col("material_class").is_in(SOURCE_MATERIAL_CLASSES))
    unsupported_zips = sorted(set(frame["zip_code"].to_list()) - set(SOURCE_ZIP_TO_COUNTY))
    if unsupported_zips:
        raise ValueError(f"source prices contain unmapped ZIP codes: {unsupported_zips}")
    if frame.is_duplicated().any() or frame.select(
        pl.struct("zip_code", "material_class").is_duplicated().any()
    ).item():
        raise ValueError("source prices contain duplicate ZIP/material rows")
    if frame.filter(
        pl.col("median_price_per_square").is_null()
        | ~pl.col("median_price_per_square").is_finite()
        | (pl.col("median_price_per_square") <= 0)
    ).height:
        raise ValueError("source median prices must be finite and positive")
    missing_materials = sorted(set(SOURCE_MATERIAL_CLASSES) - set(frame["material_class"]))
    if missing_materials:
        raise ValueError(f"source prices are missing material classes: {missing_materials}")
    return frame.sort(["material_class", "zip_code"])


def load_county_gdp(path: Path, year: int) -> pl.DataFrame:
    """Load usable county GDP-per-capita rows for one year."""
    frame = pl.read_csv(path, schema_overrides={"county_fips": pl.String})
    _require_columns(
        frame,
        {"county_fips", "county_name", "state_abbr", "year", "gdp_per_capita"},
        "county GDP",
    )
    selected = frame.filter(
        (pl.col("year") == year)
        & pl.col("state_abbr").is_not_null()
        & pl.col("gdp_per_capita").is_not_null()
        & pl.col("gdp_per_capita").is_finite()
        & (pl.col("gdp_per_capita") > 0)
    ).with_columns(
        pl.col("county_fips").str.zfill(5),
        pl.col("state_abbr").str.to_uppercase(),
    ).select("county_fips", "county_name", "state_abbr", "year", "gdp_per_capita")
    if not selected.height:
        raise ValueError(f"county GDP has no usable rows for {year}")
    if selected["county_fips"].n_unique() != selected.height:
        raise ValueError(f"county GDP contains duplicate usable county rows for {year}")
    invalid_fips = selected.filter(~pl.col("county_fips").str.contains(r"^\d{5}$"))
    if invalid_fips.height:
        raise ValueError("county GDP contains invalid county FIPS values")
    return selected.sort("county_fips")


def load_county_points(path: Path) -> pl.DataFrame:
    """Load one interior WGS84 point for each county geometry."""
    zones = gpd.read_file(path)
    missing = {"GEOID", "geometry"} - set(zones.columns)
    if missing:
        raise ValueError(f"county geometry is missing columns: {sorted(missing)}")
    if zones.crs is None:
        raise ValueError("county geometry has no declared CRS")
    zones = zones[["GEOID", "geometry"]].to_crs("EPSG:4326")
    if zones.geometry.isna().any() or zones.geometry.is_empty.any():
        raise ValueError("county geometry contains null or empty shapes")
    points = zones.geometry.representative_point()
    result = pl.DataFrame(
        {
            "county_fips": [normalize_county_geoid(value) for value in zones["GEOID"]],
            "latitude": points.y.to_numpy(),
            "longitude": points.x.to_numpy(),
        }
    )
    if result["county_fips"].n_unique() != result.height:
        raise ValueError("county geometry contains duplicate normalized county FIPS")
    return result.sort("county_fips")


def expand_material_prices(
    source_prices: pl.DataFrame,
    county_gdp: pl.DataFrame,
    county_points: pl.DataFrame,
    *,
    ratio_min: float = 0.6,
    ratio_max: float = 1.6,
    tile_multiplier: float = 1.2,
) -> pl.DataFrame:
    """Project observed material prices to every usable GDP county."""
    if not np.isfinite(ratio_min) or not np.isfinite(ratio_max) or ratio_min <= 0:
        raise ValueError("ratio bounds must be finite and positive")
    if ratio_min > ratio_max:
        raise ValueError("ratio_min cannot exceed ratio_max")
    if not np.isfinite(tile_multiplier) or tile_multiplier <= 0:
        raise ValueError("tile_multiplier must be finite and positive")

    _require_columns(
        county_points,
        {"county_fips", "latitude", "longitude"},
        "county points",
    )
    targets = county_gdp.join(county_points, on="county_fips", how="left", validate="1:1")
    unresolved = targets.filter(
        pl.col("latitude").is_null() | pl.col("longitude").is_null()
    )
    if unresolved.height:
        raise ValueError(
            "county geometry is missing GDP county FIPS: "
            + ", ".join(unresolved["county_fips"].to_list())
        )

    source_prices = source_prices.with_columns(
        pl.col("zip_code")
        .replace_strict(SOURCE_ZIP_TO_COUNTY)
        .alias("source_county_fips")
    )
    source_counties = set(source_prices["source_county_fips"].to_list())
    available_gdp = set(county_gdp["county_fips"].to_list())
    available_points = set(county_points["county_fips"].to_list())
    missing_sources = sorted(source_counties - available_gdp.intersection(available_points))
    if missing_sources:
        raise ValueError(f"source counties are missing GDP or geometry: {missing_sources}")

    output_rows: list[dict[str, object]] = []
    for material_class in SOURCE_MATERIAL_CLASSES:
        observed = source_prices.filter(pl.col("material_class") == material_class).sort(
            ["source_county_fips", "zip_code"]
        )
        output_rows.extend(
            _project_material(
                targets,
                observed,
                county_gdp,
                county_points,
                ratio_min,
                ratio_max,
            )
        )

    projected = pl.DataFrame(output_rows)
    metal = projected.filter(pl.col("material_class") == "metal_corrugated_panel")
    tile_expressions = []
    for column in PRICE_COLUMNS:
        tile_expressions.append((pl.col(column) * tile_multiplier).alias(column))
    tile = metal.with_columns(
        pl.lit("tile_proxy").alias("material_class"),
        *tile_expressions,
        pl.lit(None, dtype=pl.Int64).alias("source_product_count"),
        pl.lit(None, dtype=pl.Int64).alias("source_store_count"),
        pl.lit(None, dtype=pl.Float64).alias("median_bulk_discount_pct"),
        pl.lit("nearest_metal_gdp_ratio_then_tile_multiplier").alias("estimate_method"),
        pl.lit(True).alias("is_derived"),
        pl.lit(tile_multiplier).alias("tile_multiplier"),
    )
    result = pl.concat([projected, tile], how="vertical_relaxed").sort(
        ["county_fips", "material_class"]
    )
    if result.select(pl.struct("county_fips", "material_class").is_duplicated().any()).item():
        raise ValueError("expanded prices contain duplicate county/material rows")
    if result.filter(
        pl.col("median_price_per_square").is_null()
        | ~pl.col("median_price_per_square").is_finite()
    ).height:
        raise ValueError("expanded prices contain null or nonfinite median prices")
    return result


def build_expanded_material_prices(
    price_path: Path = DEFAULT_PRICE_PATH,
    gdp_path: Path = DEFAULT_GDP_PATH,
    geometry_path: Path = DEFAULT_GEOMETRY_PATH,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    *,
    gdp_year: int = 2024,
    ratio_min: float = 0.6,
    ratio_max: float = 1.6,
    tile_multiplier: float = 1.2,
) -> dict[str, object]:
    """Build the expanded CSV and its adjacent provenance manifest."""
    result = expand_material_prices(
        load_source_prices(price_path),
        load_county_gdp(gdp_path, gdp_year),
        load_county_points(geometry_path),
        ratio_min=ratio_min,
        ratio_max=ratio_max,
        tile_multiplier=tile_multiplier,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_csv(result, output_path)
    manifest_path = output_path.with_suffix(".manifest.json")
    manifest: dict[str, object] = {
        "generator_version": GENERATOR_VERSION,
        "assumption_version": ASSUMPTION_VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "source_prices": _file_record(price_path),
            "county_gdp": _file_record(gdp_path),
            "county_geometry": _shapefile_record(geometry_path),
        },
        "gdp_year": gdp_year,
        "source_zip_to_county": SOURCE_ZIP_TO_COUNTY,
        "ratio_policy": {"minimum": ratio_min, "maximum": ratio_max},
        "tile_policy": {"material_class": "tile_proxy", "multiplier": tile_multiplier},
        "row_count": result.height,
        "county_count": result["county_fips"].n_unique(),
        "material_classes": sorted(result["material_class"].unique().to_list()),
        "capped_row_count": result.filter(pl.col("ratio_was_capped")).height,
        "raw_ratio_minimum": result["raw_gdp_ratio"].min(),
        "raw_ratio_maximum": result["raw_gdp_ratio"].max(),
        "output": _file_record(output_path),
    }
    _atomic_write_json(manifest, manifest_path)
    return manifest


def _project_material(
    targets: pl.DataFrame,
    observed: pl.DataFrame,
    county_gdp: pl.DataFrame,
    county_points: pl.DataFrame,
    ratio_min: float,
    ratio_max: float,
) -> list[dict[str, object]]:
    source = observed.join(
        county_gdp.select(
            pl.col("county_fips").alias("source_county_fips"),
            pl.col("gdp_per_capita").alias("source_gdp_per_capita"),
        ),
        on="source_county_fips",
        how="left",
        validate="m:1",
    ).join(
        county_points.select(
            pl.col("county_fips").alias("source_county_fips"),
            pl.col("latitude").alias("source_latitude"),
            pl.col("longitude").alias("source_longitude"),
        ),
        on="source_county_fips",
        how="left",
        validate="m:1",
    )
    target_xyz = _coordinates_to_unit_sphere(targets["latitude"], targets["longitude"])
    source_xyz = _coordinates_to_unit_sphere(
        source["source_latitude"], source["source_longitude"]
    )
    dot_products = np.sum(
        target_xyz[:, np.newaxis, :] * source_xyz[np.newaxis, :, :], axis=2
    )
    angular_distances = np.arccos(np.clip(dot_products, -1.0, 1.0))
    nearest_indices = np.argmin(angular_distances, axis=1)

    rows: list[dict[str, object]] = []
    target_rows = targets.iter_rows(named=True)
    source_rows = source.iter_rows(named=True)
    source_records = list(source_rows)
    for target_index, target in enumerate(target_rows):
        source_index = int(nearest_indices[target_index])
        anchor = source_records[source_index]
        raw_ratio = float(target["gdp_per_capita"]) / float(
            anchor["source_gdp_per_capita"]
        )
        applied_ratio = float(np.clip(raw_ratio, ratio_min, ratio_max))
        row: dict[str, object] = {
            "county_fips": target["county_fips"],
            "county_name": target["county_name"],
            "state_abbr": target["state_abbr"],
            "material_class": anchor["material_class"],
            "source_material_class": anchor["material_class"],
            "source_zip_code": anchor["zip_code"],
            "source_county_fips": anchor["source_county_fips"],
            "source_distance_km": float(
                angular_distances[target_index, source_index] * EARTH_RADIUS_KM
            ),
            "gdp_year": target["year"],
            "target_gdp_per_capita": target["gdp_per_capita"],
            "source_gdp_per_capita": anchor["source_gdp_per_capita"],
            "raw_gdp_ratio": raw_ratio,
            "applied_gdp_ratio": applied_ratio,
            "ratio_was_capped": raw_ratio < ratio_min or raw_ratio > ratio_max,
            "ratio_minimum": ratio_min,
            "ratio_maximum": ratio_max,
            "estimate_method": "nearest_observed_county_gdp_per_capita_ratio",
            "is_derived": False,
            "tile_multiplier": None,
            "scrape_date": anchor["scrape_date"],
            "retailer": anchor["retailer"],
            "source_product_count": anchor["product_count"],
            "source_store_count": anchor["store_count"],
            "median_bulk_discount_pct": anchor["median_bulk_discount_pct"],
            "assumption_version": ASSUMPTION_VERSION,
        }
        for column in PRICE_COLUMNS:
            value = anchor[column]
            row[column] = None if value is None else float(value) * applied_ratio
        rows.append(row)
    return rows


def _coordinates_to_unit_sphere(
    latitude: pl.Series, longitude: pl.Series
) -> np.ndarray:
    latitude_values = np.asarray(latitude.to_list(), dtype=np.float64)
    longitude_values = np.asarray(longitude.to_list(), dtype=np.float64)
    if not np.isfinite(latitude_values).all() or not np.isfinite(longitude_values).all():
        raise ValueError("county coordinates must be finite")
    if (np.abs(latitude_values) > 90).any() or (np.abs(longitude_values) > 180).any():
        raise ValueError("county coordinates are outside WGS84 bounds")
    latitude_radians = np.radians(latitude_values)
    longitude_radians = np.radians(longitude_values)
    cosine_latitude = np.cos(latitude_radians)
    return np.column_stack(
        (
            cosine_latitude * np.cos(longitude_radians),
            cosine_latitude * np.sin(longitude_radians),
            np.sin(latitude_radians),
        )
    )


def _require_columns(frame: pl.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{label} is missing columns: {missing}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_record(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _shapefile_record(path: Path) -> dict[str, object]:
    component_paths = sorted(
        candidate
        for candidate in path.parent.glob(path.stem + ".*")
        if candidate.suffix.lower() in {".cpg", ".dbf", ".prj", ".shp", ".shx"}
    )
    required_suffixes = {".dbf", ".shp", ".shx"}
    available_suffixes = {candidate.suffix.lower() for candidate in component_paths}
    missing_suffixes = sorted(required_suffixes - available_suffixes)
    if missing_suffixes:
        raise ValueError(f"county shapefile is missing components: {missing_suffixes}")
    return {
        "path": str(path),
        "components": [_file_record(candidate) for candidate in component_paths],
    }


def _atomic_write_csv(frame: pl.DataFrame, path: Path) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    frame.write_csv(temporary_path)
    temporary_path.replace(path)


def _atomic_write_json(payload: dict[str, object], path: Path) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary_path.replace(path)


def _parse_args(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-prices", type=Path, default=DEFAULT_PRICE_PATH)
    parser.add_argument("--county-gdp", type=Path, default=DEFAULT_GDP_PATH)
    parser.add_argument("--county-geometry", type=Path, default=DEFAULT_GEOMETRY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--gdp-year", type=int, default=2024)
    parser.add_argument("--ratio-min", type=float, default=0.6)
    parser.add_argument("--ratio-max", type=float, default=1.6)
    parser.add_argument("--tile-multiplier", type=float, default=1.2)
    return parser.parse_args(arguments)


def main() -> None:
    args = _parse_args()
    manifest = build_expanded_material_prices(
        args.source_prices,
        args.county_gdp,
        args.county_geometry,
        args.output,
        gdp_year=args.gdp_year,
        ratio_min=args.ratio_min,
        ratio_max=args.ratio_max,
        tile_multiplier=args.tile_multiplier,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()