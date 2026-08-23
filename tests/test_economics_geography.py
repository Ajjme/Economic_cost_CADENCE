import polars as pl
import pytest

from cadence.economics.geography import build_zcta_dimension


def _crosswalk() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "zcta5": ["02108", "29401"],
            "state_code": ["MA", "SC"],
            "county_fips": ["25025", "45019"],
            "cbsa_code": ["14460", "16700"],
            "labor_market_id": ["0014460", "0016700"],
            "crosswalk_method": ["largest_overlap", "largest_overlap"],
            "source_vintage": ["2020", "2020"],
        }
    )


def test_builds_sorted_id_only_zcta_dimension() -> None:
    result = build_zcta_dimension(_crosswalk().reverse())

    assert result["zcta5"].to_list() == ["02108", "29401"]
    assert result.row(0, named=True)["county_fips"] == "25025"
    assert "geometry" not in result.columns


def test_rejects_duplicate_zcta_assignment() -> None:
    crosswalk = pl.concat([_crosswalk(), _crosswalk().head(1)])

    with pytest.raises(ValueError, match="exactly one row per zcta5"):
        build_zcta_dimension(crosswalk)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("zcta5", "2108"),
        ("county_fips", "5025"),
        ("labor_market_id", None),
        ("crosswalk_method", ""),
    ],
)
def test_rejects_invalid_or_unresolved_assignment(
    column: str, value: object
) -> None:
    crosswalk = _crosswalk().with_columns(
        pl.when(pl.col("zcta5") == "02108")
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .alias(column)
    )

    with pytest.raises(ValueError, match="invalid or unresolved"):
        build_zcta_dimension(crosswalk)