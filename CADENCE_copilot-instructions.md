# CADENCE Engineering Instructions for GitHub Copilot

**Scope:** This file governs all code generated for the CADENCE roofing-resilience simulation platform. CADENCE is a longitudinal decision-support system that ages a portfolio of 100–10,000+ roofing assets across 30+ simulated years under a single climate hazard trajectory, cost, and incentive conditions, using an annual stock-flow model. V1 runs against **one climate scenario only** — there is no multi-scenario (e.g. RCP) selection or comparison in V1. **Insurance variables (premium, deductible, discount) are direct user-supplied inputs** on the asset record, not a separate insurance rules/lookup table — CADENCE V1 does not model insurance rules.

Copilot should treat this document as binding architectural law for the project, not general best-practice suggestions. Where a request conflicts with a rule below, follow this document and flag the conflict rather than silently overriding it.

---

## 0. How to Use This Document

- Read this in full before generating any data pipeline, simulation, or Streamlit code for CADENCE.
- Section 12 lists decisions that are **explicitly still open**. Never silently resolve these — implement a clearly-labeled placeholder/stub and surface the decision back to the developer.
- Everything else below is a **locked decision**. Do not propose alternate architectures (e.g. "let's just use Postgres" or "let's use a plain Python loop") without being asked — V1's constraints are deliberate, not oversights.

---

## 1. Role & Mission

You are acting as a senior data engineer / simulation architect with three specialties layered on top of each other:

1. **Analytical data engineering** — star-schema modeling, columnar storage, DuckDB/Parquet pipelines.
2. **Geospatial engineering** — CRS handling, spatial indexing, H3, GeoParquet.
3. **System dynamics / stock-flow simulation** — vectorized, year-sequential, state-carrying simulation over large asset populations.

All CADENCE code sits inside the same pipeline shape:

```text
Streamlit  →  DuckDB  →  Parquet / GeoParquet  →  Python/Polars Simulation Engine  →  Streamlit Cache
```

Prefect sits alongside this as the orchestration layer for simulation runs (see §7.5).

Design before code. Prefer producing a small, independently verifiable spec or function signature set over a single large monolithic implementation. If a request is broad ("build the simulation engine"), break it into fine-grained phases and implement/verify one at a time.

---

## 2. System Architecture

```
┌─────────────┐     ┌──────────┐     ┌────────────────────┐     ┌─────────────────┐     ┌────────────────┐
│  Streamlit   │ ──▶ │  DuckDB  │ ──▶ │ Parquet/GeoParquet │ ──▶ │ Python/Polars    │ ──▶ │ Streamlit Cache │
│  (UI layer)  │ ◀── │ (SQL     │ ◀── │ (columnar storage, │ ◀── │ Simulation Engine│ ◀── │ (3-tier caching)│
│              │     │ engine)  │     │  versioned)        │     │ (stock-flow)     │     │                 │
└─────────────┘     └──────────┘     └────────────────────┘     └─────────────────┘     └────────────────┘
                                                                          │
                                                                          ▼
                                                                    Prefect (orchestrates
                                                                    simulation runs)
```

V1 target scale: **100–10,000 assets**. This is deliberately *not* a full production database (no Postgres/PostGIS, no Redis, no API backend) — that architecture is reserved for V1.5+. Do not introduce production-database complexity into V1 code.

---

## 3. Non-Negotiable Engineering Principles

These are the rules that should shape every function you write, in priority order:

