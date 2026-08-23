from pathlib import Path

import polars as pl
import pytest

from cadence.reference_data.material_price_expansion import (
    PRICE_COLUMNS,
    SOURCE_ZIP_TO_COUNTY,
    expand_material_prices,
    load_county_gdp,
    normalize_county_geoid,
)


def _source_prices() -> pl.DataFrame:
    rows = []
    material_zips = {
        "asphalt_3_tab": list(SOURCE_ZIP_TO_COUNTY),
        "asphalt_architectural": list(SOURCE_ZIP_TO_COUNTY),
        "metal_corrugated_panel": ["27705", "32459", "94582", "98004"],
    }
    for material_index, (material_class, zip_codes) in enumerate(material_zips.items()):
        for zip_index, zip_code in enumerate(zip_codes):
            price = float(100 + material_index * 100 + zip_index)
            rows.append(
                {
                    "scrape_date": "2026-07-31",
                    "retailer": "Home Depot",
                    "material_class": material_class,
                    "zip_code": zip_code,
                    "median_price_per_square": price,
                    "p25_price_per_square": price - 10,
                    "p75_price_per_square": price + 10,
                    "min_price_per_square": price - 20,
                    "max_price_per_square": price + 20,
                    "product_count": 1,
                    "store_count": 0,
                    "median_bulk_price_per_square": None,
                    "median_bulk_discount_pct": None,
                }
            )
    return pl.DataFrame(rows)


def _county_inputs() -> tuple[pl.DataFrame, pl.DataFrame]:
    coordinates = {
        "37063": (0.0, 1.0),
        "12131": (0.0, 20.0),
        "48201": (0.0, 0.0),
        "06013": (0.0, 30.0),
        "53033": (0.0, 40.0),
        "01001": (0.0, 0.1),
        "01003": (0.0, 1.1),
    }
    gdp_values = {county_fips: 100.0 for county_fips in coordinates}
    gdp_values["01001"] = 10.0
    gdp_values["01003"] = 300.0
    gdp = pl.DataFrame(
        {
            "county_fips": list(coordinates),
            "county_name": [f"County {value}" for value in coordinates],
            "state_abbr": ["TS"] * len(coordinates),
            "year": [2024] * len(coordinates),
            "gdp_per_capita": list(gdp_values.values()),
        }
    )
    points = pl.DataFrame(
        {
            "county_fips": list(coordinates),
            "latitude": [value[0] for value in coordinates.values()],
            "longitude": [value[1] for value in coordinates.values()],
        }
    )
    return gdp, points


def test_normalizes_prefixed_county_geoid() -> None:
    assert normalize_county_geoid("G04013") == "04013"
    assert normalize_county_geoid(1001) == "01001"
    with pytest.raises(ValueError, match="invalid county GEOID"):
        normalize_county_geoid("county-1")


def test_expands_with_material_specific_anchors_caps_and_tile_proxy() -> None:
    gdp, points = _county_inputs()
    result = expand_material_prices(_source_prices(), gdp, points)

    assert result.height == gdp.height * 4
    assert result.select("county_fips", "material_class").n_unique() == result.height
    assert result.sort(["county_fips", "material_class"]).equals(result)

    low_asphalt = result.filter(
        (pl.col("county_fips") == "01001")
        & (pl.col("material_class") == "asphalt_3_tab")
    ).row(0, named=True)
    low_metal = result.filter(
        (pl.col("county_fips") == "01001")
        & (pl.col("material_class") == "metal_corrugated_panel")
    ).row(0, named=True)
    high_asphalt = result.filter(
        (pl.col("county_fips") == "01003")
        & (pl.col("material_class") == "asphalt_3_tab")
    ).row(0, named=True)

    assert low_asphalt["source_county_fips"] == "48201"
    assert low_asphalt["raw_gdp_ratio"] == pytest.approx(0.1)
    assert low_asphalt["applied_gdp_ratio"] == 0.6
    assert low_asphalt["ratio_was_capped"] is True
    assert low_metal["source_county_fips"] == "37063"
    assert high_asphalt["raw_gdp_ratio"] == pytest.approx(3.0)
    assert high_asphalt["applied_gdp_ratio"] == 1.6

    metal = result.filter(pl.col("material_class") == "metal_corrugated_panel").sort(
        "county_fips"
    )
    tile = result.filter(pl.col("material_class") == "tile_proxy").sort("county_fips")
    for column in PRICE_COLUMNS:
        if column == "median_bulk_price_per_square":
            assert tile[column].null_count() == tile.height
        else:
            assert tile[column].to_list() == pytest.approx(
                (metal[column] * 1.2).to_list()
            )
    assert tile["source_county_fips"].to_list() == metal["source_county_fips"].to_list()
    assert tile["source_product_count"].null_count() == tile.height
    assert tile["is_derived"].all()


def test_rejects_missing_county_geometry() -> None:
    gdp, points = _county_inputs()
    points = points.filter(pl.col("county_fips") != "01001")

    with pytest.raises(ValueError, match="01001"):
        expand_material_prices(_source_prices(), gdp, points)


def test_load_county_gdp_filters_legacy_rows_and_rejects_duplicate_valid_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "gdp.csv"
    pl.DataFrame(
        {
            "county_fips": ["01001", "01001", "02901"],
            "county_name": ["Autauga, AL", "Autauga duplicate, AL", "Legacy, AK*"],
            "state_abbr": ["AL", "AL", None],
            "year": [2024, 2024, 2024],
            "gdp_per_capita": [50_000.0, 51_000.0, None],
        }
    ).write_csv(path)

    with pytest.raises(ValueError, match="duplicate usable county rows"):
        load_county_gdp(path, 2024)
