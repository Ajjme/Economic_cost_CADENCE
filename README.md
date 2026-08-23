# CADENCE

CADENCE is a modular roofing-resilience platform. Its implemented pipelines currently cover asset-scoped wind vulnerability and annual roof economics for the three official V1 material classes: Asphalt, Metal, and Tile.

## System Layout

The repository is organized so a human can locate each domain by intent rather than by a single flat script folder:

```text
CADENCE/
├── Data/
│   ├── Climate_Delta/
│   ├── Climate_Zones/
│   ├── Fragility_Curves/
│   ├── User_Inputs/
│   ├── Wind_Return_Periods/
│   └── ...other source datasets for future modules...
├── Data_Catalogs/
│   └── Mapping/
├── src/
│   └── cadence/
│       ├── __init__.py
│       ├── economics/
│       │   ├── cli.py
│       │   ├── contracts.py
│       │   ├── costs.py
│       │   ├── externalities.py
│       │   ├── labor.py
│       │   ├── materials.py
│       │   └── pipeline.py
│       ├── reference_data/
│       │   ├── __init__.py
│       │   ├── fragility.py
│       │   └── year1_damage.py
│       └── vulnerability/
│           ├── __init__.py
│           ├── expected_damage.py
│           ├── pipeline.py
│           └── year1_damage.py
├── tests/
│   ├── test_asset_scoped_damage.py
│   └── test_economics_*.py
├── cadence_datalake/
│   └── results/
├── README.md
├── pyproject.toml
└── Future_enhancements.md
```

The important distinction is:

- `Data/` holds source datasets by domain.
- `src/cadence/reference_data/` holds the shared lookup and fragility-reference code that the system reuses.
- `src/cadence/vulnerability/` is the human-facing module that calculates projected damage at user-provided locations.
- `src/cadence/economics/` calculates annual installed, replacement, repair, disposal, carbon, and expected loss-of-use values from precomputed asset features.
- Additional modules can be added alongside `vulnerability/` and `economics/`, with domain-specific source inputs under `Data/`.

## Vulnerability Module

The Vulnerability module is the first end-to-end calculation pipeline in the codebase. It does the following:

1. Reads the user asset workbook from `Data/User_Inputs/` and validates each location, roof age, and terrain.
2. Finds the nearest CONUS404 wind grid cell from `Data/Wind_Return_Periods/gev_return_periods.csv`.
3. Joins each asset to its IECC climate zone from `Data/Climate_Zones/ClimateZones.shp`.
4. Loads the approved fragility curves from `Data/Fragility_Curves/iecc2021` for the relevant climate zone, age, terrain, and material proxy.
5. Interpolates the six return-period gust values, integrates the damage curve over annual exceedance probability, and computes year-one expected damage by roof material.
6. Publishes a cached lookup and per-asset material results for the portfolio run.

In other words, the Vulnerability module is the process of taking and combining:

- `Data/User_Inputs/` for asset points and roof metadata
- `Data/Wind_Return_Periods/` for the hazard return-period gust values
- `Data/Climate_Zones/` for the climate-zone polygon assignment
- `Data/Fragility_Curves/` for the HAZUS-based damage curves
- `Data/Climate_Delta/` as the climate context that can be layered into future modules and scenario logic

This is the material component of the platform for the current code path: project damage at the user-supplied locations for Asphalt, Metal, and Tile roofs.

## Data Mapping Status

CADENCE preserves raw source labels and resolves them through versioned, source-aware mappings before simulation. The current review artifacts are:

- `Data_Catalogs/Mapping/data_consistency_review_2026-07-31.md`
- `Data_Catalogs/Mapping/source_dataset_registry_draft.csv`
- `Data_Catalogs/Mapping/canonical_roof_materials_draft.csv`
- `Data_Catalogs/Mapping/master_mapping_reference_draft.csv`
- `Data_Catalogs/Mapping/material_coverage_matrix.csv`
- `Data_Catalogs/Mapping/official_material_class_map_v1.csv`
- `Data_Catalogs/Mapping/material_consolidation_rules_v1.csv`
- `Data_Catalogs/Mapping/material_consolidation_rules_v1.md`

