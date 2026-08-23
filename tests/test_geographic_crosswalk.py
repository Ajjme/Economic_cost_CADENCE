from pathlib import Path

import polars as pl

from cadence.reference_data.geographic_crosswalk import (
    build_national_zcta_dimension,
)


def test_builds_dominant_county_cbsa_and_direct_labor_area(tmp_path: Path) -> None:
    zcta_path = tmp_path / "zcta.csv"
    zcta_path.write_text(
        "blk2010gj,blk2010ge,zcta2020gj,zcta2020ge,parea,weight\n"
        "G1,010010001001001,GZ,G02108,1,0.8\n"
        "G2,010030001001002,GZ,G02108,1,0.2\n".replace("G02108", "02108"),
        encoding="utf-8",
    )
    cbsa_path = tmp_path / "cbsa.csv"
    cbsa_path.write_text(
        "blk2010gj,blk2010ge,cbsa2020gj,cbsa2020ge,parea,weight\n"
        "G1,010010001001001,GC,10180,1,1\n"
        "G2,010030001001002,GC,99999,1,1\n",
        encoding="utf-8",
    )
    labor_path = tmp_path / "labor.csv"
    pl.DataFrame(
        {
            "AREA": ["0010180"],
            "AREA_TITLE": ["Test metro"],
            "PRIM_STATE": ["AL"],
            "GEOGRAPHY_TYPE": ["msa"],
            "COVERAGE_STATUS": ["matched"],
        }
    ).write_csv(labor_path)

    result = build_national_zcta_dimension(
        zcta_path,
        cbsa_path,
        labor_path,
    )
    row = result.row(0, named=True)

    assert row["zcta5"] == "02108"
    assert row["county_fips"] == "01001"
    assert row["state_code"] == "AL"
    assert row["cbsa_code"] == "10180"
    assert row["labor_market_id"] == "0010180"
    assert row["county_weight_share"] == 0.8
    assert row["cbsa_weight_share"] == 0.8
    assert row["crosswalk_method"] == "nhgis_weighted_direct_cbsa"


def test_rejects_geometry_only_area_as_direct_wage_coverage(tmp_path: Path) -> None:
    zcta_path = tmp_path / "zcta.csv"
    zcta_path.write_text(
        "blk2010gj,blk2010ge,zcta2020gj,zcta2020ge,parea,weight\n"
        "G1,010010001001001,GZ,02108,1,1\n",
        encoding="utf-8",
    )
    cbsa_path = tmp_path / "cbsa.csv"
    cbsa_path.write_text(
        "blk2010gj,blk2010ge,cbsa2020gj,cbsa2020ge,parea,weight\n"
        "G1,010010001001001,GC,10180,1,1\n",
        encoding="utf-8",
    )
    labor_path = tmp_path / "labor.csv"
    pl.DataFrame(
        {
            "AREA": ["0010180"],
            "AREA_TITLE": [None],
            "PRIM_STATE": [None],
            "GEOGRAPHY_TYPE": ["msa"],
            "COVERAGE_STATUS": ["geometry_only"],
        }
    ).write_csv(labor_path)

    try:
        build_national_zcta_dimension(zcta_path, cbsa_path, labor_path)
    except ValueError as error:
        assert "geometry paths are required" in str(error)
    else:
        raise AssertionError("geometry-only labor area must not count as wage coverage")