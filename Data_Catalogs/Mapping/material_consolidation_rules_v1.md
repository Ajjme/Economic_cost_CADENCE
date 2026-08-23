# V1 Official Material Consolidation Rules

Version: `v1.0.0`

## Purpose

CADENCE V1 exposes exactly three official modeled material classes: `OFFICIAL_ASPHALT`, `OFFICIAL_METAL`, and `OFFICIAL_TILE`. Consolidation does not erase physical subtype identity in raw or cleaned source data. It creates a derived class-level reference value after source labels have been resolved to canonical IDs.

The runtime chain is:

```text
raw source value
  -> source-aware canonical mapping
  -> canonical_id
  -> official_material_class_map_v1.csv
  -> domain rule in material_consolidation_rules_v1.csv
  -> versioned consolidated reference row
```

No raw string is matched directly to an official class, and no fuzzy matching is allowed.

## Core Membership

| Official class | V1 average members |
|---|---|
| Asphalt | 3-tab, architectural, premium architectural |
| Metal | corrugated panel, standing seam, metal tile/shingle form |
| Tile | clay, concrete |

Impact-resistant Class 4, aluminum, and copper retain their official family identity but do not contribute to the V1 core averages. Generic asphalt, metal, and tile are input placeholders: resolve them through their approved defaults before lookup, but never count the placeholder as another average member. Slate, wood, BUR/modified bitumen, and single-ply membranes are rejected or quarantined because coercing them into one of the three classes would change their physical meaning. Components are always excluded.

## Evaluation Contract

1. Resolve every source value through `master_mapping_reference_draft.csv` using the full source-aware key. Reject or quarantine unresolved and unapproved mappings.
2. Join the resulting `canonical_id` to `official_material_class_map_v1.csv` with the requested `mapping_version`.
3. Keep only `aggregation_eligible=true` members for class-level reference construction.
4. Apply approved source-specific shared buckets, proxies, and fallback values before aggregation. Preserve every applied mapping and fallback flag.
5. Group by the keys in the selected domain rule. Never average across geography, year, labor scenario, occupation, EOL pathway, damage type, climate zone, or roof age.
6. Apply the named aggregation and missing-value policy. `arithmetic_mean_available_members` skips unavailable core members but emits their IDs in `missing_member_ids`; `arithmetic_mean_resolved_members` requires a source value or approved member-level fallback for every core member.
7. Emit the rule version, source dataset versions, contributing canonical IDs, missing canonical IDs, member count, expected member count, mapping/proxy flags, and output status with every consolidated row.
8. Fail a simulation-time lookup when a rule is `blocked` or its empty-class policy is not satisfied. Do not invent a cross-family substitute.

The arithmetic mean is equal-weighted by core subtype, not weighted by product count or source-row count. A repeated source bucket may resolve multiple members, but source row duplication must never create extra members.

## Important Domain Behavior

- Price geography fallback runs before consolidation: ZIP, CBSA, state, national, then class-level user override. Current observed data yields an asphalt mean of 3-tab and architectural, a metal value from corrugated alone, and no tile price. These partial contributor sets must be flagged.
- Labor is averaged only after matching all scenario and occupation keys. Derived labor columns are recomputed from the consolidated base parameter and the unchanged scenario factors; they are not independently averaged.
- Mass per square is the primary measure. Derive mass per square foot as `weight_per_square_lbs / 100` after consolidation.
- Physical service life remains separate from fragility-archetype EUL. User-entered EUL retains asset-level precedence over the consolidated physical default.
- Carbon landfill and recycling are separate rules. Apply an approved same-material pathway fallback before class averaging and carry the substitution flag.
- Fragility curves are never averaged across physical materials or HAZUS proxy building types. Each official class selects one approved interim proxy: Asphalt uses `WSF1`, Metal uses `SERBL`, and Tile uses `MSF1`. Within one selected proxy, climate zone, roof age, and retained terrain class, V1 equal-averages HAZUS construction variants that current asset inputs cannot resolve. Preserve the physical class and emit the proxy ID, mapping version, low confidence, `fragility_proxy_applied=true`, and `curve_aggregation_method=equal_average_unresolved_variants_within_terrain`; `MERBL` remains excluded.

## Required Validation

A published rule version must have unique canonical IDs in the class map, exactly three nonblank official class IDs, one rule per `(rule_version, data_domain, official_class_id)`, valid defaults, no component or placeholder marked aggregation-eligible, and complete coverage of the canonical material dimension. Consolidated outputs must report contributor coverage so a partial average cannot appear complete.

## Implemented Economics Usage

The annual economics pipeline in `src/cadence/economics` applies these rules for material price, material escalation, labor productivity, material mass, and landfill carbon. Its execution order is:

```text
source label -> canonical ID -> official core membership
             -> source-specific fallback/shared value
             -> class consolidation -> annual asset-option cost
```

Material-price geography fallback occurs before consolidation. The pipeline preserves contributing and missing canonical IDs, and it leaves Tile source pricing null with `blocked_without_override`. Operational option costs use required full-installed class overrides; source-consolidated values remain an audit track and do not conceal incomplete coverage.

See `docs/economics.md` for input contracts, formulas, output columns, and CLI/Python usage.