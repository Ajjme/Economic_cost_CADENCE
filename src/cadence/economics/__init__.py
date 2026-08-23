"""Public API for CADENCE roof economics calculations."""

from cadence.economics.contracts import (
    AssetEconomicsInput,
    CostStream,
    EconomicsRunConfig,
    GeographicEconomicsRunConfig,
    InstalledCostOverride,
    RepresentativeRoofScenario,
)
from cadence.economics.costs import (
    build_annual_roof_option_costs,
    build_geographic_installed_costs,
)
from cadence.economics.geography import build_zcta_dimension
from cadence.economics.geographic_pipeline import (
    run_geographic_economics_from_references,
    run_geographic_economics_pipeline,
)
from cadence.economics.geographic_sources import (
    build_geographic_annual_source_costs,
    build_geographic_removal_costs_from_references,
)
from cadence.economics.pipeline import run_economics_pipeline

__all__ = [
    "AssetEconomicsInput",
    "build_annual_roof_option_costs",
    "build_geographic_installed_costs",
    "build_geographic_annual_source_costs",
    "build_geographic_removal_costs_from_references",
    "build_zcta_dimension",
    "CostStream",
    "EconomicsRunConfig",
    "GeographicEconomicsRunConfig",
    "InstalledCostOverride",
    "RepresentativeRoofScenario",
    "run_geographic_economics_from_references",
    "run_geographic_economics_pipeline",
    "run_economics_pipeline",
]