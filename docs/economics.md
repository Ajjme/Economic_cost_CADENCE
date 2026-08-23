# Economics Pipeline Guide

## Purpose And Scope

The CADENCE economics module produces annual roof-option cost records for every asset, year from 2026 through 2050, and official V1 material class:

- `OFFICIAL_ASPHALT`
- `OFFICIAL_METAL`
- `OFFICIAL_TILE`

It is a standalone Polars pipeline under `src/cadence/economics`. It can be called from Python, tests, or the `cadence-calculate-roof-economics` command without Prefect. The runtime reads ID-keyed source tables only and performs no geometry operation.

The module currently answers four questions:

1. What is the annual installed replacement value of the current roof and the two alternative material classes?
2. What source-computed material and installation-labor estimate supports or challenges the user value?
3. What disposal, landfill-carbon, and expected loss-of-use costs are associated with the option?
4. Given an annual expected damage ratio, what is the provisional repair estimate?

It does not yet calculate NPV, BCR, payback, incentives, insurance effects, adoption decisions, burnout replacement, or year-to-year roof-state transitions.

## Package Layout

| Module | Responsibility |
|---|---|
| `contracts.py` | Pydantic configuration and input value contracts |
| `materials.py` | Price geography fallback, class consolidation, material growth, and mass |
| `labor.py` | Geometry defaults, size bucket, occupation productivity, wages, and startup labor |
| `externalities.py` | Disposal, landfill emissions, SC-CO2 monetization, and temporary housing |
| `costs.py` | Option expansion, valuation precedence, enabled totals, repairs, and sanity checks |
| `pipeline.py` | Source orchestration, deterministic run identity, partition writes, and manifest |
| `cli.py` | CSV/Parquet and JSON command-line interface |

## Runtime Flow

```text
precomputed asset_features
  ├── material labels already resolved to OFFICIAL_* IDs
  ├── ZIP / CBSA / state / county IDs
  └── labor_market_id
          │
          ▼
material reference builders ─┐
labor cost builder ──────────┼─> asset × 25 years × 3 classes
external cost builder ───────┤          │
optional hazard economics ───┘          ▼
                                  annual option costs
                                      │       │
                                      │       └─> current-roof sanity checks
                                      └─> immutable Parquet partitions + manifest
```

The annual option expansion is vectorized. There is no Python loop over assets or material options. The pipeline writes one compact file per annual partition.

## Inputs

### Asset Feature Table

The `--assets` argument accepts CSV or Parquet. The runtime requires these columns:

| Column | Contract | Use |
|---|---|---|
| `asset_id` | Unique, nonblank string | Asset key |
| `official_current_material_id` | One of the three official classes | Current-roof counterfactual and removal mass |
| `roof_area_sqft` | Positive number | Installed total, mass, startup-labor allocation |
| `replacement_value_usd` | Nonnegative real 2026 dollars | Preferred current-roof installed value |
| `zip_code` | Five-character string | First material-price geography |
| `cbsa_code` | Five-character string | Second material-price geography |
| `state_code` | Two-letter code | Material-price and disposal geography |
| `county_fips` | Five-character string | Temporary-housing lookup |
| `labor_market_id` | Existing BLS MSA/BOS `AREA` string | Annual wage projection lookup |
| `roof_shape` | String or null | Labor productivity key |
| `roof_deck_attachment` | String or null | Labor productivity key |
| `roof_wall_connection` | String or null | Labor productivity key |

All columns must exist. The three roof-construction values may be null only when configuration defaults are provided. `size_bucket` is derived from `roof_area_sqft`:

| Bucket | Area |
|---|---|
| `small` | Less than 1,500 sqft |
| `medium` | 1,500 through 3,000 sqft |
| `large` | Greater than 3,000 sqft |

Material resolution belongs upstream. Raw values such as `asphalt`, `metal`, or `tile` must first pass through the source-aware mapping catalog and approved defaults. The economics runtime does not fuzzy-match them.

Example CSV:

```csv
asset_id,official_current_material_id,roof_area_sqft,replacement_value_usd,zip_code,cbsa_code,state_code,county_fips,labor_market_id,roof_shape,roof_deck_attachment,roof_wall_connection
A-1,OFFICIAL_ASPHALT,1800,21600,77001,26420,TX,48201,0010180,flat,6d_6in_12in,strap
```

### Run Configuration

The `--config` argument is a JSON document validated by `EconomicsRunConfig`.

| Field | Required | Contract |
|---|---|---|
| `start_year` | No | Must be `2026`; default `2026` |
| `end_year` | No | Must be `2050`; default `2050` |
| `enabled_cost_streams` | No | Subset of `material`, `labor`, `disposal`, `carbon`, `loss_of_use`; defaults to material and labor |
| `installed_cost_overrides` | Yes | Exactly one Asphalt, Metal, and Tile override |
| `default_roof_shape` | Yes | Nonblank labor lookup default |
| `default_roof_deck_attachment` | Yes | Nonblank labor lookup default |
| `default_roof_wall_connection` | Yes | Nonblank labor lookup default |
| `scghg_discount_rate` | No | `1.5`, `2.0`, or `2.5`; default `2.0` |
| `replacement_value_tolerance_percent` | No | Nonnegative percentage; default `20.0` |

Each installed override has:

```json
{
  "installed_usd_per_sqft": 15.0,
  "material_share": 0.7,
  "labor_share": 0.3
}
```

The shares must each be between zero and one and sum to one. They are fixed 2026 weights used to blend annual material and local labor growth. Explicit weights are particularly important for Tile because no observed Tile material price exists.

Material and labor are the installed-cost basis and are always represented by `installed_capex_usd`. `enabled_cost_streams` controls whether disposal, carbon, and loss of use are added to the corresponding enabled totals. The material and labor names are retained in the controlled vocabulary for compatibility with the broader run configuration.

### Optional Annual Hazard Economics

The `--annual-hazard-economics` argument accepts CSV or Parquet. Supported columns are:

| Column | Required when supplied | Contract |
|---|---|---|
| `asset_id` | Yes | Existing asset key |
| `year` | Yes | 2026 through 2050 |
| `official_material_id` | Yes | Candidate official class |
| `expected_damage_ratio` | Optional | Null or value in `[0, 1]` |
| `expected_loss_of_use_days` | Optional | Expected annual downtime days |

The full intended grain is one row per asset, year, and official class. If `expected_damage_ratio` is absent, repair values remain null. If `expected_loss_of_use_days` is absent, expected loss-of-use values remain null. Missing values are never converted to zero.

## Material Cost Construction

### Price Fallback

For each asset and exact core subtype, material prices use this fallback:

```text
ZIP -> CBSA -> state -> national
```

Fallback happens before class consolidation. Price source values map to canonical IDs through `master_mapping_reference_draft.csv`, then to official classes through `official_material_class_map_v1.csv`.

The core members are:

| Official class | Core members |
|---|---|
| Asphalt | 3-tab, architectural, premium architectural |
| Metal | corrugated panel, standing seam, metal tile/shingle form |
| Tile | clay, concrete |

The source price is the arithmetic mean of available exact members at their resolved geographies. Every row records contributors and missing members.

Current expected statuses are:

| Class | Source status |
|---|---|
| Asphalt | Partial: premium architectural is missing |
| Metal | Partial: standing seam and metal tile are missing |
| Tile | `blocked_without_override`: no observed members |

Material prices are stored per roofing square and divided by 100 to produce dollars per square foot.

### Material Growth

Subtype annual material factors are mapped and averaged by official class. For year $y$:

$$
g_{m,y} = f_m^{y-2026}
$$

where $f_m$ is the consolidated annual material escalation factor. These are real relative cost-growth assumptions; general CPI is not applied.

## Labor Cost Construction

The productivity table is keyed by candidate subtype, roof shape, deck attachment, wall connection, size bucket, and occupation. Core subtype values are averaged only inside matching non-material keys.

The module uses:

- all occupation rows in the productivity model;
- `H_MEDIAN_CONSTRAINED_PROJECTED_WAGE` for 2026 through 2050;
- the asset's exact precomputed BLS `labor_market_id`;
- fixed productivity through time; and
- startup person-hours charged once per roof and allocated across roof area.

For occupation $o$:

$$
C_{variable,o,y} = h_{o} \times w_{o,y}
$$

$$
C_{startup,o,y} = \frac{s_o \times w_{o,y}}{A}
$$

The annual labor unit cost is:

$$
C_{labor,y} = \sum_o (C_{variable,o,y} + C_{startup,o,y})
$$

where $h_o$ is person-hours per sqft, $s_o$ is startup person-hours, $w_{o,y}$ is the annual hourly wage, and $A$ is roof area.

The wage projection table contains MSA and BOS rows. State/national fallback is already represented in the baseline wage provenance fields (`SOURCE_LEVEL`, `SOURCE_AREA`, and `IS_IMPUTED`) used to construct those area projections. The runtime therefore requires an exact existing `labor_market_id`; it does not perform another geography fallback.

All current productivity parameters are provisional `ASSUMPTION_V1` values. Outputs retain that status and any geometry-default or wage-imputation flags.

## Removal And External Costs

### Disposal

Removal mass uses the current roof, not the candidate replacement:

$$
M_{removed} = A \times m_{current}
$$

Disposal uses state, then EREF region, then national average:

$$
C_{disposal} = \frac{M_{removed}}{2000} \times F_{tip}
$$

where mass is pounds and $F_{tip}$ is dollars per short ton. Disposal is landfill-only in the current pipeline. EREF values are held constant as fixed 2026-real proxies through 2050.

### Landfill Carbon

The class landfill factor is catalog-mapped and consolidated from core members. Annual emissions and monetized carbon are:

$$
E_{kg} = M_{removed} \times EF_{landfill}
$$

$$
C_{carbon,y} = \frac{E_{kg}}{1000} \times SC\text{-}CO2_y
$$

The run selects the CO2 series at a 1.5%, 2.0%, or 2.5% source discount rate. This selection chooses a published SC-CO2 column; it does not discount CADENCE cash flows.

### Expected Loss Of Use

Expected loss of use is candidate-specific:

$$
C_{LOU,y} = D_{y} \times H_{county}
$$

where $D_y$ is expected annual downtime days and $H_{county}$ is the county's daily temporary-housing cost. The current implementation uses the source's two-bedroom primary rate and holds it constant as a fixed 2026-real proxy.

Loss of use is not replacement CapEx. It is added only to `enabled_economic_total_usd` when enabled.

## Valuation Tracks And Precedence

The pipeline deliberately preserves three separate values:

1. **Source-computed installed value:** annual material plus installation labor; used for audit and sanity checking.
2. **Class installed override:** user-provided 2026 installed $/sqft projected with the class's fixed material/labor blend.
3. **Asset replacement value:** user-provided 2026 total dollars projected with the current class's fixed material/labor blend.

For class $c$ and year $y$, the installed growth factor is:

$$
g_{c,y} = s_{material,c} g_{material,c,y} + s_{labor,c} g_{labor,c,y}
$$

Operational precedence is:

| Option row | Operational installed value |
|---|---|
| Candidate equals current roof | Projected asset `replacement_value_usd` |
| Candidate differs from current roof | Projected class installed override × roof area |

The source-computed value never silently replaces either operational value.

## Replacement And Repair Formulas

Installed CapEx is:

$$
C_{installed} = C_{operational,sqft} \times A
$$

Replacement economic cost is:

$$
C_{replacement} = C_{installed} + C_{enabled\ disposal} + C_{enabled\ carbon}
$$

The all-enabled analytical total is:

$$
C_{enabled\ total} = C_{replacement} + C_{enabled\ loss\ of\ use}
$$

Provisional repair is:

$$
C_{repair} = C_{installed} \times r_{damage}
$$

Repair does not prorate disposal, carbon, or loss of use. This is a provisional proxy, not a repair-specific labor/material model.

## Outputs

### Directory Layout

