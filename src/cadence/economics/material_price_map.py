"""Render county choropleth maps for projected material prices."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional, Sequence

import geopandas as gpd
import matplotlib as mpl
import matplotlib.pyplot as plt
import polars as pl

CONTINENTAL_US_STATE_FIPS = {
    "01", "04", "05", "06", "08", "09", "10", "11", "12", "13", "16", "17",
    "18", "19", "20", "21", "22", "23", "24", "25", "26", "27", "28", "29",
    "30", "31", "32", "33", "34", "35", "36", "37", "38", "39", "40", "41",
    "42", "44", "45", "46", "47", "48", "49", "50", "51", "53", "54", "55",
    "56",
}

CONTINENTAL_US_STATE_ABBRS = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

STATE_ABBREVIATIONS = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

STATE_NAME_TO_ABBREVIATION = {value: key for key, value in STATE_ABBREVIATIONS.items()}

DEFAULT_COUNTY_GEOJSON = (
    "https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_20m.zip"
)
DEFAULT_STATE_GEOJSON = (
    "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/"
    "data/geojson/us-states.json"
)
DEFAULT_PRICE_CSV = (
    Path("Data/Materials/Start_Year_2026/home_depot_material_price_expanded.csv")
)
DEFAULT_OUTPUT_ROOT = Path("Data/Materials/Start_Year_2026/material_price_maps")
DEFAULT_COMBINED_OUTPUT_FILENAME = "materials_combined_median_price_per_square.png"
DEFAULT_STATE_OUTPUT_ROOT = (
    Path("Data/Materials/Start_Year_2026/material_price_maps_by_state")
)
MAP_BORDER_LINEWIDTH = 0.25

MATERIAL_LABELS = {
    "asphalt_3_tab": "Asphalt 3-tab",
    "asphalt_architectural": "Asphalt architectural",
    "metal_corrugated_panel": "Metal corrugated panel",
}


def _require_columns(frame: pl.DataFrame, required: set[str], name: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing columns: {missing}")


def build_material_price_map_table(
    source: pl.DataFrame,
    material_classes: Sequence[str],
) -> pl.DataFrame:
    """Filter projected material prices to the continental U.S. by county or state."""
    material_set = {str(item) for item in material_classes}

    if "state" in source.columns and "county_fips" not in source.columns:
        required = {"state", "material_class", "median_price_per_square"}
        _require_columns(source, required, "state material prices")
        result = (
            source.with_columns(
                pl.col("state").cast(pl.String).str.to_uppercase().alias("state"),
                pl.col("median_price_per_square").cast(pl.Float64),
            )
            .filter(pl.col("material_class").is_in(material_set))
            .filter(pl.col("state").is_in(CONTINENTAL_US_STATE_ABBRS))
            .sort(["material_class", "state"])
            .select("state", "material_class", "median_price_per_square")
        )
        return result.unique(subset=["state", "material_class"], keep="first").sort(
            "state"
        )

    required = {"county_fips", "material_class", "median_price_per_square"}
    _require_columns(source, required, "material prices")

    result = (
        source.with_columns(
            pl.col("county_fips").cast(pl.String).str.strip_chars().str.zfill(5),
            pl.col("median_price_per_square").cast(pl.Float64),
        )
        .with_columns(pl.col("county_fips").str.slice(0, 2).alias("state_fips"))
        .filter(pl.col("material_class").is_in(material_set))
        .filter(pl.col("state_fips").is_in(CONTINENTAL_US_STATE_FIPS))
        .sort(["material_class", "county_fips"])
        .select("county_fips", "state_fips", "material_class", "median_price_per_square")
    )
    return (
        result.unique(subset=["county_fips", "material_class"], keep="first")
        .sort("county_fips")
    )


def _prepare_joined_geography(
    prices: pl.DataFrame,
    county_geojson: str,
    state_geojson: str,
) -> tuple[gpd.GeoDataFrame, str, str, str]:
    if "state" in prices.columns and "county_fips" not in prices.columns:
        states = gpd.read_file(state_geojson)[["id", "name", "geometry"]].copy()
        states["state"] = states["name"].map(STATE_NAME_TO_ABBREVIATION)
        states = states[states["state"].isin(CONTINENTAL_US_STATE_ABBRS)].copy()
        states["state"] = states["state"].astype(str).str.upper()
        joined = states.merge(
            prices.with_columns(pl.col("state").cast(pl.String).str.to_uppercase()).to_pandas(),
            on="state",
            how="left",
        )
        return joined, "state", "median_price_per_square", "State"

    counties = gpd.read_file(county_geojson)[["GEOID", "STATEFP", "geometry"]].copy()
    counties = counties[counties["STATEFP"].isin(CONTINENTAL_US_STATE_FIPS)].copy()
    counties["county_fips"] = counties["GEOID"].astype(str).str.zfill(5)
    joined = counties.merge(
        prices.with_columns(
            pl.col("county_fips").cast(pl.String).str.strip_chars().str.zfill(5)
        ).to_pandas(),
        on="county_fips",
        how="left",
    )
    return joined, "county_fips", "median_price_per_square", "County"


def render_material_price_maps(
    csv_path: Path,
    output_root: Path,
    county_geojson: str = DEFAULT_COUNTY_GEOJSON,
    state_geojson: str = DEFAULT_STATE_GEOJSON,
    material_classes: Sequence[str] = (
        "asphalt_3_tab",
        "asphalt_architectural",
        "metal_corrugated_panel",
    ),
) -> dict[str, Path]:
    """Render a PNG choropleth for each requested material class."""
    output_root.mkdir(parents=True, exist_ok=True)
    source = pl.read_csv(
        csv_path,
        schema_overrides={
            "county_fips": pl.String,
            "state": pl.String,
            "material_class": pl.String,
            "median_price_per_square": pl.Float64,
        },
    )
    prices = build_material_price_map_table(source, material_classes)
    joined, geography_key, value_key, title_prefix = _prepare_joined_geography(
        prices,
        county_geojson,
        state_geojson,
    )

    output_paths: dict[str, Path] = {}

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

    base_geography = joined[[geography_key, "geometry"]].drop_duplicates(
        subset=[geography_key]
    )

    for material_class in material_classes:
        merged = joined[joined["material_class"] == material_class].copy()
        if merged.empty:
            merged = base_geography.copy()
            merged[value_key] = float("nan")

        fig, ax = plt.subplots(figsize=(16, 10))
        merged.plot(
            column=value_key,
            ax=ax,
            cmap="YlOrRd",
            linewidth=MAP_BORDER_LINEWIDTH,
            edgecolor="#D9DEE3",
            missing_kwds={
                "color": "#F3F5F7",
                "edgecolor": "#D9DEE3",
                "label": "No price data",
            },
        )
        ax.set_axis_off()
        ax.set_title(
            (
                f"Projected {MATERIAL_LABELS.get(material_class, material_class)} "
                f"median price per square foot by {title_prefix.lower()}"
            ),
            loc="left",
            pad=20,
            fontweight="bold",
        )

        valid_values = merged[value_key].dropna()
        if not valid_values.empty:
            colorbar = fig.colorbar(
                mpl.cm.ScalarMappable(
                    norm=mpl.colors.Normalize(
                        vmin=float(valid_values.min()),
                        vmax=float(valid_values.max()),
                    ),
                    cmap="YlOrRd",
                ),
                ax=ax,
                orientation="horizontal",
                fraction=0.03,
                pad=0.04,
            )
            colorbar.set_label("Median price per square foot (USD / sq ft)")

        output_path = output_root / f"{material_class}_median_price_per_square.png"
        fig.savefig(output_path, dpi=220, bbox_inches="tight")
        plt.close(fig)
        output_paths[material_class] = output_path

    return output_paths


def render_combined_material_price_map(
    csv_path: Path,
    output_path: Path,
    county_geojson: str = DEFAULT_COUNTY_GEOJSON,
    state_geojson: str = DEFAULT_STATE_GEOJSON,
    material_classes: Sequence[str] = (
        "asphalt_3_tab",
        "asphalt_architectural",
        "metal_corrugated_panel",
    ),
) -> Path:
    """Render a single multi-panel PNG with a shared viridis legend scale."""
    if not material_classes:
        raise ValueError("material_classes must include at least one material class")

    source = pl.read_csv(
        csv_path,
        schema_overrides={
            "county_fips": pl.String,
            "state": pl.String,
            "material_class": pl.String,
            "median_price_per_square": pl.Float64,
        },
    )
    prices = build_material_price_map_table(source, material_classes)
    joined, geography_key, value_key, title_prefix = _prepare_joined_geography(
        prices,
        county_geojson,
        state_geojson,
    )

    global_values = joined[joined["material_class"].isin(material_classes)][value_key].dropna()
    if global_values.empty:
        raise ValueError("No price values available for the requested material classes")

    vmin = float(global_values.min())
    vmax = float(global_values.max())
    if vmin == vmax:
        vmax = vmin + 1e-9

    mpl.rcParams.update(
        {
            "font.family": ["Avenir Next", "DejaVu Sans"],
            "font.size": 11,
            "axes.titlesize": 14,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "savefig.facecolor": "white",
        }
    )

    panel_count = len(material_classes)
    fig, axes = plt.subplots(1, panel_count, figsize=(6.4 * panel_count, 7.2))
    axes_sequence = [axes] if panel_count == 1 else list(axes)

    base_geography = joined[[geography_key, "geometry"]].drop_duplicates(
        subset=[geography_key]
    )

    for axis, material_class in zip(axes_sequence, material_classes):
        merged = joined[joined["material_class"] == material_class].copy()
        if merged.empty:
            merged = base_geography.copy()
            merged[value_key] = float("nan")

        merged.plot(
            column=value_key,
            ax=axis,
            cmap="viridis",
            vmin=vmin,
            vmax=vmax,
            linewidth=MAP_BORDER_LINEWIDTH,
            edgecolor="#D9DEE3",
            missing_kwds={
                "color": "#F3F5F7",
                "edgecolor": "#D9DEE3",
                "label": "No price data",
            },
        )
        axis.set_axis_off()
        axis.set_title(
            MATERIAL_LABELS.get(material_class, material_class),
            loc="left",
            pad=12,
            fontweight="bold",
        )

    fig.suptitle(
        f"Projected median roofing material price per square foot by {title_prefix.lower()}",
        x=0.03,
        y=0.98,
        ha="left",
        fontweight="bold",
        fontsize=16,
    )
    fig.subplots_adjust(left=0.02, right=0.98, top=0.9, bottom=0.14, wspace=0.02)

    colorbar = fig.colorbar(
        mpl.cm.ScalarMappable(
            norm=mpl.colors.Normalize(vmin=vmin, vmax=vmax),
            cmap="viridis",
        ),
        ax=axes_sequence,
        orientation="horizontal",
        fraction=0.04,
        pad=0.05,
    )
    colorbar.set_label("Median price per square foot (USD / sq ft)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Render the requested county material-price maps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=DEFAULT_PRICE_CSV,
        help="Expanded county price CSV to map.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory for the rendered PNG files.",
    )
    parser.add_argument(
        "--county-geojson",
        default=DEFAULT_COUNTY_GEOJSON,
        help="Census county boundary dataset to use for the county map.",
    )
    parser.add_argument(
        "--state-geojson",
        default=DEFAULT_STATE_GEOJSON,
        help="U.S. state boundary dataset to use for the state map.",
    )
    arguments = parser.parse_args(argv)

    output_paths = render_material_price_maps(
        csv_path=arguments.csv,
        output_root=arguments.output_root,
        county_geojson=arguments.county_geojson,
        state_geojson=arguments.state_geojson,
    )
    combined_output_path = render_combined_material_price_map(
        csv_path=arguments.csv,
        output_path=arguments.output_root / DEFAULT_COMBINED_OUTPUT_FILENAME,
        county_geojson=arguments.county_geojson,
        state_geojson=arguments.state_geojson,
    )
    for material_class, path in output_paths.items():
        print(f"{material_class}: {path}")
    print(f"combined: {combined_output_path}")


if __name__ == "__main__":
    main()
