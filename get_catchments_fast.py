"""
Spec-driven upstream basin delineation.

Given a GeoPackage of outlet points and one or more HydroBASINS-style
grid layers (a "level ladder", e.g. level1 -> level2 -> ...), traces the
upstream contributing area for each outlet at each level, exports the
results, and combines the dissolved upstream polygons across all levels
into a single "full upstream" layer per outlet.

Each outlet is processed as one self-contained pipeline before moving to
the next outlet. Outlets don't depend on one another, so there is no
level-wide batching step; grid layers are simply loaded once up front and
reused.

This script requires levels to contain EXACTLY 2 entries: [coarsest,
finest]. Processing is Pfafstetter-driven and fine-level-first:

- The FINE level is always sjoined/traced first. Whether its local basin
  is a headwater is decided per the headwater_detection spec option:
  "trace" (default) runs the upstream trace and treats an empty result as
  headwater (authoritative); "pfaf" reads it a priori from the PFAF code
  parity via is_headwater() and skips the trace when it says headwater
  (fast, but only as reliable as the codes). Either way the search_depth
  PFAF-prefix filter still confines the trace to the coarse parent. An
  outlet listed in the force_headwaters spec option is forced to headwater
  regardless of detection (a manual override for cases where inspection
  showed the automatic result was wrong).
- If the fine level is a HEADWATER: the outlet's catchment is fully local
  to that fine sub-basin. No other fine sub-basin feeds it, and - by the
  same containment logic - no coarse-level upstream basin can reach it
  either, since any such contribution would have to pass through some
  fine sub-basin upstream of it, and there isn't one. The coarse level is
  never even queried. catchment_upper = the fine local basin alone; there
  is no catchment_lower to bracket it with, so DEM refinement (when a
  dem_path is configured) is always attempted rather than gated by the
  usual tolerance check.
- If the fine level is a TRUNK basin: its upstream fine sub-basins are
  traced too - but confined to the coarse parent's own territory (via
  search_depth), never all the way up the wider river network. The coarse
  level is then also checked:
    - coarse HEADWATER: nothing more to add. catchment_upper = fine local
      + fine upstream (dissolved). catchment_lower = fine upstream
      (dissolved) alone.
    - coarse TRUNK: the coarse level's own upstream (dissolved) is folded
      in too. catchment_upper = fine local + fine upstream (dissolved) +
      coarse upstream (dissolved) - note the coarse LOCAL basin itself is
      deliberately NOT included, since fine local + fine upstream already
      cover that same extent at finer resolution. catchment_lower = fine
      upstream (dissolved) + coarse upstream (dissolved) only (no local
      basin at either scale).
  The usual acceptable-error tolerance then decides whether DEM
  refinement is needed, same as before.
- Whenever DEM refinement runs, the FINE level's local basin is always the
  clip extent - raster resolution happens at fine scale in every case.
- Two manual per-outlet overrides (by outlet id) bypass the automatic
  decisions above, for outlets where inspection showed the automatic
  result was wrong: force_headwaters forces an outlet to be treated as a
  fine-level headwater; force_upper forces catchment_upper as the final
  result and skips raster resolution entirely (useful in flat plains where
  DEM delineation mis-aligns).

Usage:

    python get_catchments_fast.py --spec spec.json
    python get_catchments_fast.py --spec spec.json --outlet-id 3
    python get_catchments_fast.py --spec spec.json --dry-run

Spec file (JSON). Required keys are marked; everything else is optional
and falls back to the default shown. 'levels' must contain EXACTLY 2
entries, ordered [coarsest, finest]:

    {
        "src_dir":        "data/in",              // required
        "dst_dir":        "data/out",             // required
        "src_db":         "basins.gpkg",          // required (filename in src_dir)

        "outlets_layer":  "outlets",              // required
        "outlet_field":   "outlet_id",            // required (unique id per outlet point)

        "id_field":       "HYBAS_ID",             // required (basin id)
        "id_down_field":  "NEXT_DOWN",            // required (downstream basin id)
        "pfaf_field":     "PFAF_ID",              // required (Pfafstetter code)
        "area_field":     "SUB_AREA",             // required (per-basin area attribute)

        "levels": [                               // required, EXACTLY 2, [coarse, fine]
            {"label": "lev06", "layer": "hybas_lev06", "search_depth": null},
            {"label": "lev08", "layer": "hybas_lev08", "search_depth": 6}
        ],

        // ---- optional: area handling ----
        "compute_area_crs":               null,   // e.g. "ESRI:102033" for a fresh geometric area
        "area_units":                     "km2",  // "km2" | "m2"
        "catchment_acceptable_error_pct": 5.0,    // accept vector upper if lower >= 95% of it
        "headwater_detection":            "trace",// "trace" (default, authoritative) |
                                                  //   "pfaf" (fast, trusts PFAF parity) | "auto"
        "force_headwaters":               null,   // e.g. [3, 17, 42] - outlet ids forced to
                                                  //   headwater regardless of detection.
                                                  //   null/absent -> none forced.
        "force_upper":                    null,   // e.g. [8, 21] - outlet ids forced to the
                                                  //   upper approximation, skipping raster
                                                  //   resolution. null/absent -> none forced.

        // ---- optional: outputs (both written to dst_dir) ----
        "aggregated_output": "catchments.gpkg",   // shared layers, keyed by outlet_field
        "discrete_output":   null,                // e.g. "database.gpkg" -> one
                                                  //   catchment_<outlet_id> layer per outlet.
                                                  //   null/absent -> not written.

        // ---- optional: DEM refinement (single-file mode) ----
        // omit dem_path entirely to disable raster refinement (outlets that
        // fail the tolerance check just fall back to catchment_upper).
        "dem_path":            null,              // one DEM raster, OR a folder (tiled mode below)
        "dem_clip_buffer_pct": 0.10,              // buffer around fine local basin before clipping
        "dem_clip_square":     true,              // square up the clip bbox (see compute_clip_bounds)
        "dem_snap_search_cells": 3,               // pour-point snap radius, in DEM cells
        "is_dem_conditioned":  false,             // true skips fill/resolve (pre-conditioned DEMs)
        "export_rasters":      false,             // true keeps intermediate ldd/acc/mask rasters

        // ---- optional: DEM refinement (tiled mode) ----
        // set dem_tile_index_db to switch dem_path to a FOLDER of tiles; then
        // dem_tile_index_layer / dem_tile_field / dem_file_pattern are ALSO required.
        "dem_tile_index_db":    null,             // gpkg holding the tile footprints
        "dem_tile_index_layer": null,             // layer name within that gpkg
        "dem_tile_field":       null,             // column holding each tile's code
        "dem_file_pattern":     null              // e.g. "{tile}_dem.tif" (formatted with tile code)
    }

Note: the block above is annotated with // comments for readability;
strict JSON does not allow comments, so a real spec.json must omit them.

"""
import argparse
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pysheds.io
from shapely.geometry import Polygon, MultiPolygon

from plans.geo import get_upstream_features_iterative


# --------------------------------------------------------------------------
# Spec loading
# --------------------------------------------------------------------------

