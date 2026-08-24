# get-catchments-fast

A fast Python script for delineating catchment polygons from outlet points.

Generate catchment polygons for many outlet points at a fraction of the time
of conventional raster processing routines.

This script avoids large-scale raster processing by leveraging available
pre-computed vector topological catchment grids, like `HydroBASINS`.

For each outlet it derives coarse **upper** and **lower** bound approximations
of the catchment by topological means alone; only then, and only when the two
bounds disagree by more than a tolerance, does it fall back to raster tracing —
and even then only to fine-tune the outlet's immediate surroundings.

If you are in a hurry, the raster fine-tuning can be skipped entirely, leaving
you with the two polygon approximations and a measure of the error between them.

## How it works

Processing is **Pfafstetter-driven** and **fine-level-first**. The level ladder
must contain **exactly two levels**, ordered coarse → fine (e.g. HydroBASINS
lev06 then lev08).

The key idea is that a Pfafstetter code tells you *a priori* whether a basin is
a **headwater** (nothing else at that level drains into it) or a **trunk /
interbasin** unit (it receives flow from upstream). The rule: strip trailing
zeros from the code, and if the last remaining digit is **even** it's a
headwater. That single fact drives how much work each outlet needs.

1. **Fine level first.** Each outlet is located in the fine grid and its
   headwater status is determined (see *Headwater detection* below).

   - **Fine basin is a headwater** → the catchment is fully local to that fine
     sub-basin. The coarse level is never even queried.
     `catchment_upper` = the fine local basin alone; there is no
     `catchment_lower`.
   - **Fine basin is a trunk** → its upstream fine sub-basins are traced too,
     but **confined to the coarse parent's own territory** (via `search_depth`)
     so the trace never runs away up the wider river network. The coarse level
     is then checked as well:
     - **coarse headwater** → `catchment_upper` = fine local + fine upstream;
       `catchment_lower` = fine upstream only.
     - **coarse trunk** → `catchment_upper` = fine local + fine upstream +
       coarse upstream (the coarse *local* basin is deliberately excluded — fine
       local + fine upstream already cover that extent at finer resolution);
       `catchment_lower` = fine upstream + coarse upstream.

   `catchment_upper` is always an overestimate (or exact); `catchment_lower` is
   always an underestimate.

2. **Accept or refine.** If `catchment_lower` is within
   `catchment_acceptable_error_pct` of `catchment_upper`, the vector bracket is
   tight enough — `catchment_upper` becomes the final `catchment`, with no
   raster work at all. (For a fine headwater there is no lower bound to compare
   against, so refinement is attempted whenever a DEM is configured.)

3. **Raster fine-tuning** (only when the bracket is too loose, and only if a DEM
   is configured). Clips a small DEM extract around the outlet's **fine** local
   basin, delineates a terrain-following catchment (fill → D8 flow direction →
   accumulation → pour-point snap → trace → polygonize), and unions it with
   `catchment_lower` to produce the final `catchment`.

Every outlet is processed independently start to finish; one failing doesn't
stop the batch.

## Requirements

- Python 3.10+
- `geopandas`, `pandas`, `shapely`, `pyproj` (all pulled in by geopandas)
- `pysheds` (only needed for raster fine-tuning) — note: pysheds 0.5 calls the
  since-removed `numpy.in1d`; the script includes a compatibility shim for
  numpy ≥ 2.0
- `rasterio` (only needed for raster fine-tuning)
- `plans.geo.get_upstream_features_iterative` — internal dependency for the
  vector upstream trace

## Usage

```bash
python get_catchments_fast.py --spec spec.json
python get_catchments_fast.py --spec spec.json --outlet-id 3
python get_catchments_fast.py --spec spec.json --dry-run
```

`--dry-run` walks the whole decision logic and prints what would happen, without
writing or touching any files.

## spec.json reference

### Required

