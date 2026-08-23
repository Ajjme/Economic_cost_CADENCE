# CADENCE Data Consistency Review

Date: 2026-07-31  
Status: evidence review and mapping draft; no raw data changed

## Scope

This review covers:

- all five non-temporary workbooks in `Data_Catalogs/`;
- all four workbooks in `Data/User_Inputs/`;
- all 31 CSV files, seven non-fragility Parquet files, climate-zone shapefile metadata and DBF attributes, and the wind NetCDF header;
- the fragility build manifest, reports, and one authoritative sample of each Parquet family across the repeated 480 zone-age partitions;
- `CADENCE_copilot-instructions.md` as the controlling architecture.

The repository contains 1,505 files under `Data/`. Of the 1,447 Parquet files, 1,440 are the repeated fragility outputs: 16 climate zones x 30 ages x 3 files. The build manifest confirms all 480 partitions are generated.

## Mapping Contract

The draft mapping is in `master_mapping_reference_draft.csv`. It deliberately distinguishes four relationships:

1. `exact_bucket`: a physical material and source bucket have the same intended meaning.
2. `shared_bucket`: a source intentionally combines physical materials, such as one carbon factor for 3-tab and architectural asphalt.
3. `group_only`: a term such as `asphalt`, `metal`, or `tile` is not specific enough for downstream lookup.
4. `model_proxy`: a physical roof covering is assigned to a HAZUS building archetype for modeling. A proxy is never treated as a synonym.

Runtime lookup should use the source-aware key:

`(mapping_domain, source_dataset_id, source_column, normalized_source_value, mapping_version)`

The raw source value must be retained beside the canonical ID. Ambiguous, missing, or unapproved terms must be rejected or quarantined during ingestion, not fuzzily matched during simulation.

## Decisions Recorded During Review

1. All 17 specific roof coverings observed across the current sources remain in the canonical source taxonomy, but the V1 model exposes only three official options: Asphalt, Metal, and Tile. `official_material_class_map_v1.csv` defines class membership and exclusions; `material_consolidation_rules_v1.csv` defines domain-specific aggregation.
2. Generic `asphalt`, `metal`, and `tile` remain provenance placeholders and are not candidate replacement materials. At ingestion they default to architectural asphalt, corrugated metal, and concrete tile respectively, with the raw term retained and a `default_applied` flag.
3. Every asset is compared with the three official material classes. Physical subtypes remain available for source mapping and provenance but are consolidated before candidate ranking.
4. Candidate ranking is an intermediate derived product by location and run configuration. Enabled cost and benefit streams affect the BCA and therefore the ordering; a material does not have one global rank.
5. Burnout/RUL precedence rule: use the user-entered Fragility/EUL value first; if that value is blank, use physical-material service life from the data source; if both are missing, mark unresolved/error. HAZUS proxy EUL remains separate from physical burnout timing.
6. General asphalt EOL factors may serve both 3-tab and architectural asphalt in that carbon/disposal context only.
7. The interim fragility bridges are active for V1: Asphalt uses `WSF1`, Metal uses `SERBL` (Steel Engineered Residential Buildings, 1-2 Stories), and Tile uses `MSF1` (Single Family Homes, 1 Story - Masonry). Every affected result retains its physical material class and exposes the proxy ID, mapping version, low-confidence assumption status, and `fragility_proxy_applied=true`. `MERBL` remains excluded.
8. "Nearest currently available option" means a reviewed source- and context-specific fallback relationship. It never means fuzzy text matching or a global substitution across price, mass, labor, carbon, service life, and fragility.
9. Any in-scope option without an observed material price requires a user price override before ranking. Cross-family nearest-price substitution is not allowed.
10. Fiberglass shingle mat/insulation and gypsum roof cover board are ignored by current roof-option and calculation logic, but their source rows remain preserved for provenance.
11. Exact-material price lookup falls back ZIP -> CBSA -> state -> national -> user price override.
12. Premium architectural asphalt shares architectural asphalt mass and physical life. All three metal forms share generic-metal mass and physical life. Class 4 asphalt shares premium-architectural labor. Wood requires user mass and EUL values.
13. A missing recycling EOL factor uses the same material's landfill factor with a substitution flag. TPO requires a user EOL-factor override because both pathway factors are missing.
14. The baseline Year 1 roof-damage lookup uses 3-second-gust point estimates for RP10, RP25, RP50, RP100, RP200, and RP500; converts m/s to mph; and trapezoid-integrates building loss ratio over annual exceedance probability. Provisional endpoints are `(AEP=1, damage=0)` and `(AEP=0, damage=1)`, and every result carries `tail_assumption=true`. Climate-delta scaling is deferred to the next phase.
15. The lookup retains all five HAZUS terrain classes. It does not average across materials or proxy building types. Within one approved proxy, climate zone, roof age, and terrain, it equal-averages the construction variants that current asset inputs cannot resolve and records `curve_aggregation_method=equal_average_unresolved_variants_within_terrain`.
16. Production Year 1 damage is calculated only for unique keys present in the uploaded asset inventory, not materialized as a full wind-grid x age x terrain x material cube. Each key produces three material results that are joined back to its assets. Calculation-cache identity is separate from portfolio asset-result identity.
17. Asset ingestion requires HAZUS terrain as a label or code (`1 Open`, `2 Light Suburban`, `3 Suburban`, `4 Light Trees`, `5 Trees`). The test workbook now uses `Terrain` and `Terrain_numeric`; lowercase `terrain` and `terrain_id` are canonical equivalents, while `Terrian` and `Terrian_numeric` remain legacy aliases. Both current test rows resolve to `Open` / `1`. Label/code disagreement is an error.
18. Asset coordinates outside valid IECC polygons are rejected rather than assigned a guessed zone. The two current test coordinates at `32.994, -78.8986` are offshore and must be corrected before the test workbook can run end to end.