The source taxonomy retains all 17 specific roof coverings currently observed in the data, but the V1 model exposes exactly three official options: Asphalt, Metal, and Tile. The versioned official-class map determines class membership, core averaging membership, defaults, and explicit exclusions. Generic `asphalt`, `metal`, and `tile` records remain provenance placeholders and resolve through explicit defaults: asphalt to architectural asphalt, metal to corrugated panel, and tile to concrete tile. A `default_applied` flag must identify those resolutions.

Class-level values are derived through the domain rules, not by averaging every numeric source row. The V1 core members are 3-tab, architectural, and premium architectural for Asphalt; corrugated, standing seam, and metal tile/shingle for Metal; and clay and concrete for Tile. Each domain rule defines its grouping grain, missing-member behavior, and whether arithmetic averaging is valid. Fragility curves are never averaged: a class must select one approved common proxy. The later ranking product compares the three official options by asset location and the run's enabled cost and benefit streams; it is not a static material or resilience tier.

Missing source coverage is handled only through an explicit mapping for that source and context. For example, 3-tab and architectural asphalt share the current general-asphalt EOL factor. That does not make their prices, masses, service lives, or labor requirements interchangeable. Fuzzy material matching and global "nearest material" substitutions are prohibited.

An in-scope option without an observed material price requires a user-supplied price override before it can enter cost-effectiveness ranking. CADENCE does not substitute one of the three currently priced materials across families. Fiberglass shingle mat/insulation and gypsum roof cover board are ignored by current option and calculation logic; their raw source rows remain available only for provenance.

For the three observed price buckets, the exact-material geography fallback is ZIP, then CBSA, then state, then national. If no national exact-material row exists, the user price override rule applies.

Approved physical/labor fallbacks are context-specific:

- premium architectural asphalt uses architectural asphalt mass and 27.5-year physical life;
- corrugated, standing-seam, and metal tile/shingle forms use generic Metal Roofing mass (110 lb/square) and 55-year physical life;
- Class 4 impact-resistant asphalt uses premium-architectural labor hours;
- wood shake/shingle requires user mass and EUL values.

When a selected recycling EOL factor is missing, use the same material's landfill factor and flag the pathway substitution. TPO still requires a user EOL-factor override because its landfill factor is also missing.

Burnout/RUL precedence is:

1. user-supplied EUL;
2. canonical physical-material service life;
3. unresolved/error when neither exists.

HAZUS archetype EUL remains separate and is used only as part of an approved fragility-aging method.

### Interim Fragility Assumption

The current fragility data has no asphalt roof-covering category. V1 actively uses the `WSF1`/wood-class HAZUS curves as an interim proxy for all four asphalt options while preserving each option's asphalt material ID for costs, mass, carbon, and service life. This is a model proxy, not a claim that asphalt is wood. Every affected lookup and result must carry `fragility_proxy_applied=true`, the proxy ID, mapping version, and low-confidence assumption status.

V1 uses `SERBL` (Steel Engineered Residential Buildings, 1-2 Stories) as the active interim fragility proxy for Metal and `MSF1` (Single Family Homes, 1 Story - Masonry) for Tile. These are model proxies, not physical-material synonyms. Results retain the physical material class and carry the proxy ID, mapping version, low-confidence assumption status, and `fragility_proxy_applied=true`. HAZUS proxy EUL does not control physical burnout timing, and `MERBL` remains excluded.

## Year 1 Expected Roof Damage

The production calculation is asset-scoped. It reads user locations, roof ages, and HAZUS terrain from the asset workbook; maps each unique location to the nearest CONUS404 wind-grid center and its IECC climate zone; and calculates Asphalt, Metal, and Tile only for the unique location/age/terrain keys present in that upload. Assets sharing a key reuse one calculation. No full-grid material-age-terrain damage cube is required.

Accepted terrain labels and codes are `1 Open`, `2 Light Suburban`, `3 Suburban`, `4 Light Trees`, and `5 Trees`. The current test workbook uses the corrected `Terrain` and `Terrain_numeric` headers; ingestion also accepts canonical lowercase `terrain` and `terrain_id`. If both label and code are supplied, they must agree. Roof ages above 30 use the age-30 fragility curve and carry `age_capped=true`.

