"""Command-line entry point for the standalone roof economics pipeline."""

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

import polars as pl

from cadence.economics.contracts import EconomicsRunConfig
from cadence.economics.pipeline import run_economics_pipeline


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run annual roof option costing from precomputed asset features."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--annual-hazard-economics", type=Path)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args(argv)

    assets = _read_table(arguments.assets)
    hazard = (
        _read_table(arguments.annual_hazard_economics)
        if arguments.annual_hazard_economics is not None
        else None
    )
    config = EconomicsRunConfig.model_validate_json(
        arguments.config.read_text(encoding="utf-8")
    )
    manifest = run_economics_pipeline(
        assets,
        config,
        arguments.repository_root,
        arguments.output_root,
        annual_hazard_economics=hazard,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


def _read_table(path: Path) -> pl.DataFrame:
    suffix = path.suffix.casefold()
    if suffix == ".parquet":
        return pl.read_parquet(path)
    if suffix == ".csv":
        return pl.read_csv(
            path,
            schema_overrides={
                "zip_code": pl.String,
                "cbsa_code": pl.String,
                "state_code": pl.String,
                "county_fips": pl.String,
                "labor_market_id": pl.String,
            },
        )
    raise ValueError("input tables must be CSV or Parquet")


if __name__ == "__main__":
    main()