## Critical Findings

### 1. Roof material and fragility archetype are different dimensions

The physical material sources describe roof coverings, while the fragility files use HAZUS building archetypes:

| Source family | Exact current categories |
|---|---|
| User test input | `asphalt`, `metal` |
| Material price | 3 categories |
| Material escalation | 7 categories |
| Labor productivity | 11 categories |
| Material mass | 7 categories |
| Carbon EOL | 14 categories, including two components |
| Fragility / EUL | `MSF1`, `SERBL`, `WSF1`; classes `masonry`, `steel`, `wood` |

The vulnerability catalog explicitly proposes `wood -> asphalt`, `steel -> metal`, and `masonry -> tile`. Those are modeling proxies. They must not overwrite physical material identity used for price, mass, carbon, labor, or service life.

`MERBL` is ignored for all derived analysis, model selection, and reporting. `MSF1` is the approved interim masonry proxy for Tile; this model proxy does not overwrite physical tile identity.

### 2. EUL values conflict after the proposed proxies

`Data/Fragility_Curves/roof_eul_rul_defaults.csv` is internally complete and unique at 64 rows, but its IDs are the four HAZUS archetypes rather than roof-covering materials:

| Proposed proxy | Fragility/EUL value | Physical service-life source |
|---|---:|---:|
| Asphalt architectural -> `WSF1` | 10 years | 27.5 years |
| Asphalt Class 4 -> `WSF1` candidate | 10 years | 30 years |
| Metal -> `SERBL` | 30 years | 55 years |
| Clay tile -> masonry candidate | 24-25 years | 75 years |
| Concrete tile -> masonry candidate | 24-25 years | 45 years |

The project uses separate fields for `physical_service_life` and `fragility_age_model_id`, with a locked burnout/RUL precedence rule: user-entered Fragility/EUL first, then physical service life from the source data when the user value is blank, otherwise unresolved/error. A proxy-archetype EUL cannot silently replace material service life.