The calculation uses baseline CONUS404 3-second-gust point estimates and the approved Asphalt (`WSF1`), Metal (`SERBL`), and Tile (`MSF1`) proxies. It equal-averages unresolved construction variants within proxy, climate zone, age, and terrain. Return-period damage is integrated over annual exceedance probability with the provisional endpoints `(AEP=1, damage=0)` and `(AEP=0, damage=1)`. Wind values are kept in mph throughout the pipeline: the CONUS404 hazard grid is converted once from m/s to mph when attached, and all fragility interpolation uses mph values thereafter.

Set up the workspace environment and run the focused tests:

```shell
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,geo]'
.venv/bin/python -m pytest -q
```

Calculate only the assets in the uploaded workbook:

```shell
.venv/bin/cadence-calculate-year1-damage \
	--assets Data/User_Inputs/asset_inventory_test_1.xlsx \
	--wind-csv Data/Wind_Return_Periods/gev_return_periods.csv \
	--climate-zones Data/Climate_Zones/ClimateZones.shp \
	--fragility-root Data/Fragility_Curves/iecc2021 \
	--output-root cadence_datalake/results/year_1_expected_roof_damage
```

The command writes a compact unique-key calculation cache, a portfolio-specific asset-by-material result, and a manifest. Different portfolios can reuse equivalent calculation keys without sharing asset rows. Cache identity includes selected gust values and the selected fragility partitions' paths, sizes, and modification timestamps. Locations outside the IECC polygons are rejected rather than assigned a guessed climate zone.

## County Material Price Expansion

The standalone material-price builder expands the sparse 2026 Home Depot ZIP observations to the counties with usable 2024 GDP-per-capita data. For each source material, it selects the geographically nearest sampled county that has an observed price and multiplies the observed price statistics by the target-to-source GDP-per-capita ratio, capped to `0.6` through `1.6`. It also creates an explicitly derived `tile_proxy` at `1.2` times each county's projected corrugated-metal price.

```shell
.venv/bin/cadence-expand-material-prices
```

The command writes `Data/Materials/Start_Year_2026/home_depot_material_price_expanded.csv` and an adjacent provenance manifest. The output contains source ZIP/county, distance, GDP ratio, cap, and derivation fields. This is an upstream reference-data artifact; the current economics lookup continues to use its existing ZIP, CBSA, state, and national fallback files.

## Economics Module

The economics pipeline calculates annual real-2026-dollar costs for Asphalt, Metal, and Tile from 2026 through 2050. For each asset and year it creates three candidate rows, including the same-material replacement counterfactual, and combines:

1. exact-subtype material prices with `ZIP -> CBSA -> state -> national` fallback;
2. catalog-controlled subtype-to-class consolidation and material growth;
3. occupation-level installation labor with constrained median wage projections;
4. landfill tipping fees for the removed current roof;
5. landfill emissions monetized with the selected SC-CO2 series;
6. optional annual expected damage and loss-of-use inputs from vulnerability calculations; and
7. user replacement values and installed class overrides under explicit precedence rules.

The pipeline is vectorized across assets and options and uses only precomputed IDs. It never reads geometry or performs a spatial join.

### Required Asset Features

The asset feature input must already contain `asset_id`, `official_current_material_id`, `roof_area_sqft`, `replacement_value_usd`, `zip_code`, `cbsa_code`, `state_code`, `county_fips`, `labor_market_id`, `roof_shape`, `roof_deck_attachment`, and `roof_wall_connection`. Geometry fields may be null only when the JSON run configuration supplies explicit defaults. Geography IDs must be precomputed during ingestion.

`official_current_material_id` must be one of `OFFICIAL_ASPHALT`, `OFFICIAL_METAL`, or `OFFICIAL_TILE`. CSV readers preserve leading zeros for ZIP, CBSA, county FIPS, and labor-market identifiers. The economics runtime expects material labels to have been resolved upstream through the approved source-aware mappings.

### Configuration

The JSON configuration requires full-installed 2026-dollar-per-square-foot overrides for all three official classes. Each override includes fixed material and labor shares that sum to one and control how the override grows through 2050.

