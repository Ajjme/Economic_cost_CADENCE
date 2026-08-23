"""Repository-backed annual source components for geographic economics."""

from pathlib import Path

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.externalities import build_geographic_annual_removal_costs
from cadence.economics.labor import build_geographic_annual_labor_costs
from cadence.economics.materials import (
    build_geographic_material_costs,
    build_material_mass_lookup,
)


def build_geographic_annual_source_costs(
    zcta_dimension: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    repository_root: Path,
) -> pl.DataFrame:
    """Build the complete ZCTA/year/material/scenario installed-cost basis."""
    mapping_root = repository_root / "Data_Catalogs" / "Mapping"
    mapping_path = mapping_root / "master_mapping_reference_draft.csv"
    class_map_path = mapping_root / "official_material_class_map_v1.csv"
    material = build_geographic_material_costs(
        zcta_dimension,
        repository_root
        / "Data"
        / "Materials"
        / "Start_Year_2026"
        / "home_depot_material_price_expanded.csv",
        repository_root
        / "Data"
        / "Materials"
        / "Escalation"
        / "material_projection_rates.csv",
        mapping_path,
        class_map_path,
    )
    labor = build_geographic_annual_labor_costs(
        zcta_dimension,
        config,
        repository_root
        / "Data"
        / "Labor"
        / "Productivity"
        / "roof_labor_productivity_parameters.csv",
        repository_root
        / "Data"
        / "Labor"
        / "Escalation"
        / "labor_wage_projections_2026_2050.parquet",
        repository_root
        / "Data"
        / "Labor"
        / "Start_Year_2026"
        / "labor_wages_long.parquet",
        mapping_path,
        class_map_path,
    )
    keys = ["zcta5", "year", "official_material_id"]
    return labor.join(
        material,
        on=keys,
        how="left",
        validate="m:1",
        coalesce=True,
    ).sort(*keys, "roof_scenario_id")


def build_geographic_removal_costs_from_references(
    zcta_dimension: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    repository_root: Path,
) -> pl.DataFrame:
    """Build annual disposal and carbon costs from repository references."""
    mapping_root = repository_root / "Data_Catalogs" / "Mapping"
    mapping_path = mapping_root / "master_mapping_reference_draft.csv"
    class_map_path = mapping_root / "official_material_class_map_v1.csv"
    mass = build_material_mass_lookup(
        repository_root / "Data" / "Material_Mass" / "roofing_lbs.csv",
        mapping_path,
        class_map_path,
    )
    return build_geographic_annual_removal_costs(
        zcta_dimension,
        mass,
        config,
        repository_root / "Data" / "Disposal" / "EREF_2024_Tipping_Fees_Parsed.csv",
        repository_root / "Data" / "Carbon" / "roofing_eol_emission_factors.csv",
        repository_root
        / "Data"
        / "Carbon"
        / "table_a5_1_scghg_unrounded_2020_2080.csv",
        mapping_path,
        class_map_path,
    )