### 3. User inputs cannot currently select a price-ready subtype

- asphalt related roofing seen in subgroup option - 3-tab, architectural, premium architectural, or impact-resistant asphalt;
- all metal related roofing grouped into one option - corrugated panel, standing seam, metal tile/shingle, aluminum, or copper.

`asset_inventory_options.xlsx` contains no material options and no Excel data validations. It only lists some geometry values. The quick lookup must either require a subtype or apply a documented default. The draft marks generic material values `requires_detail`.

Additional input drift:

- The two user-input workbooks now carry validation rules and machine-readable controlled vocabulary sheets.

### 4. Material coverage is not symmetric

See `material_coverage_matrix.csv` for the source coverage matrix. The V1 product decision is now three official modeled classes, with equal-weight core members:

- Asphalt: 3-tab, architectural, and premium architectural.
- Metal: corrugated panel, standing seam, and metal tile/shingle form.
- Tile: clay and concrete.

`official_material_class_map_v1.csv` is the stable canonical-to-official map. `material_consolidation_rules_v1.csv` controls each data type's grouping grain, averaging method, missing-member policy, and blocked status. Current price consolidation is necessarily partial: asphalt has two observed members, metal has one, and tile requires a user class-price override. Fragility curves are not averaged across materials or proxy building types: Asphalt uses the active flagged `WSF1` proxy, Metal uses `SERBL`, and Tile uses `MSF1`. The Year 1 lookup separately equal-averages unresolved HAZUS construction variants within each selected proxy, climate zone, roof age, and retained terrain class. Presence in one source does not make a class simulation-ready.

### 5. Resilience tier semantics conflict

The vulnerability catalog names tiers `low`, `moderate`, and `elite`, then describes them as location-dependent cost-effectiveness rankings. Elsewhere the architecture uses examples such as Standard / Upgrade / Super Upgrade and states that material and resilience tier are separate dimensions.

Physical resilience, upgrade option, and economic ranking are not interchangeable:

- resilience should describe construction/performance attributes;
- upgrade option should describe the modeled intervention;
- BCR/payback rank should be a derived result by location and run configuration.

No `resilience_tiers` reference table currently exists.

## Source Findings

### Climate and hazard

1. The wind CSV and climate-delta Parquet each contain 1,387,505 unique `(lat_idx, lon_idx)` rows, covering indices `0..1014` x `0..1366`. Their keysets match exactly. 
4. The NetCDF contains six return periods (10, 25, 50, 100, 200, 500). Its dimensions match the CSV, and its native/3-second variables are in m/s.

7. The climate-delta file has annual columns only through 2050. Runs extending beyond 2050 will scale at the average rate of 2040-2050
8. The raw climate-zone shapefile is NAD83, while the ingestion contract requires WGS84 output. Reproject during preprocessing and leave the raw file unchanged.
9. The climate-zone DBF stores zone number and moisture regime separately (`IECC21`, `Moisture21`). They must be combined to obtain values such as `4A`.


### Vulnerability and loss of use

1. Fragility coverage is internally complete: 16 zones x ages 1-30, four building archetypes, nine damage types, and no missing source response points in the reports.


4. Ages above 30 are undefined even though physical service-life values extend to 112.5 years. we will default to 30 being the max for most of this analysis as we really care about the covering not the structure
5. Damage responses do not share one unit. Loss ratios, debris quantities, and loss-of-use days must not be stored in a generic `loss_ratio` column without a response-unit dimension.
6. `representative_loss_of_use_curve_points.csv` is a QA sample only: three zones and five ages. It is not a complete production lookup.
7. Each approved proxy contains many construction variants rather than one curve per climate zone and age. In a representative age-1 partition, each terrain contains 616 `WSF1`, 96 `SERBL`, and 624 `MSF1` building-loss curves. V1 retains terrain and equal-averages the remaining unresolved variants; this is an explicit low-confidence modeling assumption, not subtype prevalence weighting.
8. The six return levels are nested exceedance quantiles. A simple sum of `damage / return_period` would double-count exceedances and is prohibited. The approved first-pass method integrates the interpolated building loss ratios over annual exceedance probability with flagged provisional tails.