def load_spec(spec_path: Path) -> dict:
    """Load and lightly validate the JSON spec file."""
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)

    required_top = [
        "src_dir", "dst_dir", "src_db",
        "outlets_layer", "outlet_field",
        "id_field", "id_down_field", "pfaf_field",
        "levels",
    ]
    missing = [k for k in required_top if k not in spec]
    if missing:
        raise ValueError(f"Spec is missing required key(s): {missing}")

    # area_field is MANDATORY: the accept-vector-approximation-or-refine-with-DEM
    # decision needs an actual area comparison between catchment_lower and
    # catchment_upper, and there's no reliable fallback (compute_area_crs alone
    # would mean recomputing geometry on every dissolve just to make this call,
    # which isn't the "fast" path this script is for).
    if not spec.get("area_field"):
        raise ValueError(
            "area_field is required - it's used to decide whether the vector "
            "approximation (catchment_upper) is already good enough, or whether "
            "DEM-based refinement is needed."
        )

    # Optional: an equal-area CRS (e.g. "ESRI:102033" for South America Albers
    # Equal Area Conic). When set, a fresh geometric area is also computed on
    # every dissolved polygon, in the units below. Note: a raster-refined
    # catchment ALWAYS gets a geometric area regardless of this setting (its
    # attribute area is untrustworthy) - this CRS is used for it when set,
    # otherwise that area is measured geodesically on the WGS84 ellipsoid.
    spec.setdefault("compute_area_crs", None)
    spec.setdefault("area_units", "km2")
    if spec["area_units"] not in ("km2", "m2"):
        raise ValueError("area_units must be 'km2' or 'm2'")

    # Maximum acceptable gap between catchment_lower and catchment_upper, as a
    # percent (e.g. 5.0 means: if catchment_lower's area is at least 95% of
    # catchment_upper's, the vector-only upper approximation is accepted as the
    # final "catchment" outright, and DEM refinement is skipped entirely (even
    # if a dem_path is configured) - that's the whole point of "fast": only pay
    # for raster processing on outlets where the vector bracket is too loose.
    spec.setdefault("catchment_acceptable_error_pct", 5.0)

    # How to decide whether a basin is a headwater (no upstream) at a given
    # level. Pfafstetter codes give an a priori answer without tracing, but
    # can be unreliable where the PFAF system has errors/simplifications -
    # so the default is the slower-but-authoritative trace check.
    #   "trace" (default): always run the upstream trace and treat an empty
    #       result as headwater. Ignores PFAF parity for this decision even
    #       though codes are present. The authoritative path.
    #   "pfaf": use is_headwater() on the outlet's PFAF code and SKIP the
    #       upstream trace entirely when it reports headwater - the fast,
    #       "lazy" path, only as trustworthy as the PFAF codes themselves.
    #   "auto": use PFAF if a pfaf_field is available, else fall back to
    #       trace. Since pfaf_field is currently mandatory (it also drives
    #       search_depth), this is effectively identical to "pfaf" today;
    #       kept for forward-compatibility if pfaf_field ever becomes
    #       optional.
    # Note: PFAF codes are still used for the search_depth prefix filter
    # regardless of this setting - this only governs headwater detection.
    spec.setdefault("headwater_detection", "trace")
    if spec["headwater_detection"] is None:
        spec["headwater_detection"] = "trace"
    if spec["headwater_detection"] not in ("trace", "pfaf", "auto"):
        raise ValueError(
            "headwater_detection must be 'trace', 'pfaf', or 'auto' - "
            f"got {spec['headwater_detection']!r}"
        )

    # Optional: a hand-curated list of outlet ids (integers) FORCED to be
    # treated as headwater catchments, overriding whatever headwater_detection
    # would otherwise conclude for them. Intended for outlets where visual
    # inspection of earlier runs showed the automatic trace/pfaf result was
    # wrong - e.g. a spurious downstream link in the basin network pulling in
    # area that isn't really upstream. A forced outlet skips its fine-level
    # upstream trace entirely and takes the fine local basin as
    # catchment_upper, exactly like a naturally-detected headwater (so the
    # coarse level is never queried, there is no lower bound, and raster
    # refinement runs whenever a DEM is configured). Absent or null -> no
    # outlet is forced.
    spec.setdefault("force_headwaters", None)
    if spec["force_headwaters"] is None:
        spec["force_headwaters"] = []
    try:
        spec["force_headwaters"] = [int(v) for v in spec["force_headwaters"]]
    except (TypeError, ValueError) as e:
        raise ValueError(
            "force_headwaters must be a list of integer outlet ids - "
            f"got {spec['force_headwaters']!r}"
        ) from e

    # Optional: a hand-curated list of outlet ids (integers) whose result is
    # FORCED to be the vector upper approximation (catchment_upper), skipping
    # raster resolution entirely. Intended for outlets - typically in flat
    # plains - where DEM-based delineation is known to mis-align (the pour
    # point won't snap to the right channel, the catchment leaks across a
    # near-flat divide, etc.) and the vector upper bound is the more
    # trustworthy answer. Overrides both the tolerance check and DEM
    # availability. Absent or null -> no outlet is forced.
    spec.setdefault("force_upper", None)
    if spec["force_upper"] is None:
        spec["force_upper"] = []
    try:
        spec["force_upper"] = [int(v) for v in spec["force_upper"]]
    except (TypeError, ValueError) as e:
        raise ValueError(
            "force_upper must be a list of integer outlet ids - "
            f"got {spec['force_upper']!r}"
        ) from e

    # Filename (not full path) for the combined output GeoPackage, written
    # to dst_dir once all outlets are processed. Collects every outlet's
    # final catchment/catchment_upper/catchment_lower into single layers,
    # plus an 'outlets' layer with area/area_upper/area_lower attributes -
    # a single-file summary instead of hunting through per-outlet folders.
    spec.setdefault("aggregated_output", "catchments.gpkg")

    # Optional filename (not full path) for a "discrete" output GeoPackage,
    # written to dst_dir alongside aggregated_output. Instead of one shared
    # 'catchment' layer keyed by outlet id, this writes each outlet's FINAL
    # catchment to its own layer named 'catchment_<outlet_id>' - handy for
    # eyeballing/QA one outlet at a time in GIS without them stacked in a
    # single layer. Only the final catchment goes here (not the
    # upper/lower brackets); outlets that produced no catchment get no
    # layer. Null/absent -> not written at all.
    spec.setdefault("discrete_output", None)

    # Optional: path to an EXISTING DEM raster (e.g. a MERIT Hydro tile).
    # Only used if the vector approximation isn't within
    # catchment_acceptable_error_pct - when needed, each outlet gets a small
    # DEM clip around its finest-level local basin, from which a raster-
    # derived catchment is delineated and becomes the final "catchment".
    # Left null/omitted, refinement is never available - outlets that fail
    # the tolerance check just fall back to catchment_upper.
    #
    # dem_path has two modes:
    #   - Single file (legacy): dem_path points directly at one DEM raster.
    #   - Tiled (when dem_tile_index_db is also set): dem_path is a FOLDER
    #     of tile files. For each outlet, the tiles intersecting its clip
    #     extent are found via dem_tile_index_db/layer, merged into one
    #     raster, and THAT gets clipped - same clip_dem_to_bbox as before,
    #     just fed a freshly-merged mosaic instead of a single static file.
    spec.setdefault("dem_path", None)
    spec.setdefault("dem_tile_index_db", None)
    spec.setdefault("dem_tile_index_layer", None)
    spec.setdefault("dem_tile_field", None)
    spec.setdefault("dem_file_pattern", None)

    dem_tiled_mode = spec["dem_tile_index_db"] is not None
    if dem_tiled_mode:
        required_tile_keys = ("dem_tile_index_layer", "dem_tile_field", "dem_file_pattern")
        missing_tile_keys = [k for k in required_tile_keys if not spec.get(k)]
        if missing_tile_keys:
            raise ValueError(
                f"dem_tile_index_db is set, so these are also required: {missing_tile_keys}"
            )

    # Bounding-box buffer applied around the finest level's local basin
    # before clipping, as a fraction of that basin's width/height (0.10 = 10%).
    spec.setdefault("dem_clip_buffer_pct", 0.10)

    # Square up the (buffered) clip bbox to its larger dimension, centered
    # on the same point, before clipping/tile-selection. Default true - an
    # elongated clip truncates flow accumulation asymmetrically, which can
    # make the pour-point snap lock onto a small nearby creek instead of
    # the actual main river (notably for outlets in a floodplain). Set
    # false to restore the old unsquared rectangular-bbox behavior.
    spec.setdefault("dem_clip_square", True)
    if spec["dem_clip_square"] is None:
        spec["dem_clip_square"] = True

    # Pour-point snap search radius, in DEM CELL UNITS (not a real-world
    # distance) - spec-driven so it's easy to tune per DEM, but stays
    # resolution-independent since it's expressed in cells, not meters.
    spec.setdefault("dem_snap_search_cells", 3)

    # Set true if the DEM is already hydrologically conditioned (e.g. MERIT
    # Hydro's own pre-conditioned elevation product) - skips fill_pits/
    # fill_depressions/resolve_flats entirely, saving real time across many
    # outlets. Default false (also if explicitly null) - conditions the DEM
    # as normal, which is the safe default for a plain/raw elevation raster.
    spec.setdefault("is_dem_conditioned", False)
    if spec["is_dem_conditioned"] is None:
        spec["is_dem_conditioned"] = False

    # Set true to also write the intermediate flow-direction and flow-
    # accumulation rasters (and the raw catchment mask) to disk per outlet,
    # alongside the DEM clip - useful for QA/debugging a specific outlet's
    # raster delineation. Off by default: these are only intermediate
    # products, most runs don't need them kept around. Default false (also
    # if explicitly null).
    spec.setdefault("export_rasters", False)
    if spec["export_rasters"] is None:
        spec["export_rasters"] = False

    for i, lvl in enumerate(spec["levels"]):
        for k in ("label", "layer"):
            if k not in lvl:
                raise ValueError(f"levels[{i}] is missing required key '{k}'")
        lvl.setdefault("search_depth", None)

    # The fine-then-coarse Pfafstetter branching in process_outlet is defined
    # strictly in terms of "the coarse level" and "the fine level" - it does
    # not generalize to a longer ladder. Fail fast at spec-load time rather
    # than letting an N-level spec silently run with undefined behavior.
    if len(spec["levels"]) != 2:
        raise ValueError(
            "'levels' must contain exactly 2 entries - [coarsest, finest] - "
            f"for this script's Pfafstetter fine-then-coarse logic. Got "
            f"{len(spec['levels'])}: {[lvl.get('label') for lvl in spec['levels']]}"
        )

    return spec


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def load_grid(gpkg_path: Path, layer: str, id_field: str,
              id_down_field: str, pfaf_field: str,
              area_field: str = None) -> gpd.GeoDataFrame:
    """Read a basin grid layer, keep only the needed columns, cast PFAF_ID to str."""
    cols = [id_field, id_down_field, pfaf_field, "geometry"]
    if area_field:
        cols.insert(-1, area_field)
    gdf = gpd.read_file(gpkg_path, layer=layer)[cols].copy()
    gdf[pfaf_field] = gdf[pfaf_field].astype(str)
    return gdf


