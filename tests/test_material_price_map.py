from pathlib import Path

import geopandas as gpd
import polars as pl
import pytest
from shapely.geometry import box

from cadence.economics.material_price_map import (
    build_material_price_map_table,
    render_combined_material_price_map,
)


def test_build_material_price_map_table_filters_non_continental_us() -> None:
    source = pl.DataFrame(
        {
            "county_fips": ["01001", "02013", "11001", "53033", "72001"],
            "material_class": [
                "asphalt_3_tab",
                "asphalt_3_tab",
                "asphalt_3_tab",
                "asphalt_3_tab",
                "asphalt_3_tab",
            ],
            "median_price_per_square": [10.0, 20.0, 30.0, 40.0, 50.0],
        }
    )

    result = build_material_price_map_table(source, ["asphalt_3_tab"])

    assert result["county_fips"].to_list() == ["01001", "11001", "53033"]
    assert result["median_price_per_square"].to_list() == [10.0, 30.0, 40.0]


def test_render_combined_material_price_map_creates_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "material_prices.csv"
    pl.DataFrame(
        {
            "county_fips": ["01001", "01003", "01001", "01003", "01001", "01003"],
            "material_class": [
                "asphalt_3_tab",
                "asphalt_3_tab",
                "asphalt_architectural",
                "asphalt_architectural",
                "metal_corrugated_panel",
                "metal_corrugated_panel",
            ],
            "median_price_per_square": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
        }
    ).write_csv(csv_path)

    county_frame = gpd.GeoDataFrame(
        {
            "GEOID": ["01001", "01003"],
            "STATEFP": ["01", "01"],
            "geometry": [box(0, 0, 1, 1), box(1, 0, 2, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )

    def fake_read_file(path: str) -> gpd.GeoDataFrame:
        if path == "fake-county":
            return county_frame
        raise AssertionError(f"Unexpected path in test: {path}")

    monkeypatch.setattr(gpd, "read_file", fake_read_file)

    output_path = tmp_path / "combined.png"
    created_path = render_combined_material_price_map(
        csv_path=csv_path,
        output_path=output_path,
        county_geojson="fake-county",
        state_geojson="unused-state",
    )

    assert created_path == output_path
    assert output_path.exists()
    assert output_path.stat().st_size > 0


def test_render_combined_material_price_map_rejects_empty_material_classes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "material_prices.csv"
    pl.DataFrame(
        {
            "county_fips": ["01001"],
            "material_class": ["asphalt_3_tab"],
            "median_price_per_square": [10.0],
        }
    ).write_csv(csv_path)

    with pytest.raises(ValueError, match="material_classes"):
        render_combined_material_price_map(
            csv_path=csv_path,
            output_path=tmp_path / "combined.png",
            material_classes=(),
        )


def test_render_combined_material_price_map_uses_shared_scale(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "material_prices.csv"
    pl.DataFrame(
        {
            "county_fips": ["01001", "01001", "01001"],
            "material_class": [
                "asphalt_3_tab",
                "asphalt_architectural",
                "metal_corrugated_panel",
            ],
            "median_price_per_square": [2.5, 8.5, 11.0],
        }
    ).write_csv(csv_path)

    county_frame = gpd.GeoDataFrame(
        {
            "GEOID": ["01001"],
            "STATEFP": ["01"],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )

    def fake_read_file(path: str) -> gpd.GeoDataFrame:
        if path == "fake-county":
            return county_frame
        raise AssertionError(f"Unexpected path in test: {path}")

    captured: dict[str, float] = {}

    class DummyColorbar:
        def set_label(self, _: str) -> None:
            return None

    def fake_colorbar(self, mappable, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured["vmin"] = float(mappable.norm.vmin)
        captured["vmax"] = float(mappable.norm.vmax)
        return DummyColorbar()

    monkeypatch.setattr(gpd, "read_file", fake_read_file)
    monkeypatch.setattr("matplotlib.figure.Figure.colorbar", fake_colorbar)

    render_combined_material_price_map(
        csv_path=csv_path,
        output_path=tmp_path / "combined.png",
        county_geojson="fake-county",
        state_geojson="unused-state",
    )

    assert captured == {"vmin": 2.5, "vmax": 11.0}