### Materials, labor, carbon, and disposal

1. The material price folder is now `Start_Year_2026`, consistent with the 2026 real-dollar economics baseline; every price row retains its 2026-07-31 scrape date.
2. Each local price table has 14 rather than 15 expected geography-material combinations. Texas / CBSA 26420 / ZIP 77001 lacks corrugated metal.
3. All price `store_count` values are zero and all bulk-price fields are blank. Product counts are only one to four.
4. The economic catalog describes product/store/county-level price columns, while the current files are sparse aggregate tables with different schemas.
5. Material mass is internally consistent: `weight_per_square_lbs = 100 * weight_per_sqft_lbs` within floating-point tolerance.
6. Carbon EOL has one missing landfill factor (TPO) and five missing recycling factors (clay tile, wood, TPO, PVC, gypsum cover board). These are text `NA` values in numeric columns.
7. `Fiberglass Shingle Mat/Insulation` and `Gypsum Roof Cover Board` are components, not primary roof types. They cannot be mapped to a roof covering without an assembly rule.
8. Disposal has 51 rows: 44 direct state rows, six custom region rows, and one national row. Connecticut, Delaware, District of Columbia, Hawaii, Massachusetts, Rhode Island, and Vermont have no direct row and require a documented regional fallback.
9. Labor productivity forms a complete 7,920-row Cartesian lookup across 11 roof types, three shapes, four deck attachments, two wall connections, three size buckets, and ten occupations. Occupation shares sum to 1.0 for every scenario.
10. Every labor productivity row is `ASSUMPTION_V1`, `low` confidence, and `provisional`; this must propagate to output provenance.
11. Labor geography coverage is 484 matched, 47 geometry-only, and 46 wage-only records. Catalog paths now point to `Start_Year_2026`; the economics runtime uses constrained 2026-2050 median projected wages and requires an exact precomputed MSA/BOS `AREA` key.
12. The employment catalog describes private NAICS 2361 only. The actual file includes ownership codes 2, 3, and 5 and NAICS 2361, 23816, 238161, and 238162.
13. The annual economics pipeline is implemented under `src/cadence/economics`. It preserves source-computed, class-override, and user-replacement valuation tracks; emits three official-class options per asset-year; and writes immutable year-partitioned results with source checksums.
14. The implemented EOL pathway is landfill-only. Disposal uses current-roof mass with state -> EREF region -> national fallback, and carbon uses landfill factors plus a run-config-selected SC-CO2 series.
15. Tear-off labor is not represented by the current productivity source. Economics outputs flag it as excluded; tipping fees must not be described as complete removal cost.

### Geography and global inputs

1. Geographic keys are heterogeneous and must remain strings: county FIPS, five-digit CBSA, seven-character BLS `AREA`, ZIP, HUD FIPS, prefixed climate `GEOID`, state names, and custom disposal regions.
2. The required county/CBSA/BLS-area/HUD/climate/store crosswalk is not present. The economics runtime therefore requires these IDs on its precomputed asset-feature input and fails unresolved labor-market IDs rather than performing runtime geography work.
3. `labor_geographies.parquet` contains geometry and belongs in preprocessing/reference-geo use, not simulation-time reference-data reads.
4. Property values are county-only despite catalog language saying tract where available.
5. GDP per-capita values are marked estimated on every row.
6. Discount rates contain six maturities and both real and nominal values, but no locked selection rule.
7. Mortgage data contains both 15-year and 30-year fixed loans; selection logic is not defined.
8. SC-GHG has three discount-rate variants. The economics run config now requires/selects `1.5`, `2.0`, or `2.5` for annual CO2 monetization; this is a source-series selection, not the future BCA cash-flow discount rate.