def normalize_to_point_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Some GIS writers (certain shapefile/GeoPackage export paths) store a
    single point as a MultiPoint with one member instead of a plain Point.
    Downstream code (e.g. derive_raster_catchment's pt.x/pt.y) assumes a
    plain Point, so flatten any single-member MultiPoint here, once, at the
    source - rather than defensively checking geometry type everywhere a
    point gets used later.

    Raises ValueError if any row is a MultiPoint with more than one member -
    that's genuinely ambiguous (no single "the" outlet to pick).
    """
    def _flatten(geom):
        if geom is not None and geom.geom_type == "MultiPoint":
            pts = list(geom.geoms)
            if len(pts) != 1:
                raise ValueError(
                    f"outlet geometry is a MultiPoint with {len(pts)} points - "
                    f"expected exactly one point per outlet"
                )
            return pts[0]
        return geom

    gdf = gdf.copy()
    gdf["geometry"] = gdf["geometry"].apply(_flatten)
    return gdf


def load_outlets(gpkg_path: Path, layer: str, outlet_field: str = None,
                  outlet_id=None) -> gpd.GeoDataFrame:
    """Read the outlet points layer, optionally filtered to a single outlet id."""
    gdf = gpd.read_file(gpkg_path, layer=layer)
    gdf = normalize_to_point_geometry(gdf)
    if outlet_id is not None:
        # outlet_id arrives as a str from --outlet-id on the CLI, but
        # outlet_field is typically an int column - cast so the query
        # actually matches instead of silently returning nothing.
        int_outlet_id = int(outlet_id)
        gdf = gdf.query(f"{outlet_field} == @int_outlet_id").copy()
        if gdf.empty:
            raise ValueError(f"No outlet found with {outlet_field} == {outlet_id}")
    return gdf


# --------------------------------------------------------------------------
# Pfafstetter logic
# --------------------------------------------------------------------------

def is_headwater(pfaf_code: str) -> bool:
    """
    True if pfaf_code is a Pfafstetter headwater/tributary basin.

    Rule: the first non-zero digit counting from the right is even.
    Trailing zeros are unused finer-level digits (no subdivision occurred
    under this basin), not meaningful for parity, so they're stripped
    first. A headwater basin has, by construction, no other basin at this
    same level flowing into it - so its upstream is guaranteed empty
    without needing to trace anything.

    Raises ValueError if pfaf_code is all zeros (no non-zero digit at all -
    not a valid Pfafstetter code to classify).
    """
    stripped = pfaf_code.rstrip("0")
    if not stripped:
        raise ValueError(f"pfaf_code is all zeros: {pfaf_code!r}")
    return int(stripped[-1]) % 2 == 0


# --------------------------------------------------------------------------
# Geometry / area handling
# --------------------------------------------------------------------------

def remove_holes(geometry):
    """
    Strip interior rings from a Polygon/MultiPolygon.

    grid.polygonize() on a raster catchment mask can produce spurious tiny
    holes (isolated nodata/excluded cells fully surrounded by catchment
    cells) - not real gaps in the catchment, just rasterization noise. Drop
    them for a clean solid polygon.
    """
    if geometry.geom_type == "Polygon":
        return Polygon(geometry.exterior)
    elif geometry.geom_type == "MultiPolygon":
        return MultiPolygon([Polygon(g.exterior) for g in geometry.geoms])
    return geometry


def count_polygons(geometry) -> int:
    """
    Count the number of disjoint polygon parts in a geometry.

    A clean catchment should be a single contiguous polygon (returns 1).
    More than one part is a quality-control red flag: it usually means the
    dissolve left disconnected pieces - e.g. a mis-snapped pour point that
    delineated an unrelated basin, a search_depth prefix that missed part
    of the true upstream network, or a raster catchment truncated into
    islands at the DEM clip edge. Surfaced as the 'n_polygons' attribute so
    suspicious outlets can be filtered/reviewed after a run.

    Returns 0 for a None or empty geometry.
    """
    if geometry is None or geometry.is_empty:
        return 0
    gt = geometry.geom_type
    if gt == "Polygon":
        return 1
    if gt == "MultiPolygon":
        return len(geometry.geoms)
    if gt == "GeometryCollection":
        return sum(count_polygons(g) for g in geometry.geoms)
    return 0


def dissolve_with_area(
        gdf: gpd.GeoDataFrame,
        area_field: str = None,
        compute_area_crs: str = None,
        area_units: str = "km2",
) -> gpd.GeoDataFrame:
    """
    Dissolve a GeoDataFrame into a single feature, preserving area info.

    Always dissolves via an explicit 'by' key rather than relying on
    dissolve(by=None) treating the whole frame as one group - that default
    isn't consistent across geopandas versions, so a constant grouping
    column is added and dropped instead.

    - If area_field is given and present on gdf, the dissolved feature's
      value is the SUM of that column across the input rows (aggfunc
      defaults to 'first' for every other column, matching plain dissolve).
    - If compute_area_crs is given, a fresh geometric area is computed on
      the dissolved polygon in that CRS and stored as
      'area_computed_km2' / 'area_computed_m2'.
    """
    gdf = gdf.copy()
    gdf["_dissolve_key"] = 0

    other_cols = [c for c in gdf.columns if c not in (gdf.geometry.name, "_dissolve_key")]
    aggfunc = {c: "first" for c in other_cols}
    if area_field and area_field in gdf.columns:
        aggfunc[area_field] = "sum"

    if aggfunc:
        gdf_dissolved = gdf.dissolve(by="_dissolve_key", aggfunc=aggfunc).reset_index(drop=True)
    else:
        # No non-geometry columns at all (e.g. a purely raster-derived piece
        # with no attributes) - groupby().agg({}) raises "No objects to
        # concatenate", so skip the dict form and just dissolve geometry.
        gdf_dissolved = gdf.dissolve(by="_dissolve_key").reset_index(drop=True)

    if compute_area_crs:
        area_m2 = gdf_dissolved.geometry.to_crs(compute_area_crs).area
        gdf_dissolved[f"area_computed_{area_units}"] = (
            area_m2 / 1e6 if area_units == "km2" else area_m2
        ).values

    return gdf_dissolved


def get_area_value(gdf: gpd.GeoDataFrame, area_field: str = None, area_units: str = "km2"):
    """
    Pull a single numeric area value off a one-row (dissolved) GeoDataFrame.

    Prefers the summed attribute column (area_field) if present, falls back
    to the computed geometric column, and returns None if neither exists.
    """
    if area_field and area_field in gdf.columns:
        return float(gdf[area_field].iloc[0])
    computed_col = f"area_computed_{area_units}"
    if computed_col in gdf.columns:
        return float(gdf[computed_col].iloc[0])
    return None


def compute_geometric_area(gdf: gpd.GeoDataFrame, area_units: str = "km2",
                           area_crs: str = None) -> float:
    """
    Geometric area of a GeoDataFrame's polygon(s), summed, in km2 or m2.

    This is the authoritative area for geometry that has no reliable
    attribute area to sum - notably a raster-refined catchment, whose
    raster piece carries geometry but no area_field value, so an attribute
    sum silently omits it (or is absent entirely).

    If area_crs is given, area is measured after reprojecting to it - use an
    equal-area CRS (e.g. "ESRI:102033") for a meaningful number. If not,
    area is measured geodesically on the WGS84 ellipsoid, which needs no
    CRS choice and is accurate anywhere; the geometry is reprojected to
    EPSG:4326 first when its own CRS is projected. A geographic CRS (or a
    missing CRS, assumed lon/lat) is fed to the geodesic calc directly.
    """
    if area_crs:
        area_m2 = float(gdf.geometry.to_crs(area_crs).area.sum())
    else:
        from pyproj import Geod
        geoms = gdf.geometry
        if gdf.crs is not None and gdf.crs.is_projected:
            geoms = geoms.to_crs("EPSG:4326")
        geod = Geod(ellps="WGS84")
        area_m2 = float(sum(abs(geod.geometry_area_perimeter(g)[0]) for g in geoms))
    return area_m2 / 1e6 if area_units == "km2" else area_m2


def reattach_fields(
        gdf_result: gpd.GeoDataFrame,
        source_gdf: gpd.GeoDataFrame,
        field_id: str,
        fields: list[str],
) -> gpd.GeoDataFrame:
    """
    Left-join attribute columns from source_gdf back onto gdf_result, keyed by field_id.

    get_upstream_features_iterative() only preserves what it needs to trace
    the network (id / down-id / geometry) and drops everything else - so any
    extra attribute (e.g. area) that survived on the original grid has to be
    re-joined back on afterwards. No-op for any field already present or
    missing from the source.
    """
    fields = [f for f in fields if f and f not in gdf_result.columns and f in source_gdf.columns]
    if not fields:
        return gdf_result
    lookup = source_gdf[[field_id] + fields].drop_duplicates(subset=field_id)
    return gdf_result.merge(lookup, on=field_id, how="left")


class DEMExtentError(Exception):
    """Raised when a requested DEM clip extent has no overlap with the source DEM."""


def _bounds_overlap(a: tuple, b: tuple) -> bool:
    """a, b: (minx, miny, maxx, maxy) tuples. True if they overlap at all."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def compute_clip_bounds(
        polygon_gdf: gpd.GeoDataFrame,
        buffer_pct: float,
        target_crs=None,
        square: bool = True,
) -> tuple:
    """
    Compute the final clip bounds for a polygon: buffer first, then
    (optionally) square up to the LARGER of the two buffered dimensions,
    centered on the same center point.

    Squaring matters for the raster refinement step specifically: an
    elongated (non-square) clip extent truncates flow accumulation
    asymmetrically. A real river running near one edge of a thin
    rectangular clip loses most of its true upstream contributing area -
    whatever lies outside the clip in the box's "short" direction - so its
    computed accumulation comes out artificially low relative to a small,
    fully-contained tributary nearby. That can make the pour-point snap
    lock onto the small creek instead of the actual main river, especially
    for outlets sitting in a floodplain near a big river. A square clip
    gives more balanced context in both directions, which reduces (though
    doesn't fully eliminate) that edge-truncation bias.
    """
    gdf = polygon_gdf.to_crs(target_crs) if target_crs is not None else polygon_gdf
    minx, miny, maxx, maxy = gdf.total_bounds

    width = maxx - minx
    height = maxy - miny
    minx -= width * buffer_pct
    maxx += width * buffer_pct
    miny -= height * buffer_pct
    maxy += height * buffer_pct

    if square:
        width = maxx - minx
        height = maxy - miny
        side = max(width, height)
        cx = (minx + maxx) / 2.0
        cy = (miny + maxy) / 2.0
        minx, maxx = cx - side / 2.0, cx + side / 2.0
        miny, maxy = cy - side / 2.0, cy + side / 2.0

    return minx, miny, maxx, maxy


