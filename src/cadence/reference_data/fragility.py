"""Extract approved HAZUS proxies and average unresolved variants by terrain."""

from pathlib import Path

import duckdb
import polars as pl

FRAGILITY_MAPPING_VERSION = "v1.0.0"
MATERIAL_PROXY_IDS = {
    "OFFICIAL_ASPHALT": "WSF1",
    "OFFICIAL_METAL": "SERBL",
    "OFFICIAL_TILE": "MSF1",
}
TERRAIN_LABELS = {
    1: "Open",
    2: "Light Suburban",
    3: "Suburban",
    4: "Light Trees",
    5: "Trees",
}
TERRAIN_IDS_BY_LABEL = {
    label.casefold(): terrain_id for terrain_id, label in TERRAIN_LABELS.items()
}


def load_terrain_averaged_curve(
    fragility_root: Path,
    climate_zone: str,
    roof_age: int,
    official_material_id: str,
    terrain_id: int,
) -> pl.DataFrame:
    """Load one curve averaged across unresolved variants at the requested grain."""
    if official_material_id not in MATERIAL_PROXY_IDS:
        raise ValueError(f"unsupported official material: {official_material_id}")
    if terrain_id not in TERRAIN_LABELS:
        raise ValueError("terrain_id must be an integer from 1 to 5")
    if roof_age < 1 or roof_age > 30:
        raise ValueError("roof_age must be between 1 and 30")

    partition = fragility_root / climate_zone / f"age_{roof_age:02d}"
    curves_path = _sql_path(partition / "curves_hu.parquet")
    points_path = _sql_path(partition / "curve_points_hu.parquet")
    attributes_path = _sql_path(partition / "curve_attributes_hu.parquet")
    proxy_id = MATERIAL_PROXY_IDS[official_material_id]
    query = f"""
        WITH terrain AS (
            SELECT curve_id
            FROM read_parquet('{attributes_path}')
            WHERE key = 'terrain_id' AND CAST(value AS INTEGER) = {terrain_id}
        )
        SELECT
            p.x AS wind_speed_mph,
            AVG(p.y) AS building_loss_ratio,
            COUNT(DISTINCT c.curve_id) AS source_curve_count
        FROM read_parquet('{curves_path}') c
        JOIN terrain t ON c.curve_id = t.curve_id
        JOIN read_parquet('{points_path}') p ON c.curve_id = p.curve_id
        WHERE c.damage_type = 'building_loss'
          AND c.building_type = '{proxy_id}'
        GROUP BY p.x
        ORDER BY p.x
    """
    curve = duckdb.sql(query).pl()
    if curve.is_empty():
        raise ValueError(
            f"missing curve for {climate_zone}, age {roof_age}, "
            f"{official_material_id}, terrain {terrain_id}"
        )
    if curve.filter(
        ~pl.col("wind_speed_mph").is_finite()
        | ~pl.col("building_loss_ratio").is_finite()
    ).height:
        raise ValueError("fragility curve contains non-finite values")
    if curve.filter(~pl.col("building_loss_ratio").is_between(0.0, 1.0)).height:
        raise ValueError("fragility curve contains a damage ratio outside [0, 1]")
    if curve["wind_speed_mph"].is_duplicated().any():
        raise ValueError("fragility curve contains duplicate wind speeds")
    return curve


def _sql_path(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return str(path.resolve()).replace("'", "''")