1. **No Python `for` loops over asset-year-option combinations.** Ever. All simulation math is vectorized in Polars or NumPy. At target scale (e.g. 10,000 assets × 30 years × 3 upgrade options ≈ 900K+ rows under V1's single climate trajectory, and up to hundreds of millions of rows at a hypothetical future multi-scenario production scale) row-wise Python loops will not finish or will crash memory.
2. **The year loop is sequential; the asset loop inside it is vectorized.** This is the single most important tension in the whole system — do not "helpfully" try to vectorize across years. State carryforward (RUL resets, tier changes, install-year resets) makes year *N+1* depend on the computed state of year *N*. The outer loop over years must stay a real Python loop; the inner operation across all assets for that year must be a single vectorized Polars expression/DataFrame op.
3. **Precompute anything that doesn't change at runtime.** Spatial joins, crosswalks, lookup tables, and default run-configuration templates are computed once during ingestion, never recomputed inside a request/simulation cycle ("shift left").
4. **DuckDB queries Parquet directly — don't load full reference tables into pandas.** Expose stable DuckDB `VIEW`s over Parquet files; query only the columns/rows needed.
5. **Retrieval is ID-based, never fuzzy/spatial at simulation time.** The canonical chain is:
   ```
   asset_id → wind_grid_id → county_fips → cbsa_code → climate_zone
   ```
   All hazard, cost, and incentive lookups hang off IDs resolved once during ingestion — never re-derived mid-simulation. Insurance fields (premium, deductible) are direct user input carried on the asset record, not resolved via a geo/ID lookup.
6. **Enforce data contracts before writing Parquet.** Validate with Pydantic or Pandera; raise a `ContractViolationError` before a corrupted file ever reaches disk.
7. **Every simulation run must be reproducible.** Run config + data versions + code version → deterministic output. See §9.
8. **Separate simulation data from geospatial reference data — strictly.** The production simulation engine operates *exclusively* on precomputed lookup tables and ID-based relationships; it must never read a geometry column or invoke a spatial operation. See §6.5 for the required `reference_data/` vs. `reference_geo/` directory boundary.
9. **Prefect orchestrates workflows, not business logic.** Prefect coordinates ingestion, preprocessing, simulation execution, output generation, and maintenance tasks — it does not contain simulation math itself. The Python/Polars simulation engine holds all business logic and must be callable directly (script, test, notebook) without going through Prefect. See §7.5.
10. **Reference data is immutable; simulation data is mutable.** Dimension/lookup tables in `reference_data/` are never overwritten in place — only versioned (§9.1). The in-process simulation state (`assets_df`) is mutable by design as it carries forward year to year (§7.1, rule 2) — but once a run's outputs are persisted to Parquet, that run's output files are themselves write-once; a changed input produces a new `run_id`, never a mutation of a prior run's files.

---

## 4. Data Modeling Standards

### 4.1 Star Schema (not full denormalization)

Use a star schema over Parquet/DuckDB: a central fact table (`fact_annual_asset_state`) joined to dimension tables (`dim_asset`, `dim_hazard_grid_cell`, `dim_material`, `dim_resilience_tier`, etc.). Do **not** flatten everything into one wide table (e.g. don't repeat a material's full taxonomy description on every asset-year row) — DuckDB's vectorized joins are fast enough that normalization cost is negligible, and denormalizing bloats storage and invites update anomalies.

### 4.2 Slowly Changing Dimensions — Type 2

`dim_asset` (and any dimension whose real-world attributes change independent of the simulation, e.g. a physical roof getting replaced in reality mid-study) must use **Type 2 SCD**: add `valid_from` / `valid_to` columns rather than overwriting rows.

```sql
-- Retrieval pattern for simulation year 2026
SELECT * FROM dim_asset
WHERE valid_from <= 2026 AND valid_to > 2026;
```

This is required, not optional — reproducing a past simulation run means being able to reconstruct the exact dimension state as of that run, which is impossible if rows are overwritten in place.

### 4.3 Schema Versioning

Parquet has no server enforcing a fixed schema, so schema drift can silently break the app. Partition output by schema version:

```text
s3://cadence-data/simulation_outputs/schema_version=v1.2/run_id=123/...
```

Streamlit must check the schema version of a run before visualizing it and fail cleanly (or migrate) rather than crash on an unexpected column.

### 4.4 Data Contracts

Use **Pydantic** or **Pandera** to enforce contracts in the simulation engine. Example contract shape:

> `fact_annual_asset_state` MUST contain `asset_id` (string, non-null), `year` (int, > 2020), `expected_loss` (float, ≥ 0.0).

Violations must throw before the write, not be caught downstream in Streamlit.

### 4.5 Immutability contract: reference data vs. simulation data

- **Reference data (`reference_data/`, `reference_geo/`) is immutable.** Dimension and lookup tables are never edited in place. A change to a fragility curve, cost table, or hazard product is published as a new version under a new versioned path (§9.1) — the old version stays on disk untouched, so historical runs remain reproducible.
- **Simulation data is mutable — but only the in-flight state, not persisted output.** The engine's working `assets_df` legitimately mutates every year of the outer loop (RUL decrements, tier changes, install-year resets — §7.1, rule 2). That mutability is the whole point of the stock-flow model. Once a run finishes and its output tables are written to `runs/{simulation_run_id}/`, those Parquet files are write-once, exactly like reference data — never patched or overwritten after the fact. A different input, config, or code version produces a new `run_id`, not a mutated old one.
- Practically: if you find yourself writing code that opens an existing run's output Parquet file in "append" or "overwrite" mode to patch it, or that edits a `reference_data/` file in place instead of publishing a new version, stop — both violate this contract.

### 4.6 Canonical taxonomies and source-value mappings

Raw categorical labels are source data, not join keys. During ingestion, resolve each source label through a versioned, source-aware mapping table before it can enter a simulation lookup. The minimum mapping key is:

```text
(mapping_domain, source_dataset_id, source_column,
 normalized_source_value, mapping_version)
```

The mapping record must preserve the original `source_value` and identify the `canonical_id`, relationship type, approval status, confidence, and whether a decision is still required.

Required relationship types include:

- **Exact alias/bucket:** the source term and canonical category have the same intended meaning in that lookup context.
- **Shared source bucket:** one source row intentionally serves multiple canonical categories, such as a combined EOL factor. Sharing is valid only for that named source/context and does not merge the canonical categories.
- **Group-only value:** a label such as `asphalt`, `metal`, or `tile` identifies a family but not a simulation-ready subtype. It must be resolved by an explicit input/default policy or rejected during ingestion.
- **Model proxy:** a canonical physical material is assigned to a different model taxonomy, such as a HAZUS building archetype. A proxy is never a synonym and must be versioned and approved independently.

Hard rules:

1. Keep **physical roof material**, **HAZUS building/material archetype**, **resilience tier**, **upgrade option**, and **derived economic rank** as separate fields/dimensions. Never overwrite one with another.
2. Do not fuzzy-match or guess an unrecognized value at simulation time. Unmapped, ambiguous, unapproved, or one-to-many source values fail the ingestion contract or enter an explicit quarantine/review output.
3. Preserve the raw source label on the cleaned record for provenance and remapping.
4. Apply source-specific mappings during ingestion, then use canonical IDs for all simulation-time joins.
5. A source-specific shared bucket or proxy cannot become a global equivalence rule. For example, sharing one carbon factor does not imply equal price, mass, service life, labor, or fragility.
6. Promotion of a draft mapping to `reference_data/` requires a new immutable mapping version and coverage checks against every controlled categorical value in the source version.

---

## 5. Python + DuckDB Layer

### 5.1 DuckDB as the query engine, not a database server

DuckDB is in-process OLAP over Parquet/GeoParquet files — there is no centralized DB server to manage. Expose views, don't load-then-filter in pandas:

```sql
CREATE VIEW asset_features AS
SELECT * FROM read_parquet('portfolios/{portfolio_id}/asset_features.parquet');

CREATE VIEW wind_return_periods AS
SELECT * FROM read_parquet('reference/climate/wind_return_periods.parquet');

CREATE VIEW fragility_curves AS
SELECT * FROM read_parquet('reference/vulnerability/fragility_curves.parquet');
```

The simulation engine queries these views for exactly the slice it needs (predicate pushdown handles the filtering efficiently at the Parquet row-group level — see §8.4).

### 5.2 Vectorization rules

- Simulation year-to-year transition math is done in **Polars DataFrames**, operating on all assets simultaneously for a given year.
- Reserve NumPy for lower-level numeric transforms feeding into or out of Polars expressions.
- If you find yourself writing `for asset in assets:`, stop — reframe as a column expression (`.with_columns(...)`, `.select(...)`, boolean masks) across the whole asset table for that year.

### 5.3 Where DuckDB vs. Polars is used

- **DuckDB**: reference-data retrieval, joins between fact/dimension tables, ad hoc aggregation queries that feed Streamlit charts.
- **Polars**: the actual year-over-year stock-flow transition math inside the simulation engine (state mutation, RUL decrement, decision-rule evaluation).

---

## 6. Geospatial Infrastructure

### 6.1 Coordinate Reference System (CRS) contract

- **Ingestion contract:** reject any pipeline input that is not WGS84 (EPSG:4326). All stored lat/lon is WGS84.
- Projected CRS math (e.g. EPSG:3857 for distance/area) happens only at the display layer (Streamlit / mapping) or inside dynamic queries — never baked into stored reference data.
- Raw climate model output (CONUS404) is native to a **custom Lambert Conformal Conic (LCC) grid** — not EPSG:4326 and not a standard EPSG LCC definition. This custom projected CRS is used for distance, cell-containment, and polygon-area math. EPSG:4326 is used only for storage/interchange of the exported grid-cell center coordinates.
- The climate reference table now carries **both** a true `latitude`/`longitude` grid-cell center (EPSG:4326) **and** the underlying `lat_idx`/`lon_idx` model grid indices. Both are exact — see §6.2 for which to use as a join key and when.

### 6.2 CONUS404 hazard grid: join keys and point assignment (climate reference data)

The wind/hazard reference table (`wind_return_periods` and related CONUS404-derived products) is a **complete row-major grid**: every `(lat_idx, lon_idx)` pair from `(0, 0)` through `(1014, 1366)` — 1,387,505 cells total — has exactly one `latitude`/`longitude` center and one set of return-period fields. `latitude`/`longitude` are grid-cell centers on a curvilinear grid, **not** regular 1D axes — do not treat them as a simple lat/lon mesh you can slice independently.

**Rule: prefer the integer grid-index join over the float lat/lon join whenever possible.**

- **Same-grid joins (any two datasets both derived from this CONUS404 pipeline):** join on the composite integer key `(lat_idx, lon_idx)`. This is exact and deterministic — no nearest-neighbor search needed. **Never join on floating-point latitude/longitude equality** when grid indices are available on both sides; float equality is not reliable as a join key.
- **External point data (asset centroids, addresses, weather stations)** that do *not* already carry CONUS404 grid indices — a spatial assignment is unavoidable. In order of rigor:
  1. **Best:** reproject the point into the native CONUS404 LCC grid and compute its row/column directly (deterministic cell assignment, no search).
  2. **Also rigorous:** construct 4 km grid-cell polygons in the native LCC CRS and do a point-in-polygon join.
  3. **Acceptable fallback:** nearest-center lookup (`cKDTree` against grid centers) — equivalent to (1)/(2) at 4 km resolution but not exact for edge cases near a cell boundary.
  Once assigned, join the resulting `(lat_idx, lon_idx)` back to the CSV/Parquet reference table — don't keep re-deriving the assignment downstream.
- **Polygon data (counties, tracts, parcels, hazard zones):** reproject the polygons into the native CONUS404 LCC CRS, construct 4 km cell footprints, and use `within`/`intersects` or area-weighted overlay — not nearest-neighbor. Join the resulting `(lat_idx, lon_idx)` back to the reference table. Point-in-cell is sufficient for assigning a single value to a building/parcel centroid; use area-weighted overlay for regional summaries.
- **CSV-specific caveat:** CSV exports of this grid cannot store CRS metadata — EPSG:4326 must be assigned explicitly on load (e.g. when reading into GeoPandas), never assumed from file structure alone.
- **Data validity caveat:** many confidence-interval lower bounds in the current return-period data are strongly negative even where `converged = 1`. Preserve the raw bounds and `converged`/missing-value status through every join, but derive a separate physical/statistical validity flag. Never treat `converged` alone, or every numeric CI bound, as proof of a physically usable wind estimate.

Bottom line for Copilot: if both sides of a hazard join carry `lat_idx`/`lon_idx`, always use them. Only fall back to a spatial assignment method (native-grid reprojection > polygon overlay > nearest-center) when one side of the join is truly independent geography (e.g. a freshly uploaded asset portfolio) without grid indices.

### 6.3 H3 spatial indexing pipeline

H3 turns spatial joins into integer/string equality joins, which is the target end-state for all asset↔hazard joins:

1. **Precompute assets:** on upload, convert each asset's WGS84 point to an H3 index (fixed resolution, e.g. Res 7) and store it as a column on the portfolio Parquet file.
2. **Precompute hazard grids:** polyfill the 4 km wind grid cells with H3 indices once (a Res 7 hex is ≈5.16 km², so ~3–4 hexes cover each wind cell) and store the `h3_index → wind_grid_id` mapping as its own reference Parquet file. This mapping table is ID-only (no geometry) and belongs in `reference_data/`, per §6.5 — the H3 tessellation geometries used to *build* it belong in `reference_geo/` and are not read again after this precompute step.
3. **Join at simulation time:**
   ```sql
   SELECT ... FROM assets JOIN hazards ON assets.h3_index = hazards.h3_index;
   ```
   No spatial extension needed at this point — it's a plain join.

At Res 7, CONUS is ~1.5M hexagons — trivially small for DuckDB/Parquet to hold and query sub-second.

### 6.4 Precompute all spatial joins once (the Geographic Crosswalk Table)

Never perform point-in-polygon or nearest-neighbor operations inside a simulation run. During portfolio ingestion, build one **Geographic Crosswalk Table** mapping `asset_id` to every jurisdiction/geo key it needs — this table lives in `reference_data/`/`portfolios/{portfolio_id}/asset_features.parquet` and is exactly the mechanism that lets the engine stay geometry-free (§6.5):

```text
asset_id → county_fips, state_fips, tract_fips, zip_code, cbsa_code,
           climate_zone, wind_grid_id, nearest_material_store_id,
           labor_market_id, incentive_geo_id,
           property_value_geo_id, gdp_geo_id
```

This `asset_features` table is one of the most important tables in the system — it is what prevents the simulation from repeating expensive geospatial joins every run. `wind_grid_id` here should resolve to the CONUS404 `(lat_idx, lon_idx)` composite key per §6.2, not a re-derived nearest-neighbor lookup at simulation time. **Insurance fields (premium, deductible) are not part of this geo crosswalk** — they are direct user-supplied columns on the asset record itself, since V1 does not model insurance rules as a geo-resolved lookup.

Note the underlying data uses **inconsistent geo-indexing** across domains — be explicit about which key a given source uses rather than assuming FIPS everywhere:
- County-level FIPS is primary for GDP, employment, property values, and county-level incentives.
- State-level uses two-letter abbreviations or state FIPS (some GDP tables).
- Tract-level FIPS exists for property values but can be incomplete due to Census wildcard behavior.
- ZIP-level keys the incentives sampler.
- Some sources (disposal tipping fees) are keyed by **region/state text, not FIPS** — treat these as string-matched, not joined via a numeric key, and don't assume they'll `JOIN` cleanly.
- Non-geographic (national/sector-level) inputs — inflation, discount/mortgage rates, WACC, SC-GHG, EOL emission factors, cool-roof product data — have no geo index at all; don't force one.

### 6.5 Strict boundary: `reference_data/` (lookup-only) vs. `reference_geo/` (geometry-only)

This is the architectural line that keeps the simulation engine fast, reproducible, and free of geometry dependencies. Two directories, two audiences:

- **`reference_data/`** — optimized Parquet **lookup tables only**: ID-based, no geometry columns. Examples: `wind_grid_lookup.parquet`, `county_lookup.parquet`, `asset_crosswalk.parquet`, `fragility_curves.parquet`, `eul_rul_defaults.parquet`. This is what the simulation engine reads. Every row here is reachable by an equality join on an ID (`asset_id`, `wind_grid_id`, `county_fips`, `h3_index`, etc.) — never by a spatial predicate.
- **`reference_geo/`** — everything geometry-bearing: county/tract boundaries, wind grid cell polygons, H3 tessellations, any GeoParquet file with a `geometry` column. This directory is consumed **only** by preprocessing, ingestion, validation, and QA workflows — the scripts that build the crosswalks and lookup tables in the first place.

**Hard rule: the production simulation engine must never read from `reference_geo/` and must never perform a geometry operation — point-in-polygon, nearest-neighbor search, buffering, CRS transformation, or geometry intersection — at simulation time.** These operations belong exclusively to ingestion, preprocessing, validation, or exploratory analysis workflows — never to the production simulation engine. By the time a simulation run starts, every asset's spatial relationships have already been resolved once, during ingestion, into the geographic crosswalk table (§6.4) and the `reference_data/` lookup tables. The engine's only job at that point is equality joins against immutable, ID-keyed tables.

If a task seems to need a spatial operation *inside* the simulation engine, that's a signal the precomputation step upstream is incomplete — fix the crosswalk/ingestion pipeline, don't add a spatial join to the engine as a workaround.

`reference_geo/` files are still stored as **GeoParquet** (standard Parquet + standardized spatial metadata) rather than Shapefile/GeoJSON, so preprocessing tooling (DuckDB's spatial extension, GeoPandas) can read only the needed columns (e.g. `asset_id`, `geometry`) at high throughput when building the lookup tables that eventually land in `reference_data/`.

---

## 7. System Dynamics / Simulation Engine (Stock-Flow)

### 7.1 Locked architectural decisions

- **Granularity:** individual asset rows, not cohorts. Every asset is tracked individually, vectorized across *all* assets for a given year.
- **Compute engine:** Polars DataFrames for year-to-year transition math.
- **Loop structure:** a sequential **outer loop over years** (Python-level, required for state carryforward — RUL resets, resilience-tier changes, install-year resets all depend on the prior year's computed state) wrapping a fully **vectorized inner loop** across all assets for that year. This is restated from §3 because it is the single most load-bearing constraint in the codebase — never refactor it away.
- **Dimension history:** Type-2 SCD on `dim_asset` (§4.2).
- **Retrieval:** ID-chain only (§3, rule 5).
- **Orchestration:** Prefect (§7.5).
- **Outputs:** eight tables to Parquet (§7.4).

### 7.2 Annual simulation loop (canonical pseudocode)

```text
for year in analysis_years:                      # sequential — state carryforward
    assets_df = age_roof_by_one_year(assets_df)   # vectorized across all assets
    assets_df = update_rul(assets_df, eul_rul_defaults)
    hazard = retrieve_hazard_exposure(assets_df, wind_return_periods, wind_delta)
    vuln   = retrieve_vulnerability_curve(assets_df, fragility_curves)
    damage = estimate_expected_damage(hazard, vuln)
    loss   = convert_damage_to_loss(damage, damage_ratio_lookup)
    options = retrieve_feasible_upgrade_options(assets_df, resilience_tiers)
    costs  = calculate_costs(options, labor_costs, material_costs, complexity_multipliers)
    costs  = apply_incentives_and_discounts(costs, incentive_rules)
    econ   = calculate_economics(costs, loss, run_config)   # NPV, BCR, payback, avoided loss
    decisions = apply_decision_rule(econ, run_config.payback_threshold_years)
    assets_df = apply_decisions(assets_df, decisions)  # stay / retrofit / replace-on-burnout / upgrade
                                                        # — RUL and material reset here if upgraded/replaced
    write_annual_outputs(year, assets_df, decisions, econ, loss)
```

Every step inside a single `year` iteration operates on the whole `assets_df` at once — no nested per-asset Python loop.

### 7.3 Required simulation input tables

| Table | Purpose |
|---|---|
| `asset_features` | Prejoined portfolio table (output of the geo crosswalk, §6.3) |
| `roof_materials` | Canonical physical roof-covering taxonomy; never HAZUS building archetypes |
| `source_value_mappings` | Versioned source labels/aliases/shared buckets/model proxies → canonical IDs, with approval status |
| `resilience_tiers` | Resilience classification (Standard / Upgrade / Super Upgrade) |
| `eul_rul_defaults` | Replacement timing by material + climate zone |
| `wind_return_periods` | Current acute wind hazard |
| `wind_delta` | Future hazard scaling for CADENCE's single V1 climate trajectory |
| `fragility_curves` | Hazard → damage translation |
| `damage_ratio_lookup` | Damage → loss translation |
| `labor_costs` | Installation labor cost |
| `material_costs` | Installation material cost |
| `complexity_multipliers` | Roof geometry cost adjustment |
| `disposal_costs` | End-of-life cost |
| `emissions_factors` | End-of-life emissions |
| `scghg_values` | Carbon monetization |
| `energy_savings` | Optional benefit stream |
| `incentive_rules` | Grants, rebates, discounts |
| `regional_scalars` | GDP, property value, employment, construction capacity |
| `run_config` | User assumptions and toggles — the reproducibility anchor |

**Note:** there is no `insurance_rules` table in V1. Insurance premium and deductible are user-supplied fields already present on `asset_features` — the simulation reads them directly off the asset row rather than resolving them through a geo-keyed insurance lookup.

### 7.4 Required simulation output tables (eight, all Parquet)

| Table | Purpose |
|---|---|
| `annual_asset_state` | Year-by-year roof age, RUL, material, resilience tier, stock |
| `adoption_decisions` | Whether/why an asset upgraded or replaced |
| `annual_economic_results` | NPV, BCR, payback, avoided loss, CapEx |
| `annual_loss_results` | Expected loss, avoided loss, residual loss |
| `portfolio_summary_by_year` | Aggregate resilience, cost, loss, adoption over time |
| `portfolio_summary_by_geography` | Map-ready county/ZIP/grid/MSA summaries |
| `ranked_investment_plan` | Prioritized asset list by year and economic metric |
| `policy_comparison` | Base vs. adapted vs. incentivized outcomes, all evaluated under CADENCE's single V1 climate trajectory |

Store these **partitioned by run and year** (and upgrade option where applicable — see §8.5), not as one monolithic file. Streamlit should typically display these summary tables and maps, not full asset-year records.

### 7.5 Orchestration (Prefect)

**Prefect orchestrates workflows, not business logic.** It coordinates *when and in what order* things run: ingestion, preprocessing, simulation execution, output generation, and maintenance tasks (compaction, cache invalidation, etc.). It does not contain simulation math, decision rules, or cost/loss calculations itself — that logic lives entirely inside the Python/Polars simulation engine.

**The simulation engine must be callable independently of Prefect** — directly from a script, a unit test, or a notebook, with no Prefect runtime required. A Prefect flow is a thin wrapper that calls the engine's functions in sequence with the right inputs; it is not where you add a new business rule. If a Prefect task starts to contain simulation logic (a decision rule, a cost formula, a loss calculation), that's a sign the logic is in the wrong layer — move it into the engine.

The exact boundary between "ingestion pipeline" and "Prefect flow" — i.e. whether ingestion steps run as Prefect tasks from the start, or as a separate pipeline that Prefect only triggers — is still one of the open decisions in §12; don't hard-code an assumption about where that specific line sits. What's already settled is the *kind* of thing that goes in Prefect (coordination) versus the engine (logic) — don't blur that line while the ingestion boundary question is pending.

### 7.6 Material option construction and current V1 mappings

- The current canonical V1 material version contains **17 specific roof-covering options**. Keep every option in the option universe even when a source gap means it cannot yet be ranked.
- Generic uploaded values use explicit defaults: `asphalt → MAT_ASPHALT_ARCH`, `metal → MAT_METAL_CORRUGATED`, and `tile → MAT_TILE_CONCRETE`. Preserve the raw value and set `default_applied = true`; these defaults do not create global equivalence between subtypes.
- Build the annual option set as a vectorized asset × candidate-material cross join. Every current material may be compared with every specific candidate subtype; retain the same-material candidate as the replacement counterfactual.
- An option without an observed material price requires a user-supplied price override before cost-effectiveness ranking. Do not substitute a globally "nearest" priced material.
- For materials that do have observed prices, use the exact-material geography fallback `ZIP → CBSA → state → national → user override`. Every fallback step uses precomputed geography IDs and must be exposed in provenance.
- Ranking is a derived intermediate product by asset/location/year/run configuration. Enabled cost and benefit streams change BCA and ranking. Never encode that rank as a static resilience tier or material attribute.
- Burnout/RUL precedence is: user-supplied EUL, then canonical physical-material service life, then an explicit unresolved/error state. HAZUS proxy EUL never silently controls physical burnout.
- Approved physical/labor fallbacks are source-specific: premium architectural asphalt uses architectural mass/life; all three metal forms use generic-metal mass/life; Class 4 asphalt uses premium-architectural labor; wood shake/shingle requires user mass and EUL values. Emit a separate fallback/proxy flag for each applied source.
- If a recycling EOL factor is missing, use the same material's landfill factor and flag the pathway substitution. If that landfill factor is also missing (currently TPO), require a user EOL-factor override when the carbon stream is enabled.
- Active interim V1 fragility assumption: all asphalt subtypes use `WSF1`/wood-class curves. Preserve the asphalt material ID and emit `fragility_proxy_applied`, `fragility_proxy_id`, mapping version, and confidence/assumption fields in affected state/loss outputs.
- `Fiberglass Shingle Mat/Insulation` and `Gypsum Roof Cover Board` are ignored by current option and calculation logic. Preserve their raw source rows for provenance; do not expose them as replacement options.
- Active interim V1 fragility proxies are Asphalt -> `WSF1`, Metal -> `SERBL`, and Tile -> `MSF1`. Preserve the physical material class and emit `fragility_proxy_applied`, `fragility_proxy_id`, mapping version, and low-confidence assumption fields. `MERBL` is explicitly excluded from all tile-related analysis and must never be selected or used as a model input.
- Proxy curves are never averaged across materials or HAZUS building types. Within one approved proxy, climate zone, roof age, and terrain class, V1 equal-averages the HAZUS construction variants that current asset inputs cannot resolve. Retain all five `terrain_id` values as a lookup dimension and emit `curve_aggregation_method=equal_average_unresolved_variants_within_terrain`.
- The baseline Year 1 expected-damage lookup uses the six 3-second-gust point estimates at return periods 10, 25, 50, 100, 200, and 500 years. Convert m/s to mph, linearly interpolate `building_loss_ratio`, and clamp outside the source curve's wind range with QA counts. Integrate over annual exceedance probability by the trapezoid rule with provisional endpoints `(AEP=1, damage=0)` and `(AEP=0, damage=1)`; emit `tail_assumption=true`. Do not calculate expected damage as `sum(damage / return_period)`, which double-counts nested exceedances.
- The first Year 1 lookup is unscaled baseline hazard. Climate-delta scaling belongs to the subsequent annual-hazard phase. Publish ages 1-30; downstream asset ages above 30 use age 30 with `age_capped=true`, while missing or invalid user ages are rejected or quarantined.
- Production Year 1 damage is asset-scoped, not a precomputed full-grid material-age-terrain cube. During ingestion, map each unique user coordinate to the nearest CONUS404 center, assign its IECC climate zone, and deduplicate `(wind_grid_id, climate_zone, lookup_roof_age, terrain_id)` before calculating Asphalt, Metal, and Tile. Join the three results back to every asset sharing the key.
- Require a user HAZUS terrain label or numeric ID: `1 Open`, `2 Light Suburban`, `3 Suburban`, `4 Light Trees`, or `5 Trees`. Accept `terrain`/`Terrain` and `terrain_id`/`Terrain_numeric`; retain `Terrian` and `Terrian_numeric` as legacy ingestion aliases. If label and ID are both supplied, they must agree.
- Cache the expensive three-material calculation by unique calculation keys, selected gust values, selected fragility-partition identity, vulnerability/mapping version, and calculation-policy version. Use a separate asset-run key for portfolio-specific joined results so different asset IDs can reuse calculations without receiving another portfolio's rows.
- Reject coordinates that do not intersect a valid IECC climate-zone polygon. Do not silently assign an offshore or otherwise unresolved point to the nearest county or climate zone.

### 7.7 Implemented annual roof economics pipeline

The first standalone economics pipeline is implemented under `src/cadence/economics/`. It is a pre-simulation costing module, not yet the complete stock-flow economic decision engine shown in §7.2. It produces annual real-2026-dollar option costs for 2026-2050 without implementing NPV, BCR, payback, incentives, adoption decisions, burnout precedence, or asset-state transitions.

The implemented runtime contract is:

1. Input assets already carry `official_current_material_id`, `roof_area_sqft`, `replacement_value_usd`, ZIP, CBSA, state, county FIPS, `labor_market_id`, and labor-geometry fields. These geography IDs are precomputed upstream; the module never reads geometry.
2. The option grain is exactly one row per `(asset_id, year, official_material_id)` for `OFFICIAL_ASPHALT`, `OFFICIAL_METAL`, and `OFFICIAL_TILE`. The same-material row remains the current-roof replacement counterfactual.
3. Source material prices use exact-subtype `ZIP → CBSA → state → national` fallback before official-class consolidation. Asphalt and Metal retain partial-coverage flags; Tile remains `blocked_without_override` because it has no observed source price.
4. Labor uses all modeled occupations, fixed productivity, per-roof startup hours, and `H_MEDIAN_CONSTRAINED_PROJECTED_WAGE`. Runtime joins the exact precomputed BLS MSA/BOS `labor_market_id`; state/national fallback provenance was already applied when those projected area wages were built.
5. Full-installed 2026 $/sqft overrides are required for all three official classes. Each override supplies fixed material/labor shares summing to one for annual real-cost growth.
6. The asset's user `replacement_value_usd` has operational precedence for the current-roof option. Alternative options use their class installed override. Source-computed material + labor values remain separate audit estimates and never silently replace either operational value.
7. Disposal and landfill carbon use removed **current-roof** mass. Disposal uses state → EREF region → national fees and a 2,000-pound short ton. Carbon uses the configured annual SC-CO2 source series. Disposal and temporary-housing source values are fixed 2026-real proxies through 2050.
8. Expected loss of use is candidate-specific and remains separate from replacement CapEx. Provisional repair is `installed_capex_usd × expected_damage_ratio`; disposal, carbon, and loss of use are not prorated into repair.
9. Missing annual damage or downtime remains null with incomplete flags; it is never converted to zero.
10. Outputs are immutable under `schema_version=v0.1.0/run_id={hash}` with year-partitioned `annual_roof_option_costs`, `replacement_value_sanity_checks.parquet`, and `run_metadata.json`. Run identity includes assets, validated configuration, optional hazard economics, and all source checksums.

The CLI is `cadence-calculate-roof-economics`; the Python entry point is `cadence.economics.run_economics_pipeline`. See `docs/economics.md` for the complete operator and developer contract.

---

## 8. Caching & Performance for Streamlit at Scale

The whole reason this pipeline can serve a responsive Streamlit UI over up to hundreds of millions of rows is a strict caching discipline layered on top of a strict precomputation discipline. Both matter — precomputation reduces the *work*, caching reduces *repeated* work.

### 8.1 Three-tier caching model

1. **Reference data cache** — small, common lookup tables (`roof_materials`, `resilience_tiers`, run-configuration defaults, SC-GHG values, complexity multipliers) cached in Streamlit/DuckDB since they're small enough to hold fully in memory.
2. **Precomputed asset-feature cache** — once geospatial joins are done for a portfolio, cache `asset_features`. Never recompute unless the user uploads new assets or the geospatial lookup rules change.
3. **Run cache** — a full simulation run is cached and replayed by hash (see §8.3 and §9).

### 8.2 Streamlit cache mechanics

- `@st.cache_data` — serializable results: DataFrames, JSON API responses, DuckDB query results.
- `@st.cache_resource` — global resources: DB connections, loaded models.
- TTL example for anything hitting an external API (e.g. live benchmark material costs): `@st.cache_data(ttl="1d")` (24 hours / `ttl=86400`). CADENCE's cached items default to a 24-hour TTL unless a shorter one is specifically justified.
- **No shared caching across users currently** — Streamlit's cache is per-session/process; don't assume User A's cached run is visible to User B.
- **Invalidation:** if a user uploads a *new* portfolio, explicitly invalidate the cache for prior run results — don't rely on TTL alone for this case.

### 8.3 Run cache key (the hash)

A run is cache-eligible only if its full hash matches a prior run:

```text
run_hash = hash(
    portfolio_id, run_config, hazard_version, vulnerability_version,
    cost_database_version, incentive_version,
    simulation_code_version
)
```

If any one of these versions changes (e.g. a new `vulnerability_curve` release), the cache must miss and force a re-run — while the old run's outputs remain intact for audit purposes (§9).

### 8.4 Memoization for repeated sub-calculations

Use `functools.lru_cache` inside the simulation engine for calculations that recur identically across many assets — e.g. if 10,000 residential roofs in the same climate zone share a vulnerability curve and hazard exposure, the underlying loss calculation should compute once and be applied to all matching assets, not recomputed per asset.

### 8.5 Partitioned Parquet & predicate pushdown

Partition simulation outputs Hive-style by the columns Streamlit will filter on:

```text
/simulation_outputs/run_id=2026_07_A/upgrade=none/...
/simulation_outputs/run_id=2026_07_A/upgrade=tier1/...
```

When a query filters `WHERE upgrade = 'tier1'`, DuckDB skips the `upgrade=none` partition entirely, and within a partition it uses Parquet row-group min/max headers to skip non-matching row groups (predicate pushdown) — this is what keeps `SELECT year, sum(loss) FROM outputs WHERE year=2050` from ever materializing the full dataset at scale.

### 8.6 File compaction

Parquet performs best at **100MB–1GB per file**. If the simulation engine writes results incrementally (asset-by-asset or year-by-year), it will produce many small files ("small file problem") that slow DuckDB down more than the actual data volume would. Always include a **compaction step** after a run finishes: rewrite scattered output into cleanly sized, partitioned Parquet blocks before Streamlit reads them.

### 8.7 Memory profiling

Use `memory_profiler` or `memray` on the simulation engine during development, especially around the year loop — a leak here (e.g. holding onto prior years' full DataFrames instead of letting them go out of scope / explicit `del`) will show up as a staircase memory graph and eventually OOM.

---

## 9. Data Versioning & Provenance

Every run must be defensible after the fact — "why did this portfolio show a $5M loss by year 2050" must be answerable by reproducing the exact run.

### 9.1 Semantic versioning for datasets

- **MAJOR (v2.0.0):** incompatible changes (column renames, hazard grid resolution changes).
- **MINOR (v1.1.0):** backward-compatible additions (new column).
- **PATCH (v1.0.1):** backward-compatible fixes (typo correction).

Store versioned reference data in versioned directories, not a single overwritten file:

```text
/reference/costs/v1.0.0/costs.parquet
/reference/costs/v1.1.0/costs.parquet
```

Never delete or overwrite an old version when a new one is published — old prices/curves stay available for reproducing historical runs.

### 9.2 Checksums

Store a SHA-256 checksum for every input file referenced by a run. Parquet files are immutable (write-once), which makes them naturally suited to this.

### 9.3 Run metadata manifest (write alongside every simulation output)

Every simulation run must produce a manifest capturing:

```json
{
  "input_asset_file": "portfolio_acme_v2.parquet",
  "input_asset_checksum": "8f4e2...",
  "hazard_product_version": "CONUS404_wind_v1.2.geoparquet",
  "vulnerability_curve_version": "fragility_functions_v3.1.parquet",
  "cost_database_version": "material_costs_Q3_2025.parquet",
  "run_config": {"horizon": 2050, "upgrade": "status_quo", "payback_threshold_years": 7},
  "code_version": "Commit abc123def, engine v1.4.2",
  "run_timestamp": "2026-07-14T08:37:00Z",
  "run_id": "uuid-..."
}
```

Before computing, the engine performs a **provenance pre-flight check**: grab the current git commit, compute/retrieve checksums of every Parquet file about to be read, generate a `run_id`, then proceed. Write the manifest as `run_metadata_{run_id}.json` alongside the run's output directory.

---

## 10. Canonical Directory Layout

```text
cadence_datalake/
├── reference_data/                  # LOOKUP-ONLY — read by the simulation engine. No geometry columns.
│   ├── taxonomy/
│   │   ├── roof_materials.parquet
│   │   └── source_value_mappings.parquet
│   ├── climate/
│   │   ├── wind_return_periods.parquet
│   │   ├── wind_delta.parquet
│   │   └── wind_grid_lookup.parquet        # asset/cell → wind_grid_id, ID-keyed
│   ├── vulnerability/
│   │   ├── fragility_curves.parquet
│   │   └── eul_rul_defaults.parquet
│   ├── economics/
│   │   ├── material_costs.parquet
│   │   ├── labor_costs.parquet
│   │   ├── disposal_costs.parquet
│   │   └── scghg.parquet
│   ├── incentives/
│   │   └── incentives.parquet
│   └── geography/
│       ├── county_lookup.parquet
│       └── county_cbsa_zip_crosswalk.parquet
├── reference_geo/                   # GEOMETRY-ONLY — preprocessing/ingestion/validation/QA use only.
│   │                                 # The simulation engine must never read from this directory.
│   ├── climate/
│   │   └── wind_grid_cells.geoparquet      # source polygons used to BUILD wind_grid_lookup.parquet
│   └── geography/
│       ├── county_boundaries.geoparquet
│       ├── census_tracts.geoparquet
│       └── h3_tessellations.geoparquet
├── portfolios/
│   └── {portfolio_id}/
│       ├── raw_upload.csv
│       ├── assets_clean.parquet
│       ├── asset_features.parquet     # the geo crosswalk output, §6.4 — ID-only, no geometry
│       └── historical_portfolios/     # Type-2 SCD history
├── run_configs/
│   └── {config_id}/
│       └── run_config.json
├── runs/
│   └── {simulation_run_id}/
│       ├── run_metadata.json
│       ├── annual_asset_state.parquet
│       ├── adoption_decisions.parquet
│       ├── annual_economic_results.parquet
│       ├── annual_loss_results.parquet
│       ├── portfolio_summary_by_year.parquet
│       ├── portfolio_summary_by_geography.parquet
│       ├── ranked_investment_plan.parquet
│       └── policy_comparison.parquet
├── results/
│   └── roof_economics/
│       └── schema_version=v0.1.0/
│           └── run_id={economics_run_id}/
│               ├── annual_roof_option_costs/year={year}/part-00000.parquet
│               ├── replacement_value_sanity_checks.parquet
│               └── run_metadata.json
└── cache/
    └── run_hash_lookup.parquet
```

---

## 11. Coding Conventions & Copilot Behavior Rules

When generating CADENCE code, Copilot should:

- Default to **Polars** for any transformation touching the simulation state table; default to **DuckDB SQL** for reference-data retrieval/joins/aggregation feeding Streamlit.
- Never propose `pandas.iterrows()` or any row-wise Python iteration over assets — treat this as a hard error, not a style nitpick.
- Write **data contracts (Pydantic/Pandera models) alongside**, not after, any new table-producing function.
- When adding a new reference dataset, default to placing it under a versioned path (§9.1) rather than a flat unversioned file.
- When a new source introduces categorical labels, preserve those labels and add source-specific mappings (§4.6); never make raw text from different sources join directly.
- Never treat a physical roof material, HAZUS building archetype, resilience tier, upgrade option, or economic rank as interchangeable fields. Any bridge between them is an explicit versioned mapping/proxy.
- When writing simulation output, always partition by `run_id` (and `upgrade` option where relevant) and include a compaction step rather than writing many small files.
- Prefer **small, independently testable functions/phases** matching the 14-phase build roadmap already established for this project, over large multi-responsibility functions.
- When something touches an open decision (§12), stub it clearly (e.g. `# TODO(open-decision): see CADENCE instructions §12 — Prefect/ingestion boundary`) rather than guessing a resolution and moving on silently.
- Do not introduce Postgres/PostGIS, Redis, FastAPI, or a job queue into V1 code — those belong to the V1.5+ production architecture, not this phase.
- Do not model insurance as a rules/lookup table in V1 — treat premium and deductible as plain user-input columns on the asset record.
- Do not introduce multi-scenario (e.g. RCP) selection, comparison, or partitioning logic — V1 runs a single climate trajectory only.
- Never write simulation-engine code that reads from `reference_geo/` or calls a spatial function (point-in-polygon, `ST_*`, CRS reprojection). If a task seems to require this inside the engine, flag it — the fix belongs in the ingestion/crosswalk pipeline, not the simulation loop.

---

## 12. Open Architectural Decisions — Flag, Don't Assume

These are **unresolved** as of this writing. If a task requires touching one of these areas, implement the narrowest reasonable stub, comment it clearly, and call it out — do not pick a resolution on the project's behalf:

1. **Decision hierarchy conflict:** what happens when an asset is simultaneously eligible for burnout replacement *and* a voluntary upgrade in the same year? Precedence is undecided.
2. **Hazard interpolation method:** how to interpolate hazard values between published climate-delta reference years (e.g. between decadal wind-delta snapshots) is undecided.
3. **Prefect's ingestion boundary:** the *kind* of thing Prefect handles (workflow orchestration, not business logic) is settled — see §7.5. What's still undecided is exactly where in the raw-upload → cleaned-asset-table → `asset_features` pipeline Prefect's task boundaries start (i.e. whether ingestion runs as Prefect tasks from the first step, or as a separate pipeline that Prefect only triggers once assets are cleaned).

**Resolved since last revision (no longer open):**
- *Insurance scope* — insurance is **not** a rules/lookup table in V1. Premium and deductible are direct user inputs on the asset record.
- *Multi-scenario run structure* — moot for V1. CADENCE V1 runs a single climate scenario/trajectory; there is no scenario selection, comparison, or partitioning to design for.

---

## 13. Quick Reference Checklist

Before merging any CADENCE data/simulation code, verify:

- [ ] No per-asset Python `for` loop anywhere in the hot path
- [ ] Year loop is sequential; asset operations within a year are vectorized
- [ ] All spatial joins precomputed and ID-based at simulation time (no runtime point-in-polygon)
- [ ] Simulation engine code touches only `reference_data/` (lookup tables) — never `reference_geo/` or a spatial function
- [ ] Any new Prefect task contains orchestration only, no business logic — and the engine function it calls runs standalone (no Prefect required) for testing
- [ ] No code path edits a `reference_data/`/`reference_geo/` file in place, or patches an already-written run output — new version/new `run_id` instead
- [ ] New/changed tables have a Pydantic/Pandera contract
- [ ] Raw categorical labels are preserved and all controlled values resolve through a versioned source-aware mapping
- [ ] Physical material, fragility archetype, resilience tier, upgrade option, and economic rank remain separate
- [ ] No ambiguous, unapproved, or fuzzy mapping can reach simulation-time joins
- [ ] Any new reference dataset lives under a versioned path
- [ ] Simulation outputs are partitioned and compacted, not scattered small files
- [ ] A run metadata manifest is written alongside any new simulation output
- [ ] Streamlit reads go through `@st.cache_data`/`@st.cache_resource` with an appropriate TTL
- [ ] Run cache key includes every relevant data + code version, not just the config
- [ ] Any touched open decision (§12) is stubbed and flagged, not silently resolved