def clip_dem_to_bbox(
        dem_path: Path,
        polygon_gdf: gpd.GeoDataFrame,
        dst_path: Path,
        buffer_pct: float = 0.10,
        square: bool = True,
) -> Path:
    """
    Clip a DEM raster to the buffered bounding box of a single polygon.

    Uses a rectangular bbox (not an exact polygon mask) for simplicity,
    expanded by buffer_pct of that bbox's own width/height in each
    direction so the clip isn't flush against the local basin's edge, and
    (by default) squared up to the larger dimension - see
    compute_clip_bounds for why that matters for the raster refinement step.

    Requires rasterio - imported lazily here so the rest of the script has
    no raster dependency unless this function is actually called.

    Pixels outside the DEM's own extent (e.g. if the buffer pushes past the
    edge of the source raster) are filled with the DEM's nodata value, or 0
    if none is defined - but if the WHOLE clip bbox has no overlap with the
    DEM at all (wrong CRS, wrong tile, bad coordinates...), this raises
    DEMExtentError rather than silently writing an all-nodata raster.
    """
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(dem_path) as src:
        minx, miny, maxx, maxy = compute_clip_bounds(
            polygon_gdf, buffer_pct, target_crs=src.crs, square=square,
        )

        if not _bounds_overlap((minx, miny, maxx, maxy), tuple(src.bounds)):
            raise DEMExtentError(
                f"clip bbox ({minx:.4f}, {miny:.4f}, {maxx:.4f}, {maxy:.4f}) does not "
                f"overlap DEM extent {tuple(round(v, 4) for v in src.bounds)} "
                f"(dem_path={dem_path})"
            )

        window = from_bounds(minx, miny, maxx, maxy, transform=src.transform)
        out_transform = src.window_transform(window)
        fill_value = src.nodata if src.nodata is not None else 0
        out_image = src.read(window=window, boundless=True, fill_value=fill_value)

        out_meta = src.meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": out_image.shape[1],
            "width": out_image.shape[2],
            "transform": out_transform,
        })

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst_path, "w", **out_meta) as dst:
        dst.write(out_image)

    return dst_path


# --------------------------------------------------------------------------
# Tiled DEM support
# --------------------------------------------------------------------------

def load_tile_index(gpkg_path: Path, layer: str, tile_field: str) -> gpd.GeoDataFrame:
    """Read a DEM tile index layer, keeping only the tile code field + geometry."""
    gdf = gpd.read_file(gpkg_path, layer=layer)[[tile_field, "geometry"]].copy()
    return gdf


class DEMTileError(Exception):
    """Raised when no DEM tiles intersect a requested extent, or a resolved tile file is missing."""


def find_intersecting_tiles(
        tile_index: gpd.GeoDataFrame,
        polygon_gdf: gpd.GeoDataFrame,
        tile_field: str,
        buffer_pct: float = 0.10,
        square: bool = True,
) -> list:
    """
    Select-by-location: return the tile codes whose footprint intersects the
    BUFFERED (and, by default, SQUARED) bounding box of polygon_gdf.

    Uses the exact same compute_clip_bounds math as clip_dem_to_bbox (same
    buffer_pct, same square flag) so tile selection agrees with what
    actually gets clipped afterward - selecting against a smaller/unsquared
    extent could miss a tile the eventual clip needs, leaving a strip of
    nodata (or a wrongly-truncated accumulation) in the final result.
    """
    from shapely.geometry import box

    minx, miny, maxx, maxy = compute_clip_bounds(
        polygon_gdf, buffer_pct, target_crs=None, square=square,
    )

    search_box = gpd.GeoDataFrame(
        geometry=[box(minx, miny, maxx, maxy)], crs=polygon_gdf.crs
    ).to_crs(tile_index.crs)

    matches = tile_index[tile_index.intersects(search_box.geometry.iloc[0])]
    return matches[tile_field].unique().tolist()


def merge_dem_tiles(
        dem_dir: Path,
        tile_codes: list,
        file_pattern: str,
        dst_path: Path,
) -> Path:
    """
    Merge (mosaic) one or more DEM tile files into a single raster.

    Builds each tile's path as dem_dir / file_pattern.format(tile=code) and
    raises DEMTileError if any resolved file is missing. The merged raster
    is NOT yet clipped to any outlet's extent - that's still done by
    clip_dem_to_bbox afterward, on this merge's output.

    Requires rasterio - imported lazily, matching clip_dem_to_bbox.
    """
    import rasterio
    from rasterio.merge import merge

    if not tile_codes:
        raise DEMTileError("no tile codes given to merge")

    tile_paths = []
    for code in tile_codes:
        p = dem_dir / file_pattern.format(tile=code)
        if not p.exists():
            raise DEMTileError(f"DEM tile file not found: {p} (tile code={code!r})")
        tile_paths.append(p)

    srcs = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, out_transform = merge(srcs)
        out_meta = srcs[0].meta.copy()
        out_meta.update({
            "driver": "GTiff",
            "height": mosaic.shape[1],
            "width": mosaic.shape[2],
            "transform": out_transform,
        })
    finally:
        for s in srcs:
            s.close()

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dst_path, "w", **out_meta) as dst:
        dst.write(mosaic)

    return dst_path


class RasterCatchmentError(Exception):
    """Raised when raster-based catchment delineation fails or produces nothing."""


def derive_raster_catchment(
        dem_clip_path: Path,
        outlet_geom_row: gpd.GeoDataFrame,
        snap_search_cells: int = 3,
        is_dem_conditioned: bool = False,
        export_rasters: bool = False,
) -> gpd.GeoDataFrame:
    """
    Derive a raster-based catchment polygon from a (small, pre-clipped) DEM
    for a single outlet.

    Pipeline: condition (fill pits/depressions, resolve flats - SKIPPED if
    is_dem_conditioned=True, e.g. for pre-conditioned products) -> D8 flow
    direction -> flow accumulation -> snap the outlet to the highest-
    accumulation cell within snap_search_cells of it -> trace the upstream
    catchment -> polygonize.

    snap_search_cells is in DEM CELL UNITS (not a real-world distance) and
    is a true circular (Euclidean) radius, not a square window - so it's
    resolution-independent, and only cells strictly LESS THAN that radius
    away are eligible (corner cells of the surrounding square, and cells
    exactly at the radius boundary, are excluded). Snapping is "max
    accumulation within the radius", not "nearest cell above a threshold" -
    a global percentile/threshold mask is fragile and can snap to a minor
    noise-driven accumulation blip instead of the real channel. The whole
    snap + catchment lookup is done in raster index (row/col) space rather
    than round-tripping through map coordinates, since pysheds' coordinate
    handling has edge-case rounding issues exactly at pixel-boundary values.

    Because the source DEM is only clipped to the finest level's local
    basin, this catchment is truncated at that clip's edge - that's
    expected, since it's still a more detailed delineation of the local
    basin than the vector polygon alone, and gets unioned with
    catchment_lower by the caller.

    If export_rasters=True, the flow-direction and flow-accumulation
    rasters, plus the raw catchment mask, are written alongside
    dem_clip_path (same directory, "<stem>_ldd.tif" / "<stem>_acc.tif" /
    "<stem>_catchment_raster.tif") for QA/debugging. Off by default - these
    are intermediate products most runs don't need to keep.

    Requires pysheds - imported lazily. Includes a numpy>=2.0 compatibility
    shim, since pysheds 0.5 calls the since-removed numpy.in1d.

    Returns a single-row GeoDataFrame (holes removed) in the DEM's own CRS.
    Raises RasterCatchmentError if delineation produces zero cells or
    polygonize yields nothing (e.g. the snap landed on a pit/sink - which,
    if is_dem_conditioned=True was set on a DEM that actually isn't
    conditioned, is a likely cause).
    """
    import numpy as _np
    if not hasattr(_np, "in1d"):
        _np.in1d = _np.isin
    from pysheds.grid import Grid
    from shapely.geometry import shape

    dem_clip_path = str(dem_clip_path)
    grid = Grid.from_raster(dem_clip_path)
    dem = grid.read_raster(dem_clip_path)

    if is_dem_conditioned:
        inflated = dem
    else:
        inflated = grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))

    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir = grid.flowdir(inflated, dirmap=dirmap)
    acc = grid.accumulation(fdir, dirmap=dirmap)

    if export_rasters:
        stem = Path(dem_clip_path).stem.replace("_dem_clip", "")
        parent = Path(dem_clip_path).parent
        ldd_path = parent / f"{stem}_ldd.tif"
        acc_path = parent / f"{stem}_acc.tif"
        pysheds.io.to_raster(fdir, str(ldd_path))
        pysheds.io.to_raster(acc, str(acc_path))
        print(f"  exported flow direction -> {ldd_path}")
        print(f"  exported flow accumulation -> {acc_path}")

    outlet_in_dem_crs = outlet_geom_row.to_crs(grid.crs)
    pt = outlet_in_dem_crs.geometry.iloc[0]
    col, row = grid.nearest_cell(pt.x, pt.y)

    r0, r1 = max(0, row - snap_search_cells), min(acc.shape[0], row + snap_search_cells + 1)
    c0, c1 = max(0, col - snap_search_cells), min(acc.shape[1], col + snap_search_cells + 1)
    window = _np.asarray(acc[r0:r1, c0:c1]).astype("float64")

    # The slice above is a SQUARE window - its corners sit up to
    # snap_search_cells*sqrt(2) away, well outside the intended radius.
    # Mask out anything at or beyond the actual circular (Euclidean)
    # distance so "radius" means what it says, not "half the side of a
    # bounding box" - only strictly-inside cells are eligible.
    rr, cc = _np.meshgrid(
        _np.arange(r0, r1) - row, _np.arange(c0, c1) - col, indexing="ij"
    )
    within_radius = (rr**2 + cc**2) < snap_search_cells**2
    window = _np.where(within_radius, window, -_np.inf)

    local_r, local_c = _np.unravel_index(_np.argmax(window), window.shape)
    row_snap, col_snap = r0 + local_r, c0 + local_c

    catch = grid.catchment(x=col_snap, y=row_snap, fdir=fdir, dirmap=dirmap, xytype="index")
    if catch.sum() == 0:
        raise RasterCatchmentError(
            f"delineation produced zero cells (snapped row={row_snap}, col={col_snap}, "
            f"acc={acc[row_snap, col_snap]:.1f}) - dem_clip_path={dem_clip_path}"
        )

    if export_rasters:
        cat_path = parent / f"{stem}_catchment_raster.tif"
        pysheds.io.to_raster(catch.astype("int32"), str(cat_path))
        print(f"  exported catchment mask -> {cat_path}")

    shapes_gen = grid.polygonize(catch.astype("int32"))
    polys = [shape(geom) for geom, val in shapes_gen if val == 1]
    if not polys:
        raise RasterCatchmentError(f"polygonize produced no polygons - dem_clip_path={dem_clip_path}")

    gdf_cat = gpd.GeoDataFrame(geometry=polys, crs=grid.crs).dissolve().reset_index(drop=True)
    gdf_cat["geometry"] = gdf_cat["geometry"].apply(remove_holes)

    return gdf_cat