```text
{output_root}/
└── schema_version=v0.1.0/
    └── run_id={24-character-hash}/
        ├── annual_roof_option_costs/
        │   ├── year=2026/part-00000.parquet
        │   ├── ...
        │   └── year=2050/part-00000.parquet
        ├── replacement_value_sanity_checks.parquet
        └── run_metadata.json
```

### Annual Roof Option Costs

The primary key is `(asset_id, year, official_material_id)`. Important output groups are:

| Group | Columns |
|---|---|
| Identity | `asset_id`, `year`, `official_material_id`, `official_current_material_id`, `is_current_roof_option` |
| Growth | `material_growth_factor`, `labor_growth_factor`, `installed_growth_factor`, override shares |
| Source audit | `source_material_usd_per_sqft`, `source_labor_usd_per_sqft`, `source_installed_usd_per_sqft`, `material_price_status`, `missing_member_ids` |
| Operational | `operational_cost_source`, `operational_installed_usd_per_sqft`, `installed_capex_usd` |
| External costs | `disposal_cost_usd`, `carbon_cost_usd`, `expected_loss_of_use_usd` |
| Enabled totals | `replacement_economic_cost_usd`, `enabled_economic_total_usd` |
| Repair | `expected_damage_ratio`, `provisional_repair_cost_usd`, `repair_cost_incomplete` |
| Assumptions | `dollar_basis`, `loss_of_use_incomplete`, `tear_off_labor_excluded` |

Exactly three rows are emitted per asset-year, so the expected row count is:

$$
N_{rows} = N_{assets} \times 25 \times 3
$$

### Replacement Value Sanity Checks

One current-roof row is emitted per asset-year with:

- projected user value;
- source-computed installed value;
- class-override value;
- source absolute and percentage variance;
- override percentage variance;
- configured source-tolerance flag; and
- incomplete-source flag.

If source material pricing is incomplete, arithmetic involving that null remains null and the comparison is marked incomplete.

### Run Manifest And Reuse

`run_metadata.json` records:

- deterministic `run_id`;
- schema version;
- UTC timestamp;
- validated run configuration;
- checksums for every source path;
- asset and output row counts;
- real-dollar basis and tear-off exclusion; and
- output paths.

The run ID hashes assets, configuration, optional hazard economics, and source checksums. If the manifest already exists, the pipeline returns it and does not rewrite output files.

## CLI Usage

Install the project:

```shell
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,geo]'
```

Run with replacement costs only:

```shell
.venv/bin/cadence-calculate-roof-economics \
  --assets path/to/asset_features.parquet \
  --config path/to/economics_config.json \
  --output-root cadence_datalake/results/roof_economics
```

Run with annual repair and loss-of-use inputs:

```shell
.venv/bin/cadence-calculate-roof-economics \
  --assets path/to/asset_features.parquet \
  --config path/to/economics_config.json \
  --annual-hazard-economics path/to/annual_hazard_economics.parquet \
  --repository-root . \
  --output-root cadence_datalake/results/roof_economics
```

`--repository-root` defaults to the current working directory. Input tables must be CSV or Parquet.

## Python Usage

```python
from pathlib import Path

import polars as pl

from cadence.economics import EconomicsRunConfig, run_economics_pipeline

repository_root = Path.cwd()
assets = pl.read_parquet("path/to/asset_features.parquet")
hazard = pl.read_parquet("path/to/annual_hazard_economics.parquet")
config = EconomicsRunConfig.model_validate_json(
    Path("path/to/economics_config.json").read_text(encoding="utf-8")
)

manifest = run_economics_pipeline(
    assets=assets,
    config=config,
    repository_root=repository_root,
    output_root=repository_root / "cadence_datalake/results/roof_economics",
    annual_hazard_economics=hazard,
)
```

The lower-level public calculation function `build_annual_roof_option_costs` can be imported from `cadence.economics` when already-normalized annual growth, source cost, damage, and external-cost tables are available.

## Validation And Troubleshooting

Run the focused economics tests:

```shell
.venv/bin/python -m pytest -q tests/test_economics_*.py
```

Common failures:

| Error | Meaning | Resolution |
|---|---|---|
| Missing required asset columns | Economics did not receive a complete precomputed feature table | Add the listed columns during ingestion |
| Unsupported current material | Raw or non-V1 material reached runtime | Resolve through the mapping catalog before economics |
| Labor productivity unresolved | Roof construction values/defaults do not match the productivity vocabulary | Use values from the controlled user-input options |
| Wage projection missing | `labor_market_id` is not an existing projected BLS area | Fix the upstream labor-area crosswalk |
| Growth row-count violation | Asset-year-class growth is incomplete or duplicated | Require exactly one row per asset, year, and official class |
| Damage ratio outside `[0, 1]` | Hazard economics violates the repair contract | Correct or quarantine the source row |
| Tile source cost is null | Expected current source gap | Supply the required Tile installed override; do not invent a source price |

## Known Limitations

- Material source prices are partial for Asphalt and Metal and absent for Tile.
- Labor productivity is provisional and low confidence.
- Tear-off labor is excluded; disposal includes tipping fees only.
- Disposal and temporary-housing values are fixed real-dollar proxies through 2050.
- Landfill is the only active EOL pathway.
- The temporary-housing model uses a fixed two-bedroom rate.
- Repair is a damage-ratio proxy, not a repair-specific cost model.
- The module does not perform ingestion geography assignments.
- The module does not implement NPV, BCR, payback, incentives, insurance behavior, adoption, burnout precedence, or stock-flow state transitions.

## Geographic Reference-Cost Pipeline

The geographic pipeline is a separate contract from asset operational costing. `GeographicEconomicsRunConfig` defines the 2026-2050 horizon and representative roof scenarios without requiring a current roof, replacement value, or user-entered location. `build_zcta_dimension` validates one ID-only county, CBSA, state, and labor-market assignment per ZCTA. Geometry remains a presentation-layer concern.

`build_geographic_installed_costs` accepts resolved annual material and labor costs at `(zcta5, year, official_material_id, roof_scenario_id)` grain. It enforces a complete Cartesian grid and calculates:

$$
C_{installed,sqft} = C_{material,sqft} + C_{labor,sqft}
$$

$$
C_{representative\ roof} = C_{installed,sqft} \times A_{scenario}
$$

`build_geographic_annual_source_costs` constructs this complete grain directly from the repository reference data. It joins ZCTAs to the expanded county material-price artifact and projects those values with class material growth. Asphalt averages the available 3-tab and architectural rows, Metal uses corrugated panel, and Tile uses the explicitly derived `tile_proxy`; missing members and confidence remain visible. The labor component directly crosses ZCTAs with configured roof scenarios, joins the existing class productivity model, and attaches occupation-level BLS wage projections by `labor_market_id`.

When a dominant county is absent from the GDP-expanded price artifact, exact source members use this controlled fallback:

```text
county modeled (rank 1) -> observed state (rank 2) -> observed national (rank 3)
```

Fallback is selected per exact source material before official-class consolidation. Tile is then derived as `1.2 × corrugated Metal` at the selected state or national level and remains labeled as a low-confidence proxy. The requested county is retained separately from `material_source_level` and `material_source_id`, preventing national values from being presented as county observations.

`run_geographic_economics_from_references` builds those components and then writes one Parquet partition per year, the validated ZCTA dimension, and a deterministic manifest under `schema_version=v1.0.0`. Input row order does not affect run identity. Component source geography, method, confidence, wage imputation, GDP-cap, and missing-member fields are retained in output. `run_geographic_economics_pipeline` remains available when callers need to publish an independently prepared source-cost table.

Reference-driven runs also publish `annual_removal_costs` at `(zcta5, year, removed_official_material_id, roof_scenario_id)` grain. Removed-roof mass uses the official class mass and representative scenario area. Disposal uses the existing state, EREF region, then national fallback. Landfill carbon uses the class factor and selected annual SC-CO2 series. Both streams are available per square foot and per representative roof. This fact remains separate from candidate installation costs because a universal geographic comparison has no observed current roof; tear-off labor remains excluded and flagged.