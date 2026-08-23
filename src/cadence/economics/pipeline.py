"""Standalone orchestration for annual roof economics reference calculations."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import polars as pl

from cadence.economics.contracts import EconomicsRunConfig
from cadence.economics.costs import (
    build_annual_roof_option_costs,
    build_replacement_value_sanity_checks,
)
from cadence.economics.externalities import build_annual_external_costs
from cadence.economics.labor import build_annual_labor_costs
from cadence.economics.materials import (
    build_material_growth_lookup,
    build_material_mass_lookup,
    build_material_price_lookup,
)

ECONOMICS_SCHEMA_VERSION = "v0.1.0"


def run_economics_pipeline(
    assets: pl.DataFrame,
    config: EconomicsRunConfig,
    repository_root: Path,
    output_root: Path,
    annual_hazard_economics: Optional[pl.DataFrame] = None,
) -> Dict[str, object]:
    """Calculate and publish annual option costs without a Prefect runtime."""
    paths = _source_paths(repository_root)
    source_checksums = {
        name: _sha256(path) for name, path in paths.items()
    }
    run_id = _run_id(assets, config, annual_hazard_economics, source_checksums)
    run_root = output_root / f"schema_version={ECONOMICS_SCHEMA_VERSION}" / f"run_id={run_id}"
    manifest_path = run_root / "run_metadata.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    price_lookup = build_material_price_lookup(
        assets,
        paths["material_price_root"],
        paths["mapping"],
        paths["class_map"],
    )
    material_growth = build_material_growth_lookup(
        paths["material_escalation"], paths["mapping"], paths["class_map"]
    )
    material_mass = build_material_mass_lookup(
        paths["material_mass"], paths["mapping"], paths["class_map"]
    )
    labor = build_annual_labor_costs(
        assets,
        config,
        paths["labor_productivity"],
        paths["labor_projection"],
        paths["labor_base"],
        paths["mapping"],
        paths["class_map"],
    )
    annual_growth = labor.select(
        "asset_id", "year", "official_material_id", "labor_growth_factor"
    ).join(
        material_growth.select(
            "official_material_id", "year", "material_growth_factor"
        ),
        on=["official_material_id", "year"],
        how="left",
        validate="m:1",
    )
    source_costs = (
        price_lookup.select(
            "asset_id",
            "official_material_id",
            "source_material_2026_usd_per_sqft",
            "material_price_status",
            "missing_member_ids",
        )
        .join(
            material_growth.select(
                "official_material_id", "year", "material_growth_factor"
            ),
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
        .join(
            labor.select(
                "asset_id",
                "year",
                "official_material_id",
                "source_labor_usd_per_sqft",
            ),
            on=["asset_id", "year", "official_material_id"],
            how="left",
            validate="1:1",
        )
    )
    loss_of_use = None
    annual_damage = None
    if annual_hazard_economics is not None:
        if "expected_loss_of_use_days" in annual_hazard_economics.columns:
            loss_of_use = annual_hazard_economics.select(
                "asset_id",
                "year",
                "official_material_id",
                "expected_loss_of_use_days",
            )
        if "expected_damage_ratio" in annual_hazard_economics.columns:
            annual_damage = annual_hazard_economics.select(
                "asset_id", "year", "official_material_id", "expected_damage_ratio"
            )
    external = build_annual_external_costs(
        assets,
        material_mass,
        config,
        paths["disposal"],
        paths["carbon"],
        paths["scghg"],
        paths["mapping"],
        paths["class_map"],
        paths["housing"],
        loss_of_use,
    )
    annual_costs = build_annual_roof_option_costs(
        assets,
        annual_growth,
        config,
        source_costs=source_costs,
        annual_damage=annual_damage,
        external_costs=external,
    ).join(
        source_costs.select(
            "asset_id",
            "year",
            "official_material_id",
            "material_price_status",
            "missing_member_ids",
        ),
        on=["asset_id", "year", "official_material_id"],
        how="left",
        validate="1:1",
    )
    sanity_checks = build_replacement_value_sanity_checks(
        annual_costs, config.replacement_value_tolerance_percent
    )

    annual_root = run_root / "annual_roof_option_costs"
    annual_root.mkdir(parents=True, exist_ok=False)
    for key, partition in annual_costs.partition_by("year", as_dict=True).items():
        year = key[0] if isinstance(key, tuple) else key
        year_root = annual_root / f"year={year}"
        year_root.mkdir()
        partition.write_parquet(year_root / "part-00000.parquet")
    sanity_checks.write_parquet(run_root / "replacement_value_sanity_checks.parquet")

    manifest = {
        "run_id": run_id,
        "schema_version": ECONOMICS_SCHEMA_VERSION,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "run_config": config.model_dump(mode="json"),
        "source_checksums": source_checksums,
        "asset_count": assets.height,
        "annual_option_row_count": annual_costs.height,
        "sanity_check_row_count": sanity_checks.height,
        "dollar_basis": "real_2026_usd",
        "tear_off_labor_excluded": True,
        "cache_hit": False,
        "annual_costs_path": str(annual_root),
        "sanity_checks_path": str(
            run_root / "replacement_value_sanity_checks.parquet"
        ),
    }
    _write_json_atomic(manifest_path, manifest)
    return manifest


def _source_paths(repository_root: Path) -> Dict[str, Path]:
    mapping_root = repository_root / "Data_Catalogs" / "Mapping"
    return {
        "material_price_root": repository_root / "Data" / "Materials" / "Start_Year_2026",
        "material_escalation": repository_root / "Data" / "Materials" / "Escalation" / "material_projection_rates.csv",
        "material_mass": repository_root / "Data" / "Material_Mass" / "roofing_lbs.csv",
        "labor_productivity": repository_root / "Data" / "Labor" / "Productivity" / "roof_labor_productivity_parameters.csv",
        "labor_projection": repository_root / "Data" / "Labor" / "Escalation" / "labor_wage_projections_2026_2050.parquet",
        "labor_base": repository_root / "Data" / "Labor" / "Start_Year_2026" / "labor_wages_long.parquet",
        "disposal": repository_root / "Data" / "Disposal" / "EREF_2024_Tipping_Fees_Parsed.csv",
        "carbon": repository_root / "Data" / "Carbon" / "roofing_eol_emission_factors.csv",
        "scghg": repository_root / "Data" / "Carbon" / "table_a5_1_scghg_unrounded_2020_2080.csv",
        "housing": repository_root / "Data" / "Loss_of_Use" / "temporary_housing_relocation_costs.csv",
        "mapping": mapping_root / "master_mapping_reference_draft.csv",
        "class_map": mapping_root / "official_material_class_map_v1.csv",
    }


def _run_id(
    assets: pl.DataFrame,
    config: EconomicsRunConfig,
    hazard: Optional[pl.DataFrame],
    source_checksums: Dict[str, str],
) -> str:
    digest = hashlib.sha256()
    digest.update(config.model_dump_json().encode("utf-8"))
    digest.update(
        assets.sort(assets.columns).hash_rows().to_numpy().tobytes()
    )
    if hazard is not None:
        digest.update(hazard.sort(hazard.columns).hash_rows().to_numpy().tobytes())
    digest.update(json.dumps(source_checksums, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:24]


def _sha256(path: Path) -> str:
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted(path.glob("*.csv")):
            digest.update(child.name.encode("utf-8"))
            digest.update(_sha256(child).encode("utf-8"))
        return digest.hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: Dict[str, object]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)