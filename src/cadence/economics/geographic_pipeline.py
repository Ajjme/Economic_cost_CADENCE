"""Orchestration for national ZCTA roof economics reference costs."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.costs import build_geographic_installed_costs
from cadence.economics.geography import build_zcta_dimension
from cadence.economics.geographic_sources import (
    build_geographic_annual_source_costs,
    build_geographic_removal_costs_from_references,
)

GEOGRAPHIC_ECONOMICS_SCHEMA_VERSION = "v1.0.0"


def run_geographic_economics_from_references(
    zcta_crosswalk: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    repository_root: Path,
    output_root: Path,
) -> Dict[str, object]:
    """Build source components from repository data and publish national costs."""
    zcta_dimension = build_zcta_dimension(zcta_crosswalk)
    annual_source_costs = build_geographic_annual_source_costs(
        zcta_dimension,
        config,
        repository_root,
    )
    annual_removal_costs = build_geographic_removal_costs_from_references(
        zcta_dimension,
        config,
        repository_root,
    )
    return run_geographic_economics_pipeline(
        zcta_dimension,
        annual_source_costs,
        config,
        output_root,
        annual_removal_costs=annual_removal_costs,
    )


def run_geographic_economics_pipeline(
    zcta_crosswalk: pl.DataFrame,
    annual_source_costs: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    output_root: Path,
    annual_removal_costs: Optional[pl.DataFrame] = None,
) -> Dict[str, object]:
    """Validate and publish annual installed costs for every supplied ZCTA."""
    zcta_dimension = build_zcta_dimension(zcta_crosswalk)
    zcta_ids = zcta_dimension.select("zcta5")
    unknown_zctas = annual_source_costs.select("zcta5").unique().join(
        zcta_ids,
        on="zcta5",
        how="anti",
    )
    missing_zctas = zcta_ids.join(
        annual_source_costs.select("zcta5").unique(),
        on="zcta5",
        how="anti",
    )
    if unknown_zctas.height or missing_zctas.height:
        raise ValueError(
            "annual_source_costs and the ZCTA dimension must contain identical zcta5 values"
        )
    if annual_removal_costs is not None:
        removal_zctas = annual_removal_costs.select("zcta5").unique()
        if (
            removal_zctas.join(zcta_ids, on="zcta5", how="anti").height
            or zcta_ids.join(removal_zctas, on="zcta5", how="anti").height
        ):
            raise ValueError(
                "annual_removal_costs and the ZCTA dimension must contain identical zcta5 values"
            )

    annual_costs = build_geographic_installed_costs(annual_source_costs, config)
    run_id = _geographic_run_id(
        zcta_dimension,
        annual_source_costs,
        config,
        annual_removal_costs,
    )
    run_root = (
        output_root
        / f"schema_version={GEOGRAPHIC_ECONOMICS_SCHEMA_VERSION}"
        / f"run_id={run_id}"
    )
    manifest_path = run_root / "run_metadata.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    annual_root = run_root / "annual_installed_costs"
    annual_root.mkdir(parents=True, exist_ok=False)
    for key, partition in annual_costs.partition_by("year", as_dict=True).items():
        year = key[0] if isinstance(key, tuple) else key
        year_root = annual_root / f"year={year}"
        year_root.mkdir()
        partition.write_parquet(year_root / "part-00000.parquet")
    zcta_dimension_path = run_root / "zcta_dimension.parquet"
    zcta_dimension.write_parquet(zcta_dimension_path)
    removal_root = None
    if annual_removal_costs is not None:
        removal_root = run_root / "annual_removal_costs"
        removal_root.mkdir()
        for key, partition in annual_removal_costs.partition_by(
            "year", as_dict=True
        ).items():
            year = key[0] if isinstance(key, tuple) else key
            year_root = removal_root / f"year={year}"
            year_root.mkdir()
            partition.write_parquet(year_root / "part-00000.parquet")

    manifest = {
        "run_id": run_id,
        "schema_version": GEOGRAPHIC_ECONOMICS_SCHEMA_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "run_config": config.model_dump(mode="json"),
        "zcta_count": zcta_dimension.height,
        "annual_installed_cost_row_count": annual_costs.height,
        "annual_removal_cost_row_count": (
            annual_removal_costs.height if annual_removal_costs is not None else 0
        ),
        "dollar_basis": "real_2026_usd",
        "cache_hit": False,
        "annual_installed_costs_path": str(annual_root),
        "zcta_dimension_path": str(zcta_dimension_path),
        "annual_removal_costs_path": (
            str(removal_root) if removal_root is not None else None
        ),
    }
    _write_json_atomic(manifest_path, manifest)
    return manifest


def _geographic_run_id(
    zcta_dimension: pl.DataFrame,
    annual_source_costs: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    annual_removal_costs: Optional[pl.DataFrame],
) -> str:
    digest = hashlib.sha256()
    digest.update(GEOGRAPHIC_ECONOMICS_SCHEMA_VERSION.encode("utf-8"))
    digest.update(config.model_dump_json().encode("utf-8"))
    normalized_zctas = zcta_dimension.select(sorted(zcta_dimension.columns)).sort(
        "zcta5"
    )
    normalized_costs = annual_source_costs.select(
        sorted(annual_source_costs.columns)
    ).sort("zcta5", "year", "official_material_id", "roof_scenario_id")
    digest.update(normalized_zctas.hash_rows().to_numpy().tobytes())
    digest.update(normalized_costs.hash_rows().to_numpy().tobytes())
    if annual_removal_costs is not None:
        normalized_removal = annual_removal_costs.select(
            sorted(annual_removal_costs.columns)
        ).sort(
            "zcta5",
            "year",
            "removed_official_material_id",
            "roof_scenario_id",
        )
        digest.update(normalized_removal.hash_rows().to_numpy().tobytes())
    return digest.hexdigest()[:24]


def _write_json_atomic(path: Path, value: Dict[str, object]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)