```json
{
	"start_year": 2026,
	"end_year": 2050,
	"enabled_cost_streams": [
		"material",
		"labor",
		"disposal",
		"carbon",
		"loss_of_use"
	],
	"installed_cost_overrides": {
		"OFFICIAL_ASPHALT": {
			"installed_usd_per_sqft": 10.0,
			"material_share": 0.6,
			"labor_share": 0.4
		},
		"OFFICIAL_METAL": {
			"installed_usd_per_sqft": 15.0,
			"material_share": 0.7,
			"labor_share": 0.3
		},
		"OFFICIAL_TILE": {
			"installed_usd_per_sqft": 20.0,
			"material_share": 0.5,
			"labor_share": 0.5
		}
	},
	"default_roof_shape": "flat",
	"default_roof_deck_attachment": "6d_6in_12in",
	"default_roof_wall_connection": "strap",
	"scghg_discount_rate": 2.0,
	"replacement_value_tolerance_percent": 20.0
}
```

The current-roof option uses the asset's projected `replacement_value_usd`. Alternative options use the projected class override. Source-computed material and labor costs remain separate audit values and sanity checks. Tile source pricing remains null and `blocked_without_override`; CADENCE does not invent a cross-family substitute.

### Optional Hazard Economics

An optional CSV or Parquet table may supply `asset_id`, `year`, `official_material_id`, `expected_damage_ratio`, and `expected_loss_of_use_days`. Damage ratios must be between zero and one. Missing damage or downtime remains null and is flagged rather than interpreted as zero.

### Run The Pipeline

Run the pipeline from precomputed CSV or Parquet inputs:

```shell
.venv/bin/cadence-calculate-roof-economics \
	--assets path/to/asset_features.parquet \
	--config path/to/economics_config.json \
	--annual-hazard-economics path/to/annual_hazard_economics.parquet \
	--output-root cadence_datalake/results/roof_economics
```

Omit `--annual-hazard-economics` when annual damage and downtime are not yet available. Replacement estimates are still produced; repair and loss-of-use fields remain null and carry incomplete flags.

The equivalent Python entry point is:

```python
from pathlib import Path

import polars as pl

from cadence.economics import EconomicsRunConfig, run_economics_pipeline

assets = pl.read_parquet("path/to/asset_features.parquet")
config = EconomicsRunConfig.model_validate_json(
	Path("path/to/economics_config.json").read_text()
)
manifest = run_economics_pipeline(
	assets=assets,
	config=config,
	repository_root=Path.cwd(),
	output_root=Path("cadence_datalake/results/roof_economics"),
)
```

### Outputs And Reuse

Outputs are immutable and partitioned as:

```text
schema_version=v0.1.0/
└── run_id={deterministic_hash}/
	├── annual_roof_option_costs/
	│   └── year=2026..2050/part-00000.parquet
	├── replacement_value_sanity_checks.parquet
	└── run_metadata.json
```

`annual_roof_option_costs` contains exactly `asset_count × 25 years × 3 classes` rows. The sanity-check table compares the preferred user value with source-computed and class-override estimates for the current roof. The manifest records source checksums, the validated run configuration, assumptions, and row counts. Repeating an identical run returns the existing manifest instead of rewriting persisted results.

This phase excludes tear-off labor, NPV, BCR, payback, incentives, insurance rules, adoption decisions, and stock-flow transitions. Disposal covers tipping fees only. Expected loss of use remains an annual hazard loss rather than replacement CapEx, and missing annual damage or downtime stays null instead of being treated as zero.

See [Economics Pipeline Guide](docs/economics.md) for complete formulas, schemas, source mappings, output definitions, reproducibility behavior, and troubleshooting.

## Geographic Economics Implementation

The national-costing implementation is available alongside the asset pipeline. It validates an ID-only Census ZCTA dimension, constructs annual material and labor components from the repository reference data, and publishes costs at:

```text
(zcta5, year, official_material_id, roof_scenario_id)
```

The locked default scenarios are 1,200, 2,250, and 3,500 square feet using the documented baseline construction values. Each output row contains material, labor, and combined installed cost per square foot plus the corresponding representative-roof total. Additional source-geography, method, and confidence columns supplied by upstream builders are preserved in the published facts.

Build the versioned national ZCTA dimension from the acquired NHGIS and Census sources:

```shell
.venv/bin/cadence-build-zcta-dimension \
	--zcta-crosswalk Data/Geography/IPUMS_NHGIS/raw/v2020/nhgis_blk2010_zcta2020/nhgis_blk2010_zcta2020.csv \
	--cbsa-crosswalk Data/Geography/IPUMS_NHGIS/raw/v2020/nhgis_blk2010_cbsa2020.zip \
	--labor-coverage Data/Labor/Start_Year_2026/labor_geography_coverage.csv \
	--zcta-geometry Data/Geography/Census_TIGER/raw/v2020/tl_2020_us_zcta520.zip \
	--labor-geometry Data/Labor/Start_Year_2026/labor_geographies.parquet \
	--output-root cadence_datalake/reference_geo/zcta
```

The resulting `zcta_dimension.parquet` contains 33,774 unique 2020 ZCTAs with dominant county and CBSA assignments derived from NHGIS block interpolation weights. Covered CBSAs map directly to BLS wage areas; other ZCTAs use representative-point assignment to the 2019 BLS polygons with explicit overlap and nearest-gap flags. The manifest records source checksums, weight-share quality measures, assignment-method counts, and 17 Census territorial polygons excluded because they are outside the NHGIS U.S./Puerto Rico block crosswalk coverage.

Run the publisher from the repository reference data:

```shell
.venv/bin/cadence-calculate-geographic-roof-economics \
	--zcta-crosswalk path/to/zcta_crosswalk.parquet \
	--config path/to/geographic_economics_config.json \
	--repository-root . \
	--output-root cadence_datalake/results/geographic_roof_economics
```

The ZCTA crosswalk requires `zcta5`, `state_code`, `county_fips`, `cbsa_code`, `labor_market_id`, `crosswalk_method`, and `source_vintage`. The publisher uses the expanded county material-price artifact, annual material escalation, representative-roof labor productivity, and BLS wage projections. Material rows retain county model anchors, GDP-cap status, missing members, and confidence. Labor rows retain labor-market, source-area, imputation, and provisional-productivity provenance. Tile is explicitly labeled as a low-confidence modeled proxy.

Each reference-driven run publishes two independent year-partitioned facts. `annual_installed_costs` compares candidate Asphalt, Metal, and Tile installation material and labor costs. `annual_removal_costs` compares disposal and landfill-carbon costs by removed roof class. Removal costs include per-square-foot and representative-roof totals, retain state/region/national tipping-fee provenance and annual SC-CO2 values, and explicitly exclude tear-off labor.

Supply `--annual-source-costs path/to/annual_source_costs.parquet` only to override automatic source construction with an already resolved table. The publisher rejects duplicate or unresolved ZCTAs, incomplete year/material/scenario grids, invalid scenario areas, and null or negative source costs. Results are immutable, deterministic, and partitioned by year under schema `v1.0.0`.

Material prices use an auditable geographic hierarchy. Counties present in the GDP expansion use `county_modeled` values at fallback rank 1. A missing county uses an exact observed state member at rank 2 when available, then the exact observed national member at rank 3. Tile remains the explicit `1.2 × corrugated Metal` proxy derived at the selected geography. Every row retains the requested county, actual source level and ID, fallback rank, method, confidence, and missing class members. This hierarchy resolves all 33,774 published ZCTAs without null material costs.

The Streamlit/Plotly visualization layer remains to be implemented. The existing asset economics command remains unchanged.

### Static Report Figures

Install the report dependencies and render the national figure set:

```shell
.venv/bin/python -m pip install -e '.[report]'
MPLBACKEND=Agg .venv/bin/cadence-render-economic-report \
	--zcta-dimension cadence_datalake/reference_geo/zcta/version=2020-nhgis-bls2019-v1.0.0/zcta_dimension.parquet \
	--zcta-geometry Data/Geography/Census_Cartographic/raw/v2020/cb_2020_us_zcta520_500k.zip \
	--reference-manifest cadence_datalake/reference_geo/zcta/version=2020-nhgis-bls2019-v1.0.0/manifest.json \
	--repository-root . \
	--output-root cadence_datalake/reports/geographic_roof_economics
```

The command produces PowerPoint-ready 16:9 PNG and SVG versions of:

- national installed-cost trajectories with p10-p90 spatial bands;
- 2026 material and installation-labor composition by material and representative roof size;
- 2026 contiguous-U.S. ZCTA installed-cost maps on a common color scale; and
- material-price source-geography shares.

Compact Parquet/CSV source tables and `figure_manifest.json` are written beside the figures so every chart remains traceable to its geography reference and assumptions.