## Catalog and Instruction Drift

| Area | Current inconsistency | Required action |
|---|---|---|
| Wind grid | Instructions have the wrong row count | Correct to 1,387,505 |
| Climate delta | Catalog filename differs from disk | Point catalog to `wind_climate_scaling.parquet` |
| Climate scenario | Catalog/output language implies multiple climate scenarios | Use one trajectory in V1; reserve policy/counterfactual labels for non-climate comparisons |
| Insurance | Catalog mentions an NAIC benchmark fallback; instructions require direct user inputs | Remove automatic insurance lookup fallback or explicitly revise the locked V1 scope |
| Material taxonomy | Required `roof_materials` table is absent | Promote an approved version of the draft mapping and canonical material dimension |
| Fragility taxonomy | HAZUS building archetype is called roof material | Separate physical material, building archetype, and proxy mapping |
| Resilience tier | Vocabulary and meaning conflict | Define static physical tier separately from derived economic rank |
| Required inputs | `damage_ratio_lookup`, `complexity_multipliers`, `energy_savings`, `incentive_rules`, and geographic crosswalk are absent | The standalone economics pipeline accepts optional annual damage/downtime but these tables remain required before full simulation-engine integration |
| Outputs | The implemented economics costing outputs are intermediate pre-simulation products, not the eight final stock-flow outputs | Preserve `annual_roof_option_costs` and sanity checks as inputs/audit artifacts; map them into the locked outputs when the full engine is implemented |
| Catalog IDs | `S-010` is used twice in the user-input catalog | Assign unique IDs |
| Catalog schema | Vulnerability workbook has duplicate `Notes` headers; intermediate workbook has stray `Column1` | Clean catalog schemas |
| Input spelling | `deductable_type` differs from `deductible_type` | Canonicalize to `deductible_type` with a temporary ingestion alias |

## Review Sequence

The safest collaborative order is:

1. **Asset material vocabulary:** generic defaults, the 17-option universe, and user-price override policy are settled; proposed lexical aliases still require review as new sources appear.
2. **Fragility and EUL:** Asphalt -> `WSF1`, Metal -> `SERBL`, and Tile -> `MSF1` are active, flagged interim proxies; validate or replace them scientifically in a later version. Physical service-life precedence is already settled, and `MERBL` remains excluded.
3. **Price, escalation, labor, and mass:** the V1 annual costing pipeline and official-class rules are implemented; calibrate provisional labor and replace partial material-price coverage.
4. **Carbon and disposal:** landfill-only class factors and state-to-region fallback are implemented; validate the EREF region assignments and add recycling only through a new approved policy/version.
5. **Roof geometry and occupations:** map display labels to labor codes and fix delimited user-input parsing.
6. **Climate and geography:** build the county/grid/CBSA/BLS/HUD/climate crosswalk and physical-validity flags.
7. **Global inputs and outputs:** lock rate-selection policies and align catalog names with instruction contracts.

## Decisions Not Made in This Draft

This review does not silently decide:

- eventual scientific replacement or validation of the active interim Asphalt-to-`WSF1`, Metal-to-`SERBL`, and Tile-to-`MSF1` proxies;
- scientific replacement of equal weighting across unresolved HAZUS construction variants and geographic assignment of terrain class;
- replacement or validation of the provisional AEP tail endpoints once lower-return-period hazard data or fitted GEV parameters are available;
- scientific validation of provisional shared carbon buckets;
- external validation of the implemented state-to-EREF-region assignments;
- resilience-tier names or ranking semantics;
- discount-rate, WACC-industry, and mortgage-term selection for future BCA (SC-CO2 source-series selection is implemented separately);
- post-2050 climate scaling.

Those remain explicit review items in the mapping table through `mapping_status`, `confidence`, and `decision_required`.