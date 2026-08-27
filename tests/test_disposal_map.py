from pathlib import Path

from cadence.economics.disposal_map import build_state_tipping_fee_table


def test_build_state_tipping_fee_table_excludes_region_summary_rows() -> None:
    table = build_state_tipping_fee_table(
        Path("Data/Disposal/EREF_2024_Tipping_Fees_Parsed.csv")
    )

    assert table.height == 44
    assert "Pacific" not in table["state_name"].to_list()
    assert "National Average" not in table["state_name"].to_list()
    assert table["average_tipping_fee_usd_per_ton"].max() == 136.65