| Key | Description |
|---|---|
| `src_dir`, `dst_dir` | Input / output directories |
| `src_db` | GeoPackage filename (in `src_dir`) with outlets + grid layers |
| `outlets_layer`, `outlet_field` | Outlet points layer and its integer ID field |
| `id_field`, `id_down_field`, `pfaf_field` | HydroBASINS-style ID, downstream-ID, and Pfafstetter fields |
| `area_field` | Attribute area column (e.g. `SUB_AREA`) — mandatory, drives the accept/refine decision |
| `levels` | List of **exactly two** `{label, layer, search_depth}` entries, **coarsest first** |

`search_depth` on a level is the number of leading Pfafstetter digits used to
confine that level's upstream trace to the coarse parent (`null` = no
confinement). It is always applied when set, independent of headwater detection.

### Headwater detection & overrides

| Key | Default | Description |
|---|---|---|
| `headwater_detection` | `"trace"` | How a basin's headwater status is decided. `"trace"` (default): run the upstream trace and treat an empty result as headwater — authoritative, ignores Pfafstetter parity. `"pfaf"`: read it from the Pfafstetter code and skip the trace when it says headwater — fast, but only as reliable as the codes. `"auto"`: pfaf when a code is present, else trace |
| `force_headwaters` | `null` | List of integer outlet IDs forced to be treated as fine-level headwaters, overriding detection. For outlets where inspection showed the automatic result was wrong (e.g. a spurious network link). `null`/absent → none forced |
| `force_upper` | `null` | List of integer outlet IDs forced to the `catchment_upper` result, **skipping raster resolution entirely**. Useful for outlets in flat plains where DEM delineation mis-aligns. `null`/absent → none forced |

> **Why `"trace"` is the default.** Pfafstetter codes give the fastest possible
> headwater answer, but real-world grids contain coding errors and
> simplifications. The default therefore trusts the topological trace, not the
> parity of the code. Switch to `"pfaf"` when you have codes you trust and want
> the speed. Note that `pfaf_field` is mandatory either way — it also drives the
> `search_depth` confinement.

### Area & acceptance

| Key | Default | Description |
|---|---|---|
| `compute_area_crs` | `null` | Equal-area CRS (e.g. `"ESRI:102033"`) for a fresh geometric area. Recommended when using raster refinement — see *Area reporting* |
| `area_units` | `"km2"` | `"km2"` or `"m2"` |
| `catchment_acceptable_error_pct` | `5.0` | Max acceptable gap between lower/upper before raster refinement triggers |

### DEM (single file)

| Key | Default | Description |
|---|---|---|
| `dem_path` | `null` | Path to a DEM raster. Omit to skip raster refinement entirely (outlets that fail the tolerance just keep `catchment_upper`) |
| `is_dem_conditioned` | `false` | Skip pit-fill / depression-fill / flat-resolve if the DEM is already hydrologically conditioned (e.g. MERIT Hydro) |

### DEM (tiled)

Set `dem_path` to a **folder** and fill these in to activate tile mode: for each
outlet the intersecting tiles are found, merged, then clipped.

| Key | Description |
|---|---|
| `dem_tile_index_db`, `dem_tile_index_layer` | GeoPackage + layer with tile footprints |
| `dem_tile_field` | Field holding each tile's code |
| `dem_file_pattern` | e.g. `"dem_{tile}.tif"` (formatted with the tile code) |

### Raster refinement tuning

| Key | Default | Description |
|---|---|---|
| `dem_clip_buffer_pct` | `0.10` | Buffer around the fine local basin before clipping |
| `dem_clip_square` | `true` | Square the clip to its larger dimension — avoids truncating flow accumulation asymmetrically (can otherwise snap to a small creek instead of a nearby main river) |
| `dem_snap_search_cells` | `3` | Pour-point snap radius, in DEM cells (circular, resolution-independent) |
| `export_rasters` | `false` | Keep intermediate `ldd` / `acc` / catchment-mask rasters per outlet (for debugging). Merged tile mosaics are always deleted after clipping regardless of this setting |

### Output

