"""Publish national ZCTA roof costs from resolved annual source components."""

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

import polars as pl

from cadence.economics.contracts import GeographicEconomicsRunConfig
from cadence.economics.geographic_pipeline import (
    run_geographic_economics_from_references,
    run_geographic_economics_pipeline,
)


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Validate and publish a national geographic economics run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zcta-crosswalk", type=Path, required=True)
    parser.add_argument("--annual-source-costs", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args(argv)

    config = GeographicEconomicsRunConfig.model_validate_json(
        arguments.config.read_text(encoding="utf-8")
    )
    crosswalk = _read_table(arguments.zcta_crosswalk)
    if arguments.annual_source_costs is None:
        manifest = run_geographic_economics_from_references(
            zcta_crosswalk=crosswalk,
            config=config,
            repository_root=arguments.repository_root,
            output_root=arguments.output_root,
        )
    else:
        manifest = run_geographic_economics_pipeline(
            zcta_crosswalk=crosswalk,
            annual_source_costs=_read_table(arguments.annual_source_costs),
            config=config,
            output_root=arguments.output_root,
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
                "zcta5": pl.String,
                "cbsa_code": pl.String,
                "state_code": pl.String,
                "county_fips": pl.String,
                "labor_market_id": pl.String,
                "source_vintage": pl.String,
            },
        )
    raise ValueError("input tables must be CSV or Parquet")


if __name__ == "__main__":
    main()