# --------------------------------------------------------------------------
# Core processing
# --------------------------------------------------------------------------

def trace_upstream_at_level(
        outlet_row: dict,
        gdf_grid: gpd.GeoDataFrame,
        label: str,
        field_id: str,
        field_id_down: str,
        pfaf_field: str,
        search_depth: int,
        area_field: str = None,
        headwater_detection: str = "trace",
        force_headwater: bool = False,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, bool]:
    """
    Trace the upstream area for a single outlet at a single grid level.

    The Pfafstetter prefix filter (search_depth) is always applied here
    regardless of headwater_detection: it narrows the search grid to only
    basins sharing the outlet's own PFAF_ID prefix before the upstream
    trace runs, which is what keeps this tractable on large/fine levels.

    force_headwater, when True, overrides ALL detection: the outlet is
    treated as a headwater unconditionally, the upstream trace is skipped,
    and (empty upstream, True) is returned. This is how the spec's
    force_headwaters list is honored - a manual override for outlets where
    visual inspection showed the automatic result was wrong. It takes
    precedence over headwater_detection entirely.

    Otherwise headwater_detection selects HOW the returned is_headwater
    flag is decided:
      - "pfaf": classify the outlet's own (untruncated) PFAF code via
        is_headwater(). When it reports headwater, the upstream is empty
        by construction, so the potentially expensive
        get_upstream_features_iterative() call is SKIPPED outright. Fast,
        but only as trustworthy as the PFAF codes. A missing/null PFAF
        code falls through to tracing rather than being assumed headwater.
      - "trace" (default) / "auto": always run the upstream trace and
        treat an empty result as headwater. Authoritative, ignores PFAF
        parity for this decision. ("auto" behaves as "pfaf" only when a
        usable PFAF code is present, else as "trace" - identical to
        "trace" whenever the code is missing.)

    Returns (gdf_local, gdf_upstream, is_headwater). gdf_upstream is empty
    whenever is_headwater is True. The caller uses is_headwater to decide
    whether the outlet is fully local - see process_outlet.
    """
    start_id = outlet_row[field_id]
    pfaf_code = outlet_row[pfaf_field]
    print(f"[{label}] Local basin ID: {start_id}")

    has_pfaf = pfaf_code is not None and not pd.isna(pfaf_code)

    # PFAF shortcut is used only in "pfaf" mode (or "auto" when a code is
    # present). In "trace" mode we never trust parity - we trace and look.
    use_pfaf_shortcut = has_pfaf and headwater_detection in ("pfaf", "auto")

    # search_depth prefix filter - always applied when configured, needs a
    # usable PFAF code to slice.
    gdf_grid_search = gdf_grid
    if search_depth is not None:
        if not has_pfaf:
            print(f"[{label}] WARNING: search_depth={search_depth} set but no usable "
                  f"PFAF code on this outlet - skipping prefix filter, tracing full grid")
        else:
            pfaf_prefix = pfaf_code[:search_depth]
            print(f"[{label}] PFAF prefix (search): {pfaf_prefix}")
            mask = gdf_grid[pfaf_field].str.startswith(pfaf_prefix)
            gdf_grid_search = gdf_grid.query("@mask")

    gdf_local = gdf_grid_search.query(f"{field_id} == {start_id}").copy()

    # Manual override (spec force_headwaters) beats all detection: treat as
    # headwater, skip the trace entirely, ignore any PFAF parity.
    if force_headwater:
        print(f"[{label}] FORCED headwater by spec (force_headwaters) - "
              f"skipping upstream trace, ignoring detection")
        gdf_upstream = gdf_grid_search.iloc[0:0].copy()
        return gdf_local, gdf_upstream, True

    if use_pfaf_shortcut:
        headwater = is_headwater(pfaf_code)
        print(f"[{label}] PFAF: {pfaf_code} -> "
              f"{'headwater' if headwater else 'interbasin/trunk'} (mode={headwater_detection})")
        if headwater:
            print(f"[{label}] headwater by PFAF - skipping upstream trace")
            gdf_upstream = gdf_grid_search.iloc[0:0].copy()
            return gdf_local, gdf_upstream, True

    gdf_upstream = get_upstream_features_iterative(
        features=gdf_grid_search,
        field_id=field_id,
        field_id_down=field_id_down,
        start_id=start_id,
        include_start=False,
    )
    # get_upstream_features_iterative() strips extra attribute columns down to
    # the tracing essentials - reattach anything else we still need (area_field)
    # from the search grid before it's lost for good.
    gdf_upstream = reattach_fields(gdf_upstream, gdf_grid_search, field_id, [area_field])

    n_upstream = len(gdf_upstream)
    print(f"[{label}] Number of upstream basins: {n_upstream}")

    # In trace/auto mode, headwater status IS the trace result: empty
    # upstream means nothing flows in. (In pfaf mode we only reach here
    # when the code already said trunk, so an empty trace still correctly
    # reports headwater=True - the trace is the tie-breaker either way.)
    headwater = n_upstream == 0
    if headwater:
        print(f"[{label}] empty upstream - headwater by trace")

    return gdf_local, gdf_upstream, headwater


