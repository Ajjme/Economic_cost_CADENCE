from pathlib import Path

from cadence.economics.geographic_cli import _read_table, main


def test_geographic_csv_reader_preserves_leading_zero_ids(tmp_path: Path) -> None:
    path = tmp_path / "crosswalk.csv"
    path.write_text(
        "zcta5,state_code,county_fips,cbsa_code,labor_market_id\n"
        "02108,MA,25025,14460,0014460\n",
        encoding="utf-8",
    )

    row = _read_table(path).row(0, named=True)

    assert row["zcta5"] == "02108"
    assert row["labor_market_id"] == "0014460"


def test_cli_builds_source_costs_from_repository_references(tmp_path: Path) -> None:
    crosswalk = tmp_path / "crosswalk.csv"
    crosswalk.write_text(
        "zcta5,state_code,county_fips,cbsa_code,labor_market_id,crosswalk_method,source_vintage\n"
        "79601,TX,01001,10180,0010180,test_fixture,2020\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    output = tmp_path / "results"

    main(
        [
            "--zcta-crosswalk",
            str(crosswalk),
            "--config",
            str(config),
            "--repository-root",
            str(Path.cwd()),
            "--output-root",
            str(output),
        ]
    )

    manifests = list(output.glob("schema_version=*/run_id=*/run_metadata.json"))
    assert len(manifests) == 1
    run_root = manifests[0].parent
    assert len(list((run_root / "annual_installed_costs").glob("year=*"))) == 25
    assert len(list((run_root / "annual_removal_costs").glob("year=*"))) == 25