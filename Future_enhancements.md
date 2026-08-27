# Future Enhancements

## Vulnerability

Before production BCA, the vulnerability model needs:

- A scientifically approved treatment below the 50 mph fragility threshold.
- A better extreme-tail model using fitted GEV parameters or additional return-period data.
- Confirmation that HAZUS curve wind convention matches the CONUS404 10-meter, open-terrain, 3-second gust convention.
- Continued enforcement of the wind unit standard: return-period values are converted to mph when attached, and fragility interpolation remains in mph thereafter.
- Annual 2026-2050 expected damage and expected loss-of-use outputs at `(asset_id, year, official_material_id)` grain for complete economics repair and downtime estimates.

## Economics

The implemented annual costing pipeline is documented in `docs/economics.md`. Before it becomes the full economic decision engine, add:

- Calibrated installation productivity to replace low-confidence `ASSUMPTION_V1` labor parameters.
- A tear-off/removal labor source or explicit user override. Current disposal cost includes tipping fees but not removal labor.
- Observed subtype prices for premium architectural asphalt, standing seam, metal tile, clay tile, and concrete tile.
- Replace the county-level GDP-per-capita price adjustment ratio in `material_price_expansion.py` with BEA Regional Price Parities (`RPP_GOODS`, LineCode=2 from BEA SARPP). RPP_GOODS directly measures local goods price levels relative to the national average (100 = national), eliminating the spurious correlation between income and projected material cost. Implementation plan: (1) download `bea_rpp_goods.csv` (geo_level, geo_code, year, rpp_goods) for states + MSAs; (2) derive county→CBSA from existing `zcta_dimension.parquet` (dominant CBSA by ZCTA count); (3) replace `load_county_gdp` with `load_county_rpp` that resolves MSA-level RPP first, state-level fallback for non-CBSA counties; (4) swap ratio computation in `_project_material` from `target_gdp / source_gdp` to `target_rpp / source_rpp`; (5) update default ratio bounds to [0.70, 1.45] to match the tighter natural range of RPP_GOODS (~85–130 nationally). No downstream pipeline changes required — the renamed output columns (`rpp_year`, `target_rpp_goods`, etc.) are internal to the expanded CSV.
- Repair-specific material and labor rules by damage state; the current repair result is installed cost multiplied by expected damage ratio.
- Validated recycling pathways and factors published as a new mapping/data version. The current pipeline is landfill-only.
- Asset bedroom count or another approved temporary-housing selection policy instead of the fixed two-bedroom assumption.
- Annual real-growth policies for disposal and temporary housing if fixed 2026-real proxies are no longer acceptable.
- Complexity multipliers, energy savings, incentives, and any other approved cost/benefit streams.
- Real cash-flow discount-rate selection, NPV, BCR, payback, and avoided-loss calculations. The existing SC-CO2 rate only selects a published carbon-value series.
- Decision rules, ranked investment plans, and portfolio summaries.
- Integration with the sequential stock-flow engine after the burnout-versus-voluntary-upgrade precedence decision is resolved.

## Ingestion And Operations

- Extend the published national ZCTA-to-county/CBSA/BLS dimension with HUD area, climate zone, and wind-grid identifiers when those domains enter the economics workflow.
- Add economics-ready asset and global-input workbook templates or an ingestion adapter that emits the documented CSV/Parquet and JSON contracts.
- Publish normalized economics reference tables under versioned `cadence_datalake/reference_data/economics/` paths rather than reading raw source folders for every run.
- Add Prefect orchestration after the open raw-upload-to-asset-features boundary is settled; keep all economics formulas callable without Prefect.