def process_outlet(
        outlet_geom_row: gpd.GeoDataFrame,
        grids: dict,
        levels_spec: list[dict],
        field_id: str,
        field_id_down: str,
        field_outlet: str,
        pfaf_field: str,
        dst_dir: Path,
        area_field: str = None,
        compute_area_crs: str = None,
        area_units: str = "km2",
        catchment_lower_layer: str = "catchment_lower",
        catchment_upper_layer: str = "catchment_upper",
        catchment_layer: str = "catchment",
        catchment_acceptable_error_pct: float = 5.0,
        headwater_detection: str = "trace",
        force_headwaters=None,
        force_upper=None,
        dem_path: Path = None,
        dem_tile_index: gpd.GeoDataFrame = None,
        dem_tile_field: str = None,
        dem_file_pattern: str = None,
        dem_clip_buffer_pct: float = 0.10,
        dem_clip_square: bool = True,
        dem_snap_search_cells: int = 3,
        is_dem_conditioned: bool = False,
        export_rasters: bool = False,
        dry_run: bool = False,
) -> dict:
    """
    Full, self-contained pipeline for ONE outlet. Processing is Pfafstetter-
    driven and fine-level-first - see the module docstring for the full
    rationale. In short:

    1. The FINE level (levels_spec[1]) is always sjoined and traced first.
    2. If the fine local basin is a HEADWATER (by headwater_detection, or
       forced via the outlet appearing in force_headwaters): the coarse
       level is never even queried. catchment_upper = fine local basin
       alone. catchment_lower = None (no lower bound exists to bracket
       with). DEM refinement, when configured, is always attempted rather
       than gated by the tolerance check, since there's nothing to check
       against.
    3. If the fine local basin is a TRUNK basin: its upstream fine
       sub-basins are traced too (confined to the coarse parent's own
       territory via search_depth). The coarse level (levels_spec[0]) is
       then sjoined and traced:
         - coarse HEADWATER: catchment_upper = fine local + fine upstream
           (dissolved). catchment_lower = fine upstream (dissolved) alone.
         - coarse TRUNK: catchment_upper = fine local + fine upstream
           (dissolved) + coarse upstream (dissolved) - the coarse LOCAL
           basin itself is deliberately excluded, since fine local + fine
           upstream already cover that same extent at finer resolution.
           catchment_lower = fine upstream (dissolved) + coarse upstream
           (dissolved) only (no local basin at either scale).
       The usual acceptable-error tolerance then decides whether DEM
       refinement is needed.

    Whenever DEM refinement runs, the FINE level's local basin is always
    the clip extent - raster resolution happens at fine scale in every
    case, unioned with catchment_lower (if any) to become the final
    catchment. catchment falls back to catchment_upper if dem_path isn't
    given or the DEM/raster step fails for any reason - always tagged via
    a catchment_source attribute (vector_upper / vector_upper_forced /
    raster_refined / vector_upper_fallback_*) so provenance and confidence
    are traceable. An outlet listed in the force_upper spec option skips
    raster resolution outright and is tagged vector_upper_forced.

    If dem_tile_index is also given, dem_path is treated as a folder of DEM
    tiles rather than one static file: the tile(s) intersecting this
    outlet's (buffered) clip extent are found via select-by-location against
    dem_tile_index, merged into one mosaic, and THAT gets clipped instead -
    same clip_dem_to_bbox as the single-file case either way.

    This is intentionally not split into level-wide batches: outlets don't
    interact with each other, so each one runs start to finish on its own.

    Returns a dict (not just the output path) so run() can aggregate every
    outlet's results into one combined GeoPackage afterward, without
    re-reading each per-outlet file back off disk:
        fo: Path to this outlet's own per-outlet GeoPackage.
        outlet_id, outlet_geom_row: the outlet's id and original point row.
        gdf_catchment_lower / gdf_catchment_upper / gdf_catchment: the
            corresponding GeoDataFrames (None if that layer wasn't produced).
        catchment_source: provenance tag for gdf_catchment (None if
            gdf_catchment is None).
    """
    outlet_id = outlet_geom_row.iloc[0][field_outlet]
    print(f"\n=== Outlet {outlet_id} ===")

    outlet_dir = dst_dir / "outlets" / f"outlet_{outlet_id}"
    fo = outlet_dir / f"outlet_{outlet_id}.gpkg"

    if not dry_run:
        outlet_dir.mkdir(parents=True, exist_ok=True)
        outlet_geom_row.to_file(fo, layer="outlet", driver="GPKG")

    # Manual headwater override for this outlet (spec force_headwaters).
    # numpy int outlet ids compare/hash equal to plain ints, so a direct
    # membership test works; guard the int() cast for non-numeric ids.
    force_fine_headwater = False
    if force_headwaters:
        try:
            force_fine_headwater = int(outlet_id) in force_headwaters
        except (TypeError, ValueError):
            force_fine_headwater = outlet_id in force_headwaters
    if force_fine_headwater:
        print(f"  outlet {outlet_id} is in force_headwaters - forcing headwater catchment")

    force_upper_this = False
    if force_upper:
        try:
            force_upper_this = int(outlet_id) in force_upper
        except (TypeError, ValueError):
            force_upper_this = outlet_id in force_upper
    if force_upper_this:
        print(f"  outlet {outlet_id} is in force_upper - will force upper approximation "
              f"(raster resolution skipped)")

    coarse_lvl, fine_lvl = levels_spec[0], levels_spec[1]

    # ---- Step 1: FINE level, always sjoined and traced first ----
    gdf_fine_grid = grids[fine_lvl["label"]]
    gdf_fine_joined = gpd.sjoin(left_df=outlet_geom_row, right_df=gdf_fine_grid, how="left")
    fine_outlet_row = gdf_fine_joined.iloc[0].to_dict()

    fine_local, fine_upstream, fine_headwater = trace_upstream_at_level(
        outlet_row=fine_outlet_row,
        gdf_grid=gdf_fine_grid,
        label=fine_lvl["label"],
        field_id=field_id,
        field_id_down=field_id_down,
        pfaf_field=pfaf_field,
        search_depth=fine_lvl["search_depth"],
        area_field=area_field,
        headwater_detection=headwater_detection,
        force_headwater=force_fine_headwater,
    )

    if len(fine_local) == 0:
        print("  no local basin found at the fine level - cannot define a catchment for this outlet")
        return {
            "fo": fo,
            "outlet_id": outlet_id,
            "outlet_geom_row": outlet_geom_row,
            "gdf_catchment_lower": None,
            "gdf_catchment_upper": None,
            "gdf_catchment": None,
            "catchment_source": None,
        }

    fine_n = len(fine_upstream)
    fine_upstream_dissolved = None
    if fine_n > 0:
        fine_upstream_dissolved = dissolve_with_area(
            fine_upstream, area_field=area_field,
            compute_area_crs=compute_area_crs, area_units=area_units,
        )

    if dry_run:
        print(f"[dry-run] would write layers: {fine_lvl['label']}_local"
              + (f", {fine_lvl['label']}_upstream, {fine_lvl['label']}_upstream_dissolved" if fine_n > 0 else ""))
    else:
        fine_local.to_file(fo, layer=f"{fine_lvl['label']}_local", driver="GPKG")
        if fine_n > 0:
            fine_upstream.to_file(fo, layer=f"{fine_lvl['label']}_upstream", driver="GPKG")
            fine_upstream_dissolved.to_file(fo, layer=f"{fine_lvl['label']}_upstream_dissolved", driver="GPKG")

    # ---- Step 2: branch on the fine level's own headwater status ----
    if fine_headwater:
        # No other fine sub-basin feeds this one, and - by the same
        # containment logic - no coarse-level upstream basin can reach it
        # either (it would have to pass through some fine sub-basin
        # upstream of this one, and there isn't one). The coarse level is
        # never even queried.
        print(f"  [{fine_lvl['label']}] headwater - outlet is fully local, "
              f"skipping the coarse level entirely")
        gdf_catchment_upper = dissolve_with_area(
            fine_local, area_field=area_field,
            compute_area_crs=compute_area_crs, area_units=area_units,
        )
        gdf_catchment_lower = None
    else:
        # ---- Step 2b: coarse level, only reached when the fine level is a trunk basin ----
        gdf_coarse_grid = grids[coarse_lvl["label"]]
        gdf_coarse_joined = gpd.sjoin(left_df=outlet_geom_row, right_df=gdf_coarse_grid, how="left")
        coarse_outlet_row = gdf_coarse_joined.iloc[0].to_dict()

        coarse_local, coarse_upstream, coarse_headwater = trace_upstream_at_level(
            outlet_row=coarse_outlet_row,
            gdf_grid=gdf_coarse_grid,
            label=coarse_lvl["label"],
            field_id=field_id,
            field_id_down=field_id_down,
            pfaf_field=pfaf_field,
            search_depth=coarse_lvl["search_depth"],
            area_field=area_field,
            headwater_detection=headwater_detection,
        )
        coarse_n = len(coarse_upstream)
        coarse_upstream_dissolved = None
        if coarse_n > 0:
            coarse_upstream_dissolved = dissolve_with_area(
                coarse_upstream, area_field=area_field,
                compute_area_crs=compute_area_crs, area_units=area_units,
            )

        if dry_run:
            print(f"[dry-run] would write layers: {coarse_lvl['label']}_local"
                  + (f", {coarse_lvl['label']}_upstream, {coarse_lvl['label']}_upstream_dissolved" if coarse_n > 0 else ""))
        else:
            coarse_local.to_file(fo, layer=f"{coarse_lvl['label']}_local", driver="GPKG")
            if coarse_n > 0:
                coarse_upstream.to_file(fo, layer=f"{coarse_lvl['label']}_upstream", driver="GPKG")
                coarse_upstream_dissolved.to_file(fo, layer=f"{coarse_lvl['label']}_upstream_dissolved", driver="GPKG")

        upper_pieces = [fine_local]
        lower_pieces = []
        if fine_upstream_dissolved is not None:
            upper_pieces.append(fine_upstream_dissolved)
            lower_pieces.append(fine_upstream_dissolved)

        if coarse_headwater:
            print(f"  [{coarse_lvl['label']}] headwater - no coarse-level upstream to add")
        else:
            print(f"  [{coarse_lvl['label']}] trunk/interbasin - folding in coarse-level upstream")
            if coarse_upstream_dissolved is not None:
                upper_pieces.append(coarse_upstream_dissolved)
                lower_pieces.append(coarse_upstream_dissolved)

        gdf_catchment_upper = dissolve_with_area(
            pd.concat(upper_pieces).reset_index(drop=True), area_field=area_field,
            compute_area_crs=compute_area_crs, area_units=area_units,
        )
        gdf_catchment_lower = None
        if lower_pieces:
            gdf_catchment_lower = dissolve_with_area(
                pd.concat(lower_pieces).reset_index(drop=True), area_field=area_field,
                compute_area_crs=compute_area_crs, area_units=area_units,
            )

    # ---- approximation quality: how close is the lower bound to the upper bound? ----
    approx_pct = None
    if gdf_catchment_lower is not None and gdf_catchment_upper is not None:
        area_lower = get_area_value(gdf_catchment_lower, area_field, area_units)
        area_upper = get_area_value(gdf_catchment_upper, area_field, area_units)
        if area_lower is not None and area_upper:
            approx_pct = 100.0 * area_lower / area_upper
            print(f"  catchment approximation: lower is {approx_pct:.1f}% of upper "
                  f"(closer to 100% = tighter bracket)")
            gdf_catchment_lower["approx_pct"] = approx_pct
            gdf_catchment_upper["approx_pct"] = approx_pct
        else:
            print("  no area info available - skipping approximation percentage")

    if dry_run:
        if gdf_catchment_lower is not None:
            print(f"[dry-run] would write layer '{catchment_lower_layer}'")
        print(f"[dry-run] would write layer '{catchment_upper_layer}'")
    else:
        if gdf_catchment_lower is not None:
            gdf_catchment_lower.to_file(fo, layer=catchment_lower_layer, driver="GPKG")
        else:
            print("  no lower-bound approximation available (fine level is a headwater), "
                  "skipping catchment_lower layer")

        gdf_catchment_upper.to_file(fo, layer=catchment_upper_layer, driver="GPKG")

    # ---- decide: is the vector-only upper approximation good enough, or is raster
    # refinement needed (and available)? Either way, exactly one 'catchment' layer
    # is the final answer, tagged with catchment_source so provenance is traceable. ----

    def _write_catchment(gdf, source, dry_run_note=""):
        gdf = gdf.copy()
        gdf["catchment_source"] = source
        gdf["n_polygons"] = gdf.geometry.apply(count_polygons).astype(int)
        n_poly = int(gdf["n_polygons"].iloc[0])
        if n_poly != 1:
            print(f"  QC WARNING: catchment has {n_poly} polygon(s) - expected 1 "
                  f"(disconnected geometry, review this outlet)")
        if dry_run:
            print(f"[dry-run] would write layer '{catchment_layer}' (source={source}){dry_run_note}")
        else:
            gdf.to_file(fo, layer=catchment_layer, driver="GPKG")
            print(f"  catchment written (source={source}, n_polygons={n_poly})")
        return {
            "fo": fo,
            "outlet_id": outlet_id,
            "outlet_geom_row": outlet_geom_row,
            "gdf_catchment_lower": gdf_catchment_lower,
            "gdf_catchment_upper": gdf_catchment_upper,
            "gdf_catchment": gdf,
            "catchment_source": source,
        }

    # Manual override (spec force_upper): skip raster resolution entirely and
    # take catchment_upper as the final answer. For outlets - typically in
    # flat plains - where raster delineation is known to mis-align and the
    # vector upper approximation is the more trustworthy result. Beats the
    # tolerance check and DEM availability alike; tagged distinctly so it's
    # never confused with a tolerance-pass or a fallback.
    if force_upper_this:
        print("  outlet forced to upper approximation (force_upper) - "
              "skipping raster resolution")
        return _write_catchment(gdf_catchment_upper, "vector_upper_forced")

    if fine_headwater:
        # No lower bound exists to compare against, by construction - the
        # tolerance check is meaningless here. Refinement is used whenever
        # a DEM is configured; the fine local basin is the fallback.
        needs_refinement = True
        print("  fine level headwater - no lower bound to compare against; "
              "raster refinement is used whenever a DEM is configured")
    else:
        needs_refinement = True
        if approx_pct is not None:
            error_pct = 100.0 - approx_pct
            if error_pct <= catchment_acceptable_error_pct:
                needs_refinement = False
                print(f"  vector approximation within tolerance (error={error_pct:.1f}% <= "
                      f"{catchment_acceptable_error_pct}%) - using catchment_upper as the final catchment")
            else:
                print(f"  vector approximation error {error_pct:.1f}% exceeds tolerance "
                      f"{catchment_acceptable_error_pct}% - refinement needed")
        else:
            print("  no lower-bound comparison available - refinement needed")

    if not needs_refinement:
        return _write_catchment(gdf_catchment_upper, "vector_upper")

    if dem_path is None:
        print("  no dem_path configured - falling back to catchment_upper as the final "
              "catchment (lower confidence: tolerance not met and no raster refinement available)")
        return _write_catchment(gdf_catchment_upper, "vector_upper_fallback_no_dem")

    if dry_run:
        dem_clip_fo = outlet_dir / f"outlet_{outlet_id}_dem_clip.tif"
        print(f"[dry-run] would clip DEM to finest-level local basin -> {dem_clip_fo}")
        return _write_catchment(gdf_catchment_upper, "raster_refined", dry_run_note=" (pending raster refinement)")

    # ---- DEM-refined catchment: clip -> raster catchment -> merge with catchment_lower ----
    # Always uses the FINE level's local basin (fine_local, checked non-empty
    # above) - raster resolution happens at fine scale in every case, keeping
    # the raster work small and per-outlet. Only reached when the vector
    # approximation wasn't good enough on its own (or there wasn't one to
    # check, i.e. the fine-headwater case).
    dem_clip_fo = outlet_dir / f"outlet_{outlet_id}_dem_clip.tif"
    try:
        if dem_tile_index is not None:
            tile_codes = find_intersecting_tiles(
                dem_tile_index, fine_local, dem_tile_field,
                buffer_pct=dem_clip_buffer_pct, square=dem_clip_square,
            )
            if not tile_codes:
                raise DEMTileError(
                    f"no DEM tiles intersect outlet {outlet_id}'s local basin extent "
                    f"(+ {dem_clip_buffer_pct * 100:.0f}% buffer)"
                )
            dem_merged_fo = outlet_dir / f"outlet_{outlet_id}_dem_merged.tif"
            merge_dem_tiles(Path(dem_path), tile_codes, dem_file_pattern, dem_merged_fo)
            print(f"  merged {len(tile_codes)} DEM tile(s) {tile_codes} -> {dem_merged_fo}")
            dem_source = dem_merged_fo
        else:
            dem_merged_fo = None
            dem_source = dem_path

        clip_dem_to_bbox(dem_source, fine_local, dem_clip_fo,
                          buffer_pct=dem_clip_buffer_pct, square=dem_clip_square)
        print(f"  DEM clipped to fine-level local basin -> {dem_clip_fo}")

        # The merge can cover a lot more area than the final small clip (a
        # multi-tile mosaic, e.g. several MERIT Hydro tiles) - it's pure
        # intermediate scratch once the clip exists, so always remove it
        # (regardless of export_rasters, which only governs the other
        # debug rasters - ldd/acc/catchment_raster).
        if dem_merged_fo is not None:
            dem_merged_fo.unlink(missing_ok=True)
            print(f"  removed intermediate merged DEM -> {dem_merged_fo}")

        try:
            gdf_raster_catchment = derive_raster_catchment(
                dem_clip_fo, outlet_geom_row,
                snap_search_cells=dem_snap_search_cells,
                is_dem_conditioned=is_dem_conditioned,
                export_rasters=export_rasters,
            ).to_crs(outlet_geom_row.crs)
            print(f"  raster catchment derived ({len(gdf_raster_catchment)} polygon)")

            refined_pieces = [gdf_raster_catchment]
            if gdf_catchment_lower is not None:
                refined_pieces.append(gdf_catchment_lower)
            gdf_refined_input = pd.concat(refined_pieces).reset_index(drop=True)
            gdf_catchment = dissolve_with_area(
                gdf_refined_input, area_field=area_field,
                compute_area_crs=compute_area_crs, area_units=area_units,
            )
            gdf_catchment["geometry"] = gdf_catchment["geometry"].apply(remove_holes)

            # The area_field attribute sum is untrustworthy for a raster-refined
            # catchment: the raster piece contributes geometry but carries no
            # area_field value, so the sum silently omits it (or the column is
            # absent entirely, leaving the area null). Always replace it with a
            # real geometric area measured on the merged polygon itself - via
            # compute_area_crs when configured, else geodesically on the WGS84
            # ellipsoid - and drop the attribute column so get_area_value picks
            # up this trustworthy value. This guarantees a non-null, correct
            # area for every raster-derived catchment.
            geom_area = compute_geometric_area(
                gdf_catchment, area_units=area_units, area_crs=compute_area_crs,
            )
            gdf_catchment = gdf_catchment.drop(columns=[area_field], errors="ignore")
            gdf_catchment[f"area_computed_{area_units}"] = geom_area
            print(f"  raster catchment area (geometric"
                  f"{', ' + compute_area_crs if compute_area_crs else ', geodesic'}): "
                  f"{geom_area:.4g} {area_units}")
            return _write_catchment(gdf_catchment, "raster_refined")
        except RasterCatchmentError as e:
            print(f"  ERROR: raster catchment derivation failed for outlet {outlet_id}: {e}")
            print("  falling back to catchment_upper as the final catchment")
            return _write_catchment(gdf_catchment_upper, "vector_upper_fallback_raster_failed")
    except (DEMExtentError, DEMTileError) as e:
        print(f"  ERROR: DEM step failed for outlet {outlet_id}: {e}")
        print("  falling back to catchment_upper as the final catchment")
        return _write_catchment(gdf_catchment_upper, "vector_upper_fallback_dem_failed")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def build_aggregated_output(
        outlet_results: list,
        outlet_field: str,
        area_field: str,
        area_units: str,
        dst_path: Path,
) -> Path | None:
    """
    Collect every outlet's process_outlet() result into one combined
    GeoPackage in dst_dir, instead of leaving the final answer scattered
    across one folder per outlet.

    Writes up to 4 layers:
        outlets: one point per outlet, with 'area', 'area_upper',
            'area_lower' (from catchment / catchment_upper / catchment_lower
            respectively - get_area_value's usual area_field-then-
            area_computed_* preference applies), plus catchment_source and
            n_polygons (geometry QC: 1 = clean contiguous catchment, >1 =
            suspicious/disconnected; None if no catchment).
        catchment / catchment_upper / catchment_lower: one polygon per
            outlet that produced that layer, keyed by outlet_field so they
            join back to the outlets layer (or each other) in GIS software.
            The catchment layer also carries catchment_source and
            n_polygons.

    catchment_lower is legitimately absent for headwater outlets (no
    upstream at any level) - those rows just don't appear in that layer,
    everything else is unaffected.

    Per-outlet folders (level*_local, DEM clips, etc.) are left in place
    for QA/debugging - this is a supplementary "final answer" file, not a
    replacement for them.

    Returns dst_path, or None if there were no successful outlets to
    aggregate at all.
    """
    outlet_rows, catchment_rows, upper_rows, lower_rows = [], [], [], []

    for r in outlet_results:
        outlet_id = r["outlet_id"]
        gdf_catchment = r["gdf_catchment"]
        gdf_upper = r["gdf_catchment_upper"]
        gdf_lower = r["gdf_catchment_lower"]

        area = get_area_value(gdf_catchment, area_field, area_units) if gdf_catchment is not None else None
        area_upper = get_area_value(gdf_upper, area_field, area_units) if gdf_upper is not None else None
        area_lower = get_area_value(gdf_lower, area_field, area_units) if gdf_lower is not None else None

        # n_polygons is a geometry QC flag set on the final catchment by
        # _write_catchment (1 = clean/contiguous, >1 = suspicious). Read it
        # straight off the stored column so the outlets summary agrees with
        # the catchment layer; None when there's no catchment for this outlet.
        n_polygons = int(gdf_catchment["n_polygons"].iloc[0]) if gdf_catchment is not None else None

        outlet_row = r["outlet_geom_row"].copy()
        outlet_row["area"] = area
        outlet_row["area_upper"] = area_upper
        outlet_row["area_lower"] = area_lower
        outlet_row["catchment_source"] = r["catchment_source"]
        outlet_row["n_polygons"] = n_polygons
        outlet_rows.append(outlet_row)

        if gdf_catchment is not None:
            row = gdf_catchment[["geometry", "catchment_source", "n_polygons"]].copy()
            row[outlet_field] = outlet_id
            catchment_rows.append(row)
        if gdf_upper is not None:
            row = gdf_upper[["geometry"]].copy()
            row[outlet_field] = outlet_id
            upper_rows.append(row)
        if gdf_lower is not None:
            row = gdf_lower[["geometry"]].copy()
            row[outlet_field] = outlet_id
            lower_rows.append(row)

    if not outlet_rows:
        print("\nNo successful outlets to aggregate - skipping combined output.")
        return None

    dst_path.parent.mkdir(parents=True, exist_ok=True)

    gdf_outlets_agg = pd.concat(outlet_rows).reset_index(drop=True)
    gdf_outlets_agg.to_file(dst_path, layer="outlets", driver="GPKG")

    if catchment_rows:
        pd.concat(catchment_rows).reset_index(drop=True).to_file(
            dst_path, layer="catchment", driver="GPKG"
        )
    if upper_rows:
        pd.concat(upper_rows).reset_index(drop=True).to_file(
            dst_path, layer="catchment_upper", driver="GPKG"
        )
    if lower_rows:
        pd.concat(lower_rows).reset_index(drop=True).to_file(
            dst_path, layer="catchment_lower", driver="GPKG"
        )

    print(f"\nAggregated output written -> {dst_path} "
          f"({len(outlet_rows)} outlet(s), "
          f"{len(catchment_rows)} catchment, {len(upper_rows)} upper, {len(lower_rows)} lower)")
    return dst_path


