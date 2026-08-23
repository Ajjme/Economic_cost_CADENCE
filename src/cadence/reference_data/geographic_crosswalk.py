"""Build the versioned national ZCTA economics geography dimension."""

import argparse
import hashlib
import json
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional, Sequence

import duckdb
import polars as pl

from cadence.economics.geography import build_zcta_dimension

STATE_FIPS_TO_CODE = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA",
    "08": "CO", "09": "CT", "10": "DE", "11": "DC", "12": "FL",
    "13": "GA", "15": "HI", "16": "ID", "17": "IL", "18": "IN",
    "19": "IA", "20": "KS", "21": "KY", "22": "LA", "23": "ME",
    "24": "MD", "25": "MA", "26": "MI", "27": "MN", "28": "MS",
    "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND",
    "39": "OH", "40": "OK", "41": "OR", "42": "PA", "44": "RI",
    "45": "SC", "46": "SD", "47": "TN", "48": "TX", "49": "UT",
    "50": "VT", "51": "VA", "53": "WA", "54": "WV", "55": "WI",
    "56": "WY", "60": "AS", "66": "GU", "69": "MP", "72": "PR",
    "78": "VI",
}
ZCTA_REFERENCE_VERSION = "2020-nhgis-bls2019-v1.0.0"


def build_national_zcta_dimension(
    zcta_crosswalk_path: Path,
    cbsa_crosswalk_path: Path,
    labor_coverage_path: Path,
    zcta_geometry_path: Optional[Path] = None,
    labor_geometry_path: Optional[Path] = None,
) -> pl.DataFrame:
    """Resolve dominant county, CBSA, and BLS labor area for each 2020 ZCTA."""
    with _crosswalk_csv(cbsa_crosswalk_path, "nhgis_blk2010_cbsa2020.csv") as cbsa_csv:
        relationships = _aggregate_relationships(zcta_crosswalk_path, cbsa_csv)

    dimension = relationships.with_columns(
        pl.col("county_fips")
        .str.slice(0, 2)
        .replace_strict(STATE_FIPS_TO_CODE, default=None)
        .alias("state_code"),
        pl.when(pl.col("cbsa_code") == "99999")
        .then(pl.lit(None, dtype=pl.String))
        .otherwise(pl.concat_str(pl.lit("00"), pl.col("cbsa_code")))
        .alias("direct_labor_market_id"),
    )
    if dimension.filter(pl.col("state_code").is_null()).height:
        raise ValueError("dominant county contains an unsupported state FIPS code")

    coverage = pl.read_csv(
        labor_coverage_path,
        schema_overrides={"AREA": pl.String},
    ).filter(
        pl.col("COVERAGE_STATUS") != "geometry_only"
    ).select(
        pl.col("AREA").alias("coverage_labor_market_id"),
        "GEOGRAPHY_TYPE",
    )
    dimension = dimension.join(
        coverage,
        left_on="direct_labor_market_id",
        right_on="coverage_labor_market_id",
        how="left",
        validate="m:1",
    ).with_columns(
        pl.when(pl.col("GEOGRAPHY_TYPE").is_not_null())
        .then(pl.col("direct_labor_market_id"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("validated_direct_labor_market_id")
    )
    unresolved = dimension.filter(pl.col("GEOGRAPHY_TYPE").is_null()).select(
        "zcta5", "state_code", "cbsa_code"
    )
    spatial_labor = pl.DataFrame(
        schema={
            "zcta5": pl.String,
            "spatial_labor_market_id": pl.String,
            "spatial_labor_method": pl.String,
        }
    )
    if unresolved.height:
        if zcta_geometry_path is None or labor_geometry_path is None:
            raise ValueError(
                "ZCTA and labor geometry paths are required for unresolved labor areas"
            )
        spatial_labor = _resolve_labor_by_point(
            unresolved,
            zcta_geometry_path,
            labor_geometry_path,
            labor_coverage_path,
        )

    return build_zcta_dimension(
        dimension.join(spatial_labor, on="zcta5", how="left", validate="1:1")
        .with_columns(
            pl.coalesce(
                "validated_direct_labor_market_id", "spatial_labor_market_id"
            ).alias("labor_market_id"),
            pl.when(pl.col("validated_direct_labor_market_id").is_not_null())
            .then(pl.lit("nhgis_weighted_direct_cbsa"))
            .otherwise(pl.col("spatial_labor_method"))
            .alias("crosswalk_method"),
            pl.lit("2020").alias("source_vintage"),
        )
        .select(
            "zcta5",
            "state_code",
            "county_fips",
            "cbsa_code",
            "labor_market_id",
            "crosswalk_method",
            "source_vintage",
            "county_weight_share",
            "cbsa_weight_share",
        )
    ).join(
        dimension.select(
            "zcta5", "county_weight_share", "cbsa_weight_share"
        ),
        on="zcta5",
        how="left",
        validate="1:1",
    )


def publish_national_zcta_reference(
    zcta_crosswalk_path: Path,
    cbsa_crosswalk_path: Path,
    labor_coverage_path: Path,
    zcta_geometry_path: Path,
    labor_geometry_path: Path,
    output_root: Path,
) -> Dict[str, object]:
    """Build and publish the national ZCTA dimension with source checksums."""
    dimension = build_national_zcta_dimension(
        zcta_crosswalk_path,
        cbsa_crosswalk_path,
        labor_coverage_path,
        zcta_geometry_path,
        labor_geometry_path,
    )
    version_root = output_root / f"version={ZCTA_REFERENCE_VERSION}"
    version_root.mkdir(parents=True, exist_ok=True)
    dimension_path = version_root / "zcta_dimension.parquet"
    manifest_path = version_root / "manifest.json"
    dimension.write_parquet(dimension_path)
    sources = {
        "nhgis_zcta_crosswalk": zcta_crosswalk_path,
        "nhgis_cbsa_crosswalk": cbsa_crosswalk_path,
        "census_zcta_geometry": zcta_geometry_path,
        "bls_labor_geometry": labor_geometry_path,
        "labor_coverage": labor_coverage_path,
    }
    import pyogrio

    census_zctas = set(
        pyogrio.read_dataframe(
            f"zip://{zcta_geometry_path}",
            columns=["GEOID20"],
            read_geometry=False,
        )["GEOID20"]
    )
    published_zctas = set(dimension["zcta5"].to_list())
    assignment_counts = {
        row["crosswalk_method"]: row["len"]
        for row in dimension.group_by("crosswalk_method")
        .len()
        .sort("crosswalk_method")
        .iter_rows(named=True)
    }
    manifest = {
        "reference_version": ZCTA_REFERENCE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "zcta_count": dimension.height,
        "source_checksums": {
            name: _sha256(path) for name, path in sources.items()
        },
        "source_paths": {name: str(path) for name, path in sources.items()},
        "dimension_path": str(dimension_path),
        "county_assignment": "maximum summed NHGIS block interpolation weight",
        "cbsa_assignment": "maximum summed product of ZCTA and CBSA block weights",
        "labor_assignment": "direct CBSA when covered; otherwise ZCTA representative point in BLS 2019 area with audited overlap and gap fallbacks",
        "labor_assignment_counts": assignment_counts,
        "low_dominance_count": dimension.filter(
            (pl.col("county_weight_share") < 0.5)
            | (pl.col("cbsa_weight_share") < 0.5)
        ).height,
        "census_zcta_geometry_count": len(census_zctas),
        "excluded_census_zctas": sorted(census_zctas - published_zctas),
        "exclusion_note": "Census territorial ZCTA polygons outside the NHGIS U.S./Puerto Rico block crosswalk coverage",
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return manifest


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Build and publish the national ZCTA economics dimension."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zcta-crosswalk", type=Path, required=True)
    parser.add_argument("--cbsa-crosswalk", type=Path, required=True)
    parser.add_argument("--labor-coverage", type=Path, required=True)
    parser.add_argument("--zcta-geometry", type=Path, required=True)
    parser.add_argument("--labor-geometry", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args(argv)
    manifest = publish_national_zcta_reference(
        arguments.zcta_crosswalk,
        arguments.cbsa_crosswalk,
        arguments.labor_coverage,
        arguments.zcta_geometry,
        arguments.labor_geometry,
        arguments.output_root,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


def _aggregate_relationships(
    zcta_crosswalk_path: Path,
    cbsa_crosswalk_path: Path,
) -> pl.DataFrame:
    connection = duckdb.connect()
    connection.read_csv(str(zcta_crosswalk_path), all_varchar=True).create_view(
        "zcta_crosswalk"
    )
    connection.read_csv(str(cbsa_crosswalk_path), all_varchar=True).create_view(
        "cbsa_crosswalk"
    )
    query = """
        WITH county_scores AS (
            SELECT
                zcta2020ge AS zcta5,
                substr(blk2010ge, 1, 5) AS county_fips,
                sum(try_cast(weight AS DOUBLE)) AS score
            FROM zcta_crosswalk
            WHERE zcta2020ge <> '99999'
            GROUP BY 1, 2
        ),
        county_ranked AS (
            SELECT *,
                score / sum(score) OVER (PARTITION BY zcta5) AS county_weight_share,
                row_number() OVER (
                    PARTITION BY zcta5 ORDER BY score DESC, county_fips
                ) AS rank
            FROM county_scores
        ),
        cbsa_scores AS (
            SELECT
                z.zcta2020ge AS zcta5,
                c.cbsa2020ge AS cbsa_code,
                sum(
                    try_cast(z.weight AS DOUBLE) * try_cast(c.weight AS DOUBLE)
                ) AS score
            FROM zcta_crosswalk z
            JOIN cbsa_crosswalk c USING (blk2010ge)
            WHERE z.zcta2020ge <> '99999'
            GROUP BY 1, 2
        ),
        cbsa_ranked AS (
            SELECT *,
                score / sum(score) OVER (PARTITION BY zcta5) AS cbsa_weight_share,
                row_number() OVER (
                    PARTITION BY zcta5 ORDER BY score DESC, cbsa_code
                ) AS rank
            FROM cbsa_scores
        )
        SELECT
            county.zcta5,
            county.county_fips,
            county.county_weight_share,
            cbsa.cbsa_code,
            cbsa.cbsa_weight_share
        FROM county_ranked county
        JOIN cbsa_ranked cbsa USING (zcta5)
        WHERE county.rank = 1 AND cbsa.rank = 1
        ORDER BY county.zcta5
    """
    result = connection.sql(query).pl()
    connection.close()
    return result


def _resolve_labor_by_point(
    unresolved: pl.DataFrame,
    zcta_geometry_path: Path,
    labor_geometry_path: Path,
    labor_coverage_path: Path,
) -> pl.DataFrame:
    import geopandas as gpd
    import pandas as pd

    zctas = gpd.read_file(
        f"zip://{zcta_geometry_path}",
        columns=["GEOID20", "geometry"],
    )
    requests = unresolved.to_pandas().rename(columns={"zcta5": "GEOID20"})
    zctas = zctas[zctas["GEOID20"].isin(requests["GEOID20"])].merge(
        requests,
        on="GEOID20",
        how="inner",
        validate="one_to_one",
    )
    coverage = pd.read_csv(
        labor_coverage_path,
        dtype={"AREA": str, "PRIM_STATE": str},
    )
    coverage = coverage[coverage["COVERAGE_STATUS"] != "geometry_only"][
        ["AREA", "PRIM_STATE", "GEOGRAPHY_TYPE"]
    ]
    labor = gpd.read_parquet(labor_geometry_path)[["AREA", "geometry"]].merge(
        coverage,
        on="AREA",
        how="inner",
        validate="one_to_one",
    )
    zctas = zctas.to_crs(labor.crs)
    zctas.geometry = zctas.geometry.representative_point()
    joined = gpd.sjoin(zctas, labor, how="left", predicate="within")
    resolved = joined[joined["AREA"].notna()].copy()
    resolved["preferred_geography_type"] = resolved["cbsa_code"].map(
        lambda value: "bos" if value == "99999" else "msa"
    )
    resolved["state_priority"] = (
        resolved["state_code"] == resolved["PRIM_STATE"]
    ).astype(int)
    resolved["type_priority"] = (
        resolved["GEOGRAPHY_TYPE"] == resolved["preferred_geography_type"]
    ).astype(int)
    match_counts = resolved.groupby("GEOID20")["AREA"].transform("count")
    resolved["spatial_labor_method"] = match_counts.map(
        lambda count: (
            "nhgis_weighted_bls_point_preferred_overlap"
            if count > 1
            else "nhgis_weighted_bls_point_within"
        )
    )
    resolved = (
        resolved.sort_values(
            ["GEOID20", "state_priority", "type_priority", "AREA"],
            ascending=[True, False, False, True],
        )
        .drop_duplicates("GEOID20", keep="first")
        .copy()
    )

    missing_ids = sorted(set(requests["GEOID20"]) - set(resolved["GEOID20"]))
    nearest_rows = []
    for zcta_id in missing_ids:
        point = zctas[zctas["GEOID20"] == zcta_id]
        if point.empty:
            raise ValueError(f"ZCTA geometry is missing for {zcta_id}")
        state_code = point.iloc[0]["state_code"]
        candidates = labor[labor["PRIM_STATE"] == state_code]
        preferred_type = "bos" if point.iloc[0]["cbsa_code"] == "99999" else "msa"
        if candidates.empty:
            wage_only = coverage[
                (coverage["PRIM_STATE"] == state_code)
                & (coverage["GEOGRAPHY_TYPE"] == preferred_type)
            ]
            if len(wage_only) == 1:
                nearest_rows.append(
                    {
                        "GEOID20": zcta_id,
                        "AREA": wage_only.iloc[0]["AREA"],
                        "spatial_labor_method": "nhgis_weighted_bls_single_state_area",
                    }
                )
                continue
            state_bos = coverage[
                (coverage["PRIM_STATE"] == state_code)
                & (coverage["GEOGRAPHY_TYPE"] == "bos")
            ]
            if preferred_type == "msa" and len(state_bos) == 1:
                nearest_rows.append(
                    {
                        "GEOID20": zcta_id,
                        "AREA": state_bos.iloc[0]["AREA"],
                        "spatial_labor_method": "nhgis_uncovered_cbsa_to_single_state_bos",
                    }
                )
                continue
            raise ValueError(
                f"BLS labor geography is unresolved for {zcta_id} in {state_code}"
            )
        preferred_candidates = candidates[
            candidates["GEOGRAPHY_TYPE"] == preferred_type
        ]
        if not preferred_candidates.empty:
            candidates = preferred_candidates
        local_crs = point.estimate_utm_crs()
        local_point = point.to_crs(local_crs).geometry.iloc[0]
        distances = candidates.to_crs(local_crs).geometry.distance(local_point)
        nearest_rows.append(
            {
                "GEOID20": zcta_id,
                "AREA": candidates.loc[distances.idxmin(), "AREA"],
                "spatial_labor_method": "nhgis_weighted_bls_nearest_same_state",
            }
        )
    nearest = pd.DataFrame(nearest_rows)
    assignments = pd.concat(
        [resolved[["GEOID20", "AREA", "spatial_labor_method"]], nearest],
        ignore_index=True,
    )
    if len(assignments) != len(requests) or assignments["GEOID20"].duplicated().any():
        raise ValueError("BLS labor geography could not uniquely resolve every ZCTA")
    return pl.from_pandas(
        assignments.rename(
            columns={
                "GEOID20": "zcta5",
                "AREA": "spatial_labor_market_id",
            }
        )
    )


@contextmanager
def _crosswalk_csv(path: Path, member_name: str) -> Iterator[Path]:
    if path.suffix.casefold() != ".zip":
        yield path
        return
    with tempfile.TemporaryDirectory() as temporary_directory:
        with zipfile.ZipFile(path) as archive:
            archive.extract(member_name, temporary_directory)
        yield Path(temporary_directory) / member_name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    main()