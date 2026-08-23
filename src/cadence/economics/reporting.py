"""Static report figures for national geographic roof economics."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Sequence

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.labor import build_geographic_annual_labor_costs
from cadence.economics.materials import build_geographic_material_costs

MATERIAL_LABELS = {
    "OFFICIAL_ASPHALT": "Asphalt",
    "OFFICIAL_METAL": "Metal",
    "OFFICIAL_TILE": "Tile",
}
MATERIAL_COLORS = {
    "OFFICIAL_ASPHALT": "#C14953",
    "OFFICIAL_METAL": "#26747A",
    "OFFICIAL_TILE": "#D6A23D",
}
MATERIAL_ORDER = tuple(MATERIAL_LABELS)


def build_geographic_report_tables(
    zcta_dimension: pl.DataFrame,
    config: GeographicEconomicsRunConfig,
    repository_root: Path,
    map_year: int = 2026,
    map_scenario: str = "medium",
) -> Dict[str, pl.DataFrame]:
    """Build compact report tables without retaining the full detailed cube."""
    mapping_root = repository_root / "Data_Catalogs" / "Mapping"
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
        mapping_root / "master_mapping_reference_draft.csv",
        mapping_root / "official_material_class_map_v1.csv",
    ).select(
        "zcta5",
        "year",
        "official_material_id",
        "source_material_usd_per_sqft",
        "material_source_level",
        "material_price_status",
        "maximum_fallback_rank",
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
        mapping_root / "master_mapping_reference_draft.csv",
        mapping_root / "official_material_class_map_v1.csv",
    ).select(
        "zcta5",
        "year",
        "official_material_id",
        "roof_scenario_id",
        "roof_area_sqft",
        "source_labor_usd_per_sqft",
    )
    annual = labor.join(
        material,
        on=["zcta5", "year", "official_material_id"],
        how="inner",
        validate="m:1",
    ).with_columns(
        (
            pl.col("source_material_usd_per_sqft")
            + pl.col("source_labor_usd_per_sqft")
        ).alias("installed_cost_usd_per_sqft")
    )
    trends = annual.group_by(
        "year", "official_material_id", "roof_scenario_id"
    ).agg(
        pl.col("installed_cost_usd_per_sqft").quantile(0.1).alias("p10"),
        pl.col("installed_cost_usd_per_sqft").median().alias("median"),
        pl.col("installed_cost_usd_per_sqft").quantile(0.9).alias("p90"),
    ).sort("year", "official_material_id", "roof_scenario_id")
    components = annual.filter(pl.col("year") == config.start_year).group_by(
        "official_material_id", "roof_scenario_id"
    ).agg(
        pl.col("source_material_usd_per_sqft").median().alias(
            "median_material_usd_per_sqft"
        ),
        pl.col("source_labor_usd_per_sqft").median().alias(
            "median_labor_usd_per_sqft"
        ),
    ).sort("official_material_id", "roof_scenario_id")
    map_values = annual.filter(
        (pl.col("year") == map_year)
        & (pl.col("roof_scenario_id") == map_scenario)
    ).select(
        "zcta5",
        "official_material_id",
        "installed_cost_usd_per_sqft",
    ).join(
        zcta_dimension.select("zcta5", "state_code"),
        on="zcta5",
        how="left",
        validate="m:1",
    )
    quality = material.filter(pl.col("year") == config.start_year).group_by(
        "official_material_id", "material_source_level", "material_price_status"
    ).agg(pl.col("zcta5").n_unique().alias("zcta_count")).sort(
        "official_material_id", "material_source_level", "material_price_status"
    )
    return {
        "trends": trends,
        "components": components,
        "map_values": map_values,
        "quality": quality,
    }


def render_geographic_report(
    tables: Dict[str, pl.DataFrame],
    zcta_geometry_path: Path,
    output_root: Path,
    reference_manifest_path: Path,
    map_year: int = 2026,
    map_scenario: str = "medium",
) -> Dict[str, object]:
    """Render report-ready PNG/SVG figures and publish their source tables."""
    import geopandas as gpd
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch

    output_root.mkdir(parents=True, exist_ok=True)
    data_root = output_root / "data"
    data_root.mkdir(exist_ok=True)
    for name, table in tables.items():
        table.write_parquet(data_root / f"{name}.parquet")
        if name != "map_values":
            table.write_csv(data_root / f"{name}.csv")

    mpl.rcParams.update(
        {
            "font.family": ["Avenir Next", "DejaVu Sans"],
            "font.size": 11,
            "axes.titlesize": 15,
            "axes.labelsize": 11,
            "axes.edgecolor": "#40474F",
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "grid.color": "#D9DEE3",
            "grid.linewidth": 0.6,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )
    figure_paths = []

    trends = tables["trends"].filter(
        pl.col("roof_scenario_id") == map_scenario
    ).to_pandas()
    figure, axis = plt.subplots(figsize=(16, 9))
    figure.subplots_adjust(left=0.08, right=0.98, bottom=0.1, top=0.82)
    for material_id in MATERIAL_ORDER:
        values = trends[trends["official_material_id"] == material_id]
        color = MATERIAL_COLORS[material_id]
        axis.fill_between(
            values["year"], values["p10"], values["p90"], color=color, alpha=0.14
        )
        axis.plot(
            values["year"],
            values["median"],
            color=color,
            linewidth=3,
            label=MATERIAL_LABELS[material_id],
        )
    figure.suptitle(
        "Installed roof cost trajectory",
        x=0.08,
        y=0.96,
        ha="left",
        fontsize=20,
        fontweight="bold",
    )
    figure.text(
        0.08,
        0.91,
        f"National ZCTA distribution · {map_scenario.title()} representative roof · shaded p10–p90",
        color="#59636E",
        fontsize=12,
    )
    axis.set_xlabel("Year")
    axis.set_ylabel("Real 2026 USD per square foot")
    axis.legend(frameon=False, ncol=3, loc="upper left")
    axis.spines[["top", "right"]].set_visible(False)
    figure_paths.extend(_save_figure(figure, output_root / "installed_cost_trends"))
    plt.close(figure)

    components = tables["components"].to_pandas()
    scenarios = ["small", "medium", "large"]
    x_positions = np.arange(len(MATERIAL_ORDER))
    width = 0.24
    figure, axis = plt.subplots(figsize=(16, 9))
    figure.subplots_adjust(left=0.08, right=0.98, bottom=0.1, top=0.82)
    for scenario_index, scenario in enumerate(scenarios):
        subset = components[components["roof_scenario_id"] == scenario].set_index(
            "official_material_id"
        ).reindex(MATERIAL_ORDER)
        positions = x_positions + (scenario_index - 1) * width
        material_values = subset["median_material_usd_per_sqft"].to_numpy()
        labor_values = subset["median_labor_usd_per_sqft"].to_numpy()
        axis.bar(
            positions,
            material_values,
            width,
            color="#D6A23D",
            edgecolor="white",
        )
        axis.bar(
            positions,
            labor_values,
            width,
            bottom=material_values,
            color="#26747A",
            edgecolor="white",
            label=scenario.title() if scenario_index == 0 else None,
        )
        for position, total in zip(positions, material_values + labor_values):
            axis.text(position, total + 0.12, scenario[0].upper(), ha="center", fontsize=9)
    axis.set_xticks(x_positions, [MATERIAL_LABELS[value] for value in MATERIAL_ORDER])
    axis.set_ylabel("Median real 2026 USD per square foot")
    figure.suptitle(
        "Installed cost composition by roof size",
        x=0.08,
        y=0.96,
        ha="left",
        fontsize=20,
        fontweight="bold",
    )
    figure.text(
        0.08,
        0.91,
        "S/M/L labels identify 1,200 / 2,250 / 3,500 square-foot representative roofs",
        color="#59636E",
        fontsize=12,
    )
    axis.legend(
        handles=[
            Patch(facecolor="#D6A23D", label="Material"),
            Patch(facecolor="#26747A", label="Installation labor"),
        ],
        frameon=False,
        ncol=2,
        loc="upper left",
    )
    axis.spines[["top", "right"]].set_visible(False)
    figure_paths.extend(_save_figure(figure, output_root / "installed_cost_components_2026"))
    plt.close(figure)

    quality = tables["quality"].group_by(
        "official_material_id", "material_source_level"
    ).agg(pl.col("zcta_count").sum()).to_pandas()
    levels = ["county_modeled", "state", "national"]
    level_colors = ["#26747A", "#D6A23D", "#C14953"]
    figure, axis = plt.subplots(figsize=(16, 9))
    figure.subplots_adjust(left=0.08, right=0.98, bottom=0.1, top=0.82)
    bottoms = np.zeros(len(MATERIAL_ORDER))
    totals = (
        quality.groupby("official_material_id")["zcta_count"]
        .sum()
        .reindex(MATERIAL_ORDER)
        .to_numpy()
    )
    for level, color in zip(levels, level_colors):
        subset = quality[quality["material_source_level"] == level].set_index(
            "official_material_id"
        ).reindex(MATERIAL_ORDER, fill_value=0)
        values = 100.0 * subset["zcta_count"].to_numpy() / totals
        axis.bar(
            x_positions,
            values,
            bottom=bottoms,
            color=color,
            label=level.replace("_", " ").title(),
        )
        for position, bottom, value in zip(x_positions, bottoms, values):
            if value >= 1.0:
                axis.text(
                    position,
                    bottom + value / 2,
                    f"{value:.1f}%",
                    ha="center",
                    va="center",
                    color="white",
                    fontweight="bold",
                )
        bottoms += values
    axis.set_xticks(x_positions, [MATERIAL_LABELS[value] for value in MATERIAL_ORDER])
    axis.set_ylim(0, 100)
    axis.set_ylabel("Share of published ZCTAs")
    figure.suptitle(
        "Material price source geography",
        x=0.08,
        y=0.96,
        ha="left",
        fontsize=20,
        fontweight="bold",
    )
    figure.text(
        0.08,
        0.91,
        "County modeled 96.9% · National fallback 3.1% · State fallback 0% in the current source release",
        color="#59636E",
        fontsize=12,
    )
    axis.spines[["top", "right"]].set_visible(False)
    figure_paths.extend(_save_figure(figure, output_root / "material_source_quality"))
    plt.close(figure)

    geometry = gpd.read_file(
        f"zip://{zcta_geometry_path}", columns=["GEOID20", "geometry"]
    ).rename(columns={"GEOID20": "zcta5"})
    map_values = tables["map_values"].filter(
        ~pl.col("state_code").is_in(["AK", "HI", "PR"])
    ).to_pandas()
    lower, upper = map_values["installed_cost_usd_per_sqft"].quantile([0.02, 0.98])
    figure, axes_grid = plt.subplots(2, 2, figsize=(16, 9))
    figure.subplots_adjust(
        left=0.03, right=0.97, bottom=0.12, top=0.84, hspace=0.13, wspace=0.04
    )
    map_axes = [axes_grid[0, 0], axes_grid[0, 1], axes_grid[1, 0]]
    for axis, material_id in zip(map_axes, MATERIAL_ORDER):
        values = map_values[map_values["official_material_id"] == material_id]
        mapped = geometry.merge(values, on="zcta5", how="inner", validate="one_to_one")
        mapped.plot(
            column="installed_cost_usd_per_sqft",
            ax=axis,
            cmap="viridis",
            vmin=lower,
            vmax=upper,
            linewidth=0,
            rasterized=True,
            missing_kwds={"color": "#E6E8EA"},
        )
        axis.set_xlim(-125, -66)
        axis.set_ylim(24, 50)
        axis.set_title(MATERIAL_LABELS[material_id], fontweight="bold")
        axis.set_axis_off()
    notes_axis = axes_grid[1, 1]
    notes_axis.set_axis_off()
    notes_axis.text(
        0.03,
        0.88,
        "How to read this map",
        transform=notes_axis.transAxes,
        fontsize=17,
        fontweight="bold",
    )
    notes_axis.text(
        0.03,
        0.72,
        "Common color scale across all materials\n"
        "Color range clipped at the combined p2–p98\n"
        f"{map_scenario.title()} representative roof scenario\n"
        "Material + installation labor only\n"
        "White areas have no published ZCTA geometry\n\n"
        "Values are real 2026 USD per square foot.\n"
        "Geographic source precision is reported separately.",
        transform=notes_axis.transAxes,
        fontsize=12,
        color="#40474F",
        linespacing=1.6,
        va="top",
    )
    colorbar = figure.colorbar(
        mpl.cm.ScalarMappable(
            norm=mpl.colors.Normalize(vmin=lower, vmax=upper), cmap="viridis"
        ),
        ax=map_axes,
        orientation="horizontal",
        fraction=0.05,
        pad=0.035,
    )
    colorbar.set_label("Installed cost · real 2026 USD per square foot")
    figure.suptitle(
        f"Roof installation cost across the contiguous U.S. · {map_year}",
        x=0.03,
        y=0.95,
        ha="left",
        fontsize=18,
        fontweight="bold",
    )
    figure_paths.extend(_save_figure(figure, output_root / f"installed_cost_maps_{map_year}"))
    plt.close(figure)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "map_year": map_year,
        "map_scenario": map_scenario,
        "metric": "real_2026_usd_per_sqft",
        "reference_manifest_path": str(reference_manifest_path),
        "reference_manifest_sha256": _sha256(reference_manifest_path),
        "zcta_geometry_path": str(zcta_geometry_path),
        "zcta_geometry_sha256": _sha256(zcta_geometry_path),
        "figures": [str(path) for path in figure_paths],
    }
    (output_root / "figure_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


def _save_figure(figure: object, path: Path) -> list[Path]:
    png_path = path.with_suffix(".png")
    svg_path = path.with_suffix(".svg")
    figure.savefig(png_path, dpi=200)
    figure.savefig(svg_path)
    return [png_path, svg_path]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Build national report tables and render static figures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zcta-dimension", type=Path, required=True)
    parser.add_argument("--zcta-geometry", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--map-year", type=int, default=2026)
    parser.add_argument("--map-scenario", default="medium")
    arguments = parser.parse_args(argv)
    config = GeographicEconomicsRunConfig()
    dimension = pl.read_parquet(arguments.zcta_dimension)
    tables = build_geographic_report_tables(
        dimension,
        config,
        arguments.repository_root,
        arguments.map_year,
        arguments.map_scenario,
    )
    manifest = render_geographic_report(
        tables,
        arguments.zcta_geometry,
        arguments.output_root,
        arguments.reference_manifest,
        arguments.map_year,
        arguments.map_scenario,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()