def build_discrete_output(
        outlet_results: list,
        outlet_field: str,
        dst_path: Path,
) -> Path | None:
    """
    Write each outlet's FINAL catchment to its own layer in a single
    GeoPackage, named 'catchment_<outlet_id>'.

    Unlike build_aggregated_output (one shared 'catchment' layer keyed by
    outlet id), this keeps every outlet's catchment in a separate layer -
    convenient for QA/eyeballing one outlet at a time in GIS without them
    stacked together. Only the final catchment is written here, not the
    upper/lower brackets.

    Outlets that produced no catchment (gdf_catchment is None) get no
    layer, same as they'd be absent from the aggregated output's shared
    catchment layer.

    Returns dst_path, or None if there were no catchments to write at all.
    """
    written = 0
    dst_path.parent.mkdir(parents=True, exist_ok=True)

    for r in outlet_results:
        gdf_catchment = r["gdf_catchment"]
        if gdf_catchment is None:
            continue
        outlet_id = r["outlet_id"]
        gdf_catchment.to_file(dst_path, layer=f"catchment_{outlet_id}", driver="GPKG")
        written += 1

    if written == 0:
        print("\nNo catchments to write - skipping discrete output.")
        return None

    print(f"\nDiscrete output written -> {dst_path} "
          f"({written} catchment layer(s), one per outlet)")
    return dst_path