| Key | Default | Description |
|---|---|---|
| `aggregated_output` | `"catchments.gpkg"` | Combined output filename, written to `dst_dir` |
| `discrete_output` | `null` | Optional filename; writes each outlet's final catchment to its **own** layer named `catchment_<outlet_id>` in a single GeoPackage. Handy for eyeballing one outlet at a time. `null`/absent → not written |

## Output

**`{dst_dir}/{aggregated_output}`** — one combined GeoPackage:

- `outlets` — one point per outlet, with `area`, `area_upper`, `area_lower`,
  `catchment_source`, and `n_polygons`.
- `catchment`, `catchment_upper`, `catchment_lower` — one polygon per outlet
  each, keyed by `outlet_field`. The `catchment` layer also carries
  `catchment_source` and `n_polygons`. `catchment_lower` is legitimately absent
  for headwater outlets.

**`{dst_dir}/{discrete_output}`** (only if `discrete_output` is set) — one
GeoPackage with a separate `catchment_<outlet_id>` layer per outlet's final
catchment. Outlets with no catchment simply get no layer.

**`{dst_dir}/outlets/outlet_{id}/`** — one folder per outlet, nested under a
single `outlets/` subfolder so `dst_dir` itself stays clean (just the aggregated
output plus that one subfolder). Kept for QA/debugging: per-level trace layers,
`outlet_{id}.gpkg` (full detail, all layers), the DEM clip, and — if
`export_rasters: true` — intermediate flow-direction / accumulation /
catchment-mask rasters.

### `catchment_source`

Every catchment is tagged so you can tell how much to trust it without
re-reading logs:

| Value | Meaning |
|---|---|
| `vector_upper` | Vector bracket was within tolerance; `catchment_upper` accepted as-is |
| `vector_upper_forced` | Outlet listed in `force_upper`; raster resolution deliberately skipped |
| `raster_refined` | DEM refinement ran; final catchment is the terrain-following result |
| `vector_upper_fallback_no_dem` | Tolerance not met but no `dem_path` configured |
| `vector_upper_fallback_dem_failed` | DEM clip / tile step raised an error |
| `vector_upper_fallback_raster_failed` | Raster delineation produced nothing usable |

### `n_polygons` (quality control)

A geometry QC flag on every catchment (and mirrored onto the `outlets` layer).
A clean catchment is a single contiguous polygon (`1`). **More than one part is
suspicious** — usually a mis-snapped pour point, a `search_depth` prefix that
missed part of the network, or a raster catchment truncated into islands at the
clip edge. Sort/filter for `n_polygons > 1` to review. Any such outlet also
prints a `QC WARNING` during the run.

### Area reporting

Area fields are always populated for outlets that have a catchment:

- **Vector catchments** report the fast attribute-column sum (`area_field`).
- **Raster-refined catchments** report a true **geometric** area measured on the
  merged polygon itself — because the raster piece carries geometry but no
  `area_field` value, so an attribute sum would be wrong or null. This is
  computed in `compute_area_crs` when set, otherwise **geodesically on the WGS84
  ellipsoid** (accurate anywhere, no CRS choice needed).

`area_lower` is legitimately `null` for headwater outlets (there is no lower
bound), and `area`/`area_upper` are `null` only for an outlet that produced no
catchment at all (its point fell outside the fine grid).

## Known limitations / notes

- `dem_clip_square` helps but doesn't fully eliminate accumulation truncation at
  clip edges — worth spot-checking outlets near large rivers or floodplains.
- Raster refinement uses a rectangular bbox clip, not an exact polygon mask.
- **Small/wrong-branch raster catchments.** The pour-point snap can occasionally
  lock onto a local tributary instead of the main channel, producing a small or
  disconnected catchment that gets unioned with `catchment_lower` into a wrong
  multi-polygon shape. This is now **surfaced automatically** via `n_polygons`
  (any value > 1 flags it for review) and can be **worked around** per-outlet
  with `force_upper` (skip raster, keep the vector upper bound) or
  `force_headwaters` (treat as fully local) once identified. It is detected and
  overridable, but not auto-corrected — the snap itself is unchanged.