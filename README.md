# get-catchments-fast

A fast Python script for delineating catchment polygons from outlet points.

Generate catchment polygons for many outlet points at a fraction of the time
of conventional raster processing routines.

This script avoids large-scale raster processing by leveraging available
pre-computed vector topological catchment grids, like `HydroBASINS`.

The script retrieves a coarse upper and lower bound approximation of an
outlet's catchment only by topological means; only then does it perform
raster tracing, and only for fine-tuning the outlet's immediate surroundings.

If you are in a hurry, the fine-tuning can be skipped entirely, leaving
you with two polygon approximations and a measure of the error between them.

## How it works

1. **Vector bounds.** For each outlet, trace upstream through one or more
   HydroBASINS-style grid levels (coarse → fine). This gives two bracketing
   estimates:
   - `catchment_lower` — union of everything strictly upstream (local basin
     excluded). Always an underestimate.
   - `catchment_upper` — the coarsest level's local basin + its own
     upstream. Always an overestimate (or exact).
2. **Accept or refine.** If `catchment_lower` is within
   `catchment_acceptable_error_pct` of `catchment_upper`, that's good
   enough — `catchment_upper` becomes the final `catchment`, no raster work
   at all.
3. **Raster fine-tuning** (only when the vector bracket is too loose, and
   only if a DEM is configured). Clips a small DEM extract around the
   outlet's local basin, delineates a terrain-following catchment (fill →
   D8 flow direction → accumulation → pour-point snap → trace →
   polygonize), and unions it with `catchment_lower` to produce the final
   `catchment`.

Every outlet is processed independently start to finish; one failing
doesn't stop the batch.

## Requirements

- Python 3.10+
- `geopandas`, `pandas`, `shapely`
- `pysheds` (only needed if using raster fine-tuning) — note: pysheds 0.5
  calls the since-removed `numpy.in1d`; the script includes a compatibility
  shim for numpy ≥ 2.0
- `rasterio` (only needed if using raster fine-tuning)
- `plans.geo.get_upstream_features_iterative` — internal dependency for the
  vector upstream trace

## Usage

```bash
python get_catchments_fast.py --spec spec.json
python get_catchments_fast.py --spec spec.json --outlet-id 3
python get_catchments_fast.py --spec spec.json --dry-run
```

`--dry-run` walks the whole decision logic and prints what would happen,
without writing or touching any files.

## spec.json reference

**Required**

| Key | Description |
|---|---|
| `src_dir`, `dst_dir` | Input / output directories |
| `src_db` | GeoPackage filename (in `src_dir`) with outlets + grid layers |
| `outlets_layer`, `outlet_field` | Outlet points layer and its ID field |
| `id_field`, `id_down_field`, `pfaf_field` | HydroBASINS-style ID, downstream-ID, and Pfafstetter fields |
| `area_field` | Attribute area column (e.g. `SUB_AREA`) — mandatory, drives the accept/refine decision |
| `levels` | List of `{label, layer, search_depth}`, coarsest first |

**Area & acceptance**

| Key | Default | Description |
|---|---|---|
| `compute_area_crs` | `null` | Equal-area CRS for a fresh geometric area check (recommended if using raster refinement) |
| `area_units` | `"km2"` | `"km2"` or `"m2"` |
| `catchment_acceptable_error_pct` | `5.0` | Max acceptable gap between lower/upper before raster refinement triggers |

**DEM (single file)**

| Key | Default | Description |
|---|---|---|
| `dem_path` | `null` | Path to a DEM raster. Omit to skip raster refinement entirely |
| `is_dem_conditioned` | `false` | Skip pit-fill/depression-fill/flat-resolve if the DEM is already conditioned |

**DEM (tiled)** — set `dem_path` to a folder and fill these in to activate tile mode

| Key | Description |
|---|---|
| `dem_tile_index_db`, `dem_tile_index_layer` | GeoPackage + layer with tile footprints |
| `dem_tile_field` | Field holding each tile's code |
| `dem_file_pattern` | e.g. `"dem_{tile}.tif"` |

**Raster refinement tuning**

| Key | Default | Description |
|---|---|---|
| `dem_clip_buffer_pct` | `0.10` | Buffer around the local basin before clipping |
| `dem_clip_square` | `true` | Square the clip to its larger dimension — avoids truncating flow accumulation asymmetrically (can otherwise snap to a small creek instead of a nearby main river) |
| `dem_snap_search_cells` | `3` | Pour-point snap radius, in DEM cells (circular, resolution-independent) |
| `export_rasters` | `false` | Keep intermediate `ldd`/`acc`/catchment-mask rasters per outlet (for debugging). Merged tile mosaics are always deleted after clipping regardless of this setting |

**Output**

| Key | Default | Description |
|---|---|---|
| `aggregated_output` | `"catchments.gpkg"` | Combined output filename, written to `dst_dir` |

## Output

**`{dst_dir}/{aggregated_output}`** — one combined GeoPackage:
- `outlets` — one point per outlet, with `area`, `area_upper`, `area_lower`,
  and `catchment_source`
- `catchment`, `catchment_upper`, `catchment_lower` — one polygon per
  outlet each, keyed by `outlet_field`. `catchment_lower` is legitimately
  absent for headwater outlets.

`catchment_source` values: `vector_upper` (accepted as-is), `raster_refined`,
or a `vector_upper_fallback_*` variant (no DEM configured, refinement
failed, etc.) — always present, so you can tell how much to trust a given
outlet's result without re-reading logs.

**`{dst_dir}/outlet_{id}/`** — one folder per outlet, kept for QA/debugging:
per-level trace layers, `outlet_{id}.gpkg` (full detail, all layers),
DEM clip, and (if `export_rasters: true`) intermediate flow-direction /
accumulation / catchment-mask rasters.

## Known limitations / notes

- `dem_clip_square` helps but doesn't fully eliminate accumulation
  truncation at clip edges — worth spot-checking outlets near large rivers
  or floodplains.
- Raster refinement uses a rectangular bbox clip, not an exact polygon
  mask.
- **Small/wrong-branch raster catchments.** The pour-point snap can
  occasionally lock onto a local tributary instead of the main channel,
  producing a small catchment.
  Right now this gets unioned with `catchment_lower` (upstream catchment) regardless, which can
  produce a wrong multi-polygon final shape rather than failing loudly. Not yet fixed.
  Likely fix: use the PFAFSTETTER code to infer whether the local
  basin at the finer level is a genuine headwater/fully-draining unit or an intermediate
  branch, and use that to sanity-check before merging it in. Not implemented yet — flagged here for later.
- **Output folder clutter.** Per-outlet folders (`outlet_1/`, `outlet_2/`,
  ...) currently sit directly in `dst_dir`, alongside the aggregated
  `catchments.gpkg` — with many outlets this buries the actual deliverable
  among a large number of per-outlet folders. Likely fix: nest all
  per-outlet folders under an intermediate subfolder (e.g.
  `dst_dir/outlets/outlet_1/`), leaving `dst_dir` itself holding just the
  aggregated output plus that one subfolder. Not implemented yet — flagged
  here for later.