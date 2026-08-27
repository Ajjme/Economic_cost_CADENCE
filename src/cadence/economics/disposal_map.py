"""Render a state choropleth for EREF tipping fees."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import polars as pl

DEFAULT_STATES_GEOJSON = (
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/"
    "data/geojson/us-states.json"
)

REGION_ROWS = {
    "Pacific",
    "Northeast",
    "Mountains/Plains",
    "Midwest",
    "Southeast",
    "South Central",
}


def build_state_tipping_fee_table(csv_path: Path) -> pl.DataFrame:
    """Return only the state rows from the parsed EREF tipping fee table."""
    return (
        pl.read_csv(
            csv_path,
            schema_overrides={
                "Region/State": pl.String,
                "Average tipping Fee": pl.Float64,
                "Standard Deviation": pl.Float64,
            },
        )
        .filter(~pl.col("Region/State").is_in(REGION_ROWS))
        .filter(pl.col("Region/State") != "National Average")
        .rename(
            {
                "Region/State": "state_name",
                "Average tipping Fee": "average_tipping_fee_usd_per_ton",
                "Standard Deviation": "standard_deviation_usd_per_ton",
            }
        )
        .select(
            "state_name",
            "average_tipping_fee_usd_per_ton",
            "standard_deviation_usd_per_ton",
        )
        .sort("state_name")
    )


def render_tipping_fee_map(
    csv_path: Path,
    output_stem: Path,
    states_geojson: str = DEFAULT_STATES_GEOJSON,
) -> dict[str, Path]:
    """Render PNG and SVG state maps colored by average tipping fee."""
    mpl.rcParams.update(
        {
            "font.family": ["Avenir Next", "DejaVu Sans"],
            "font.size": 11,
            "axes.titlesize": 16,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )

    state_values = build_state_tipping_fee_table(csv_path).to_pandas()
    geometry = gpd.read_file(states_geojson)[["name", "geometry"]].rename(
        columns={"name": "state_name"}
    )
    geometry = geometry[geometry["state_name"] != "Alaska"]
    merged = geometry.merge(state_values, on="state_name", how="left")

    figure, axis = plt.subplots(figsize=(16, 10))
    merged.plot(
        column="average_tipping_fee_usd_per_ton",
        ax=axis,
        cmap="YlOrRd",
        linewidth=0.7,
        edgecolor="#D0D5DA",
        missing_kwds={
            "color": "#F1F3F5",
            "edgecolor": "#D0D5DA",
            "hatch": "///",
            "label": "No fee data",
        },
    )
    axis.set_axis_off()
    axis.set_title(
        "2024 EREF tipping fees by state", loc="left", pad=18, fontweight="bold"
    )
    figure.text(
        0.06,
        0.91,
        "Color shows the average tipping fee ($/ton) from the parsed EREF 2024 table.\n"
        "Region summary rows and the national average are excluded from the choropleth.",
        color="#59636E",
        fontsize=12,
    )

    valid_values = merged["average_tipping_fee_usd_per_ton"].dropna()
    colorbar = figure.colorbar(
        mpl.cm.ScalarMappable(
            norm=mpl.colors.Normalize(
                vmin=float(valid_values.min()), vmax=float(valid_values.max())
            ),
            cmap="YlOrRd",
        ),
        ax=axis,
        orientation="horizontal",
        fraction=0.04,
        pad=0.04,
    )
    colorbar.set_label("Average tipping fee (USD per ton)")

    output_png = output_stem.with_suffix(".png")
    output_svg = output_stem.with_suffix(".svg")
    output_png.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output_png, dpi=200, bbox_inches="tight")
    figure.savefig(output_svg, bbox_inches="tight")
    plt.close(figure)
    return {"png": output_png, "svg": output_svg}


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Render the tipping fee map from the parsed disposal CSV."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("Data/Disposal/EREF_2024_Tipping_Fees_Parsed.csv"),
    )
    parser.add_argument(
        "--output-stem",
        type=Path,
        default=Path("Data/Disposal/EREF_2024_Tipping_Fees_Map"),
    )
    parser.add_argument(
        "--states-geojson",
        default=DEFAULT_STATES_GEOJSON,
        help="State boundary GeoJSON or local path",
    )
    arguments = parser.parse_args(argv)
    paths = render_tipping_fee_map(
        arguments.csv,
        arguments.output_stem,
        arguments.states_geojson,
    )
    print(paths["png"])
    print(paths["svg"])


if __name__ == "__main__":
    main()