def run(spec: dict, outlet_id=None, dry_run: bool = False) -> None:
    # outlet_id is reused as the loop variable below; capture the original
    # CLI argument (None = full run, else a single outlet) before that.
    outlet_id_arg = outlet_id

    src_dir = Path(spec["src_dir"])
    dst_dir = Path(spec["dst_dir"])
    src_db = src_dir / spec["src_db"]

    id_field = spec["id_field"]
    id_down_field = spec["id_down_field"]
    pfaf_field = spec["pfaf_field"]
    outlet_field = spec["outlet_field"]
    area_field = spec["area_field"]
    compute_area_crs = spec["compute_area_crs"]
    area_units = spec["area_units"]
    catchment_acceptable_error_pct = spec["catchment_acceptable_error_pct"]
    headwater_detection = spec["headwater_detection"]
    force_headwaters = set(spec["force_headwaters"])
    force_upper = set(spec["force_upper"])
    dem_path = Path(spec["dem_path"]) if spec["dem_path"] else None
    dem_tile_field = spec["dem_tile_field"]
    dem_file_pattern = spec["dem_file_pattern"]
    dem_clip_buffer_pct = spec["dem_clip_buffer_pct"]
    dem_clip_square = spec["dem_clip_square"]
    dem_snap_search_cells = spec["dem_snap_search_cells"]
    is_dem_conditioned = spec["is_dem_conditioned"]
    export_rasters = spec["export_rasters"]

    # Tile index is shared, read-only reference data - load it once, up
    # front, same as the basin grids below.
    dem_tile_index = None
    if spec["dem_tile_index_db"] is not None:
        dem_tile_index = load_tile_index(
            Path(spec["dem_tile_index_db"]), spec["dem_tile_index_layer"], dem_tile_field,
        )
        print(f"\n=== DEM tile index loaded: {len(dem_tile_index)} tile(s) ===")

    gdf_outlets = load_outlets(
        src_db, spec["outlets_layer"], outlet_field=outlet_field, outlet_id=outlet_id
    )
    print(gdf_outlets)

    # Grids are shared, read-only reference data - load each one once, up
    # front, then reuse it across every outlet. This is not "batching" the
    # processing; it's just avoiding re-reading the same GeoPackage layer
    # once per outlet.
    grids = {
        lvl["label"]: load_grid(
            src_db, lvl["layer"], id_field, id_down_field, pfaf_field,
            area_field=area_field,
        )
        for lvl in spec["levels"]
    }
    for label, gdf_grid in grids.items():
        print(f"\n=== Grid loaded: {label} ===")
        print(gdf_grid.info())

    outlet_results = []
    failed = []
    for idx in gdf_outlets.index:
        outlet_geom_row = gdf_outlets.loc[[idx]]
        outlet_id = outlet_geom_row.iloc[0][outlet_field]
        try:
            result = process_outlet(
                outlet_geom_row=outlet_geom_row,
                grids=grids,
                levels_spec=spec["levels"],
                field_id=id_field,
                field_id_down=id_down_field,
                field_outlet=outlet_field,
                pfaf_field=pfaf_field,
                dst_dir=dst_dir,
                area_field=area_field,
                compute_area_crs=compute_area_crs,
                area_units=area_units,
                catchment_acceptable_error_pct=catchment_acceptable_error_pct,
                headwater_detection=headwater_detection,
                force_headwaters=force_headwaters,
                force_upper=force_upper,
                dem_path=dem_path,
                dem_tile_index=dem_tile_index,
                dem_tile_field=dem_tile_field,
                dem_file_pattern=dem_file_pattern,
                dem_clip_buffer_pct=dem_clip_buffer_pct,
                dem_clip_square=dem_clip_square,
                dem_snap_search_cells=dem_snap_search_cells,
                is_dem_conditioned=is_dem_conditioned,
                export_rasters=export_rasters,
                dry_run=dry_run,
            )
            outlet_results.append(result)
        except Exception as e:
            # Outlets are independent (see process_outlet's docstring) - one
            # failing shouldn't stop the rest of the batch. Fail loudly, then
            # move on.
            print(f"\nERROR: outlet {outlet_id} failed and was skipped: {e}")
            failed.append(outlet_id)

    if failed:
        print(f"\n{len(failed)} outlet(s) failed: {failed}")

    # Sanity-check the hand-curated force lists against the outlets actually
    # seen - a forced id that never matched is very likely a typo. Only
    # meaningful for a full run; a single --outlet-id run naturally sees just
    # one id, so skip the check there to avoid false alarms.
    if (force_headwaters or force_upper) and outlet_id_arg is None:
        seen_ids = set()
        for oid in gdf_outlets[outlet_field].tolist():
            try:
                seen_ids.add(int(oid))
            except (TypeError, ValueError):
                seen_ids.add(oid)
        unseen_hw = sorted(i for i in force_headwaters if i not in seen_ids)
        if unseen_hw:
            print(f"\nWARNING: force_headwaters id(s) not found among outlets: {unseen_hw}")
        unseen_up = sorted(i for i in force_upper if i not in seen_ids)
        if unseen_up:
            print(f"\nWARNING: force_upper id(s) not found among outlets: {unseen_up}")

    if not dry_run:
        aggregated_path = dst_dir / spec["aggregated_output"]
        build_aggregated_output(
            outlet_results, outlet_field, area_field, area_units, aggregated_path,
        )

        if spec["discrete_output"]:
            discrete_path = dst_dir / spec["discrete_output"]
            build_discrete_output(outlet_results, outlet_field, discrete_path)

    return outlet_results



# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Spec-driven upstream basin delineation across a multi-level grid ladder."
    )
    parser.add_argument("--spec", type=Path, required=True, help="Path to JSON spec file.")
    parser.add_argument("--outlet-id", default=None, help="Process only this outlet ID.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing files.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    spec = load_spec(args.spec)
    run(spec, outlet_id=args.outlet_id, dry_run=args.dry_run)


if __name__ == "__main__":
    main()