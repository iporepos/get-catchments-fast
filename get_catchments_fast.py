"""
Spec-driven upstream basin delineation.

Given a GeoPackage of outlet points and one or more HydroBASINS-style
grid layers (a "level ladder", e.g. level1 -> level2 -> ...), traces the
upstream contributing area for each outlet at each level, exports the
results, and combines the dissolved upstream polygons across all levels
into a single "full upstream" layer per outlet.

Each outlet is processed as one self-contained pipeline (sjoin -> trace
-> export -> combine, across every level) before moving to the next
outlet. Outlets don't depend on one another, so there is no level-wide
batching step; grid layers are simply loaded once up front and reused.

Usage:

    python delineate_upstream.py --spec spec.json
    python delineate_upstream.py --spec spec.json --outlet-id 3
    python delineate_upstream.py --spec spec.json --dry-run

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

    # Optional: name of a pre-computed area column already present on the grid
    # (e.g. HydroBASINS' "SUB_AREA", in km^2). When set, this column is carried
    # through and summed on every dissolve. Leave null/omit if the grid has none.
    spec.setdefault("area_field", None)

    # Optional: an equal-area CRS (e.g. "ESRI:102033" for South America Albers
    # Equal Area Conic). When set, a fresh geometric area is also computed on
    # every dissolved polygon, in the units below.
    spec.setdefault("compute_area_crs", None)
    spec.setdefault("area_units", "km2")
    if spec["area_units"] not in ("km2", "m2"):
        raise ValueError("area_units must be 'km2' or 'm2'")

    # Optional: path to an EXISTING DEM raster (e.g. a MERIT Hydro tile).
    # When set, each outlet gets a small DEM clip around its finest-level
    # local basin, as the first building block toward a raster-refined
    # "middle" catchment estimate. Left null/omitted, this step is skipped
    # entirely - no raster dependency is needed unless this is set.
    spec.setdefault("dem_path", None)

    # Bounding-box buffer applied around the finest level's local basin
    # before clipping, as a fraction of that basin's width/height (0.10 = 10%).
    spec.setdefault("dem_clip_buffer_pct", 0.10)

    # Pour-point snap search radius, in DEM CELL UNITS (not a real-world
    # distance) - spec-driven so it's easy to tune per DEM, but stays
    # resolution-independent since it's expressed in cells, not meters.
    spec.setdefault("dem_snap_search_cells", 3)

    # Set true if the DEM is already hydrologically conditioned (e.g. MERIT
    # Hydro's elevation product) - skips fill_pits/fill_depressions/
    # resolve_flats entirely, saving real time across many outlets. Default
    # false (also if explicitly null) - conditions the DEM as normal.
    spec.setdefault("is_dem_conditioned", False)
    if spec["is_dem_conditioned"] is None:
        spec["is_dem_conditioned"] = False

    for i, lvl in enumerate(spec["levels"]):
        for k in ("label", "layer"):
            if k not in lvl:
                raise ValueError(f"levels[{i}] is missing required key '{k}'")
        lvl.setdefault("search_depth", None)

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
# Area handling
# --------------------------------------------------------------------------

# Function to remove interior rings
# Function to remove interior rings
def remove_holes(geometry):
    if geometry.geom_type == 'Polygon':
        return Polygon(geometry.exterior)
    elif geometry.geom_type == 'MultiPolygon':
        # Reconstruct MultiPolygons by removing holes from each part
        return MultiPolygon([Polygon(g.exterior) for g in geometry.geoms])
    return geometry


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

    gdf_dissolved = gdf.dissolve(by="_dissolve_key", aggfunc=aggfunc).reset_index(drop=True)

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


def clip_dem_to_bbox(
        dem_path: Path,
        polygon_gdf: gpd.GeoDataFrame,
        dst_path: Path,
        buffer_pct: float = 0.10,
) -> Path:
    """
    Clip a DEM raster to the buffered bounding box of a single polygon.

    Prototype step for a raster-refined "middle" catchment estimate: this
    ONLY clips, it does not do any flow-direction/accumulation or watershed
    delineation. Uses a rectangular bbox (not an exact polygon mask) for
    simplicity, expanded by buffer_pct of that bbox's own width/height in
    each direction so the clip isn't flush against the local basin's edge.

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
        polygon_in_dem_crs = polygon_gdf.to_crs(src.crs)
        minx, miny, maxx, maxy = polygon_in_dem_crs.total_bounds

        width = maxx - minx
        height = maxy - miny
        minx -= width * buffer_pct
        maxx += width * buffer_pct
        miny -= height * buffer_pct
        maxy += height * buffer_pct

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


class RasterCatchmentError(Exception):
    """Raised when raster-based catchment delineation fails or produces nothing."""


def derive_raster_catchment(
        dem_clip_path: Path,
        outlet_geom_row: gpd.GeoDataFrame,
        snap_search_cells: int = 3,
) -> gpd.GeoDataFrame:
    """
    Derive a raster-based catchment polygon from a (small, pre-clipped) DEM
    for a single outlet.

    Pipeline: condition (fill pits/depressions, resolve flats - SKIPPED if
    is_dem_conditioned=True, e.g. for pre-conditioned products like MERIT
    Hydro) -> D8 flow direction -> flow accumulation -> snap the outlet to
    the highest-accumulation cell within snap_search_cells of it -> trace
    the upstream catchment -> polygonize.

    snap_search_cells is in DEM CELL UNITS (not a real-world distance) and
    is a true circular (Euclidean) radius, not a square window - so it's
    resolution-independent, and only cells strictly LESS THAN that radius
    away are eligible (corner cells of the surrounding square, and cells
    exactly at the radius boundary, are excluded).
    Snapping is "max accumulation within the radius", not "nearest cell
    above a threshold" - a global percentile/threshold mask is fragile and
    can snap to a minor noise-driven accumulation blip instead of the real
    channel. The whole snap + catchment lookup is done in raster index
    (row/col) space rather than round-tripping through map coordinates,
    since pysheds' coordinate handling has edge-case rounding issues
    exactly at pixel-boundary values.

    Because the source DEM is only clipped to the finest level's local
    basin, this catchment is truncated at that clip's edge - that's
    expected, since it's still a more detailed delineation of the local
    basin than the vector polygon alone, and gets unioned with
    catchment_lower by the caller.

    Requires pysheds - imported lazily. Includes a numpy>=2.0 compatibility
    shim, since pysheds 0.5 calls the since-removed numpy.in1d.

    Returns a single-row GeoDataFrame in the DEM's own CRS.
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

    # filled
    inflated = grid.resolve_flats(grid.fill_depressions(grid.fill_pits(dem)))

    dirmap = (64, 128, 1, 2, 4, 8, 16, 32)
    fdir = grid.flowdir(inflated, dirmap=dirmap)
    acc = grid.accumulation(fdir, dirmap=dirmap)

    # export
    fdir_path = Path(dem_clip_path).parent / "fdir_pysheds.tif"
    pysheds.io.to_raster(fdir, str(fdir_path))

    acc_path = Path(dem_clip_path).parent / "acc_pysheds.tif"
    pysheds.io.to_raster(acc, str(acc_path))

    outlet_in_dem_crs = outlet_geom_row.to_crs(grid.crs)
    pt = outlet_in_dem_crs.geometry.iloc[0]
    col, row = grid.nearest_cell(pt.x, pt.y)

    r0, r1 = max(0, row - snap_search_cells), min(acc.shape[0], row + snap_search_cells + 1)
    c0, c1 = max(0, col - snap_search_cells), min(acc.shape[1], col + snap_search_cells + 1)
    window = _np.asarray(acc[r0:r1, c0:c1]).astype("float64")

    # The slice above is a SQUARE window - its corners sit up to
    # snap_search_cells*sqrt(2) away, well outside the intended radius.
    # Mask out anything beyond the actual circular (Euclidean) distance so
    # "radius" means what it says, not "half the side of a bounding box".
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

    cat_path = Path(dem_clip_path).parent / "catchment_pysheds.tif"
    pysheds.io.to_raster(catch.astype("int32"), str(cat_path))

    shapes_gen = grid.polygonize(catch.astype("int32"))
    polys = [shape(geom) for geom, val in shapes_gen if val == 1]
    if not polys:
        raise RasterCatchmentError(f"polygonize produced no polygons - dem_clip_path={dem_clip_path}")


    gdf_cat = gpd.GeoDataFrame(geometry=polys, crs=grid.crs).dissolve().reset_index(drop=True)

    # Apply the function to the geometry column
    gdf_cat['geometry'] = gdf_cat['geometry'].apply(remove_holes)

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
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """
    Trace the upstream area for a single outlet at a single grid level.

    The Pfafstetter prefix filter (search_depth) is applied here: it narrows
    the search grid to only basins sharing the outlet's own PFAF_ID prefix
    before the upstream trace runs, which is what keeps this tractable on
    large/fine-resolution levels.

    Returns (gdf_local, gdf_upstream). gdf_upstream may be empty.
    """
    start_id = outlet_row[field_id]
    print(f"[{label}] Local basin ID: {start_id}")

    gdf_grid_search = gdf_grid
    if search_depth is not None:
        pfaf_prefix = outlet_row[pfaf_field][:search_depth]
        print(f"[{label}] PFAF full/prefix: {outlet_row[pfaf_field]} / {pfaf_prefix}")
        mask = gdf_grid[pfaf_field].str.startswith(pfaf_prefix)
        gdf_grid_search = gdf_grid.query("@mask")

    gdf_local = gdf_grid_search.query(f"{field_id} == {start_id}").copy()

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

    print(f"[{label}] Number of upstream basins: {len(gdf_upstream)}")

    return gdf_local, gdf_upstream


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
        catchment_middle_layer: str = "catchment_middle",
        dem_path: Path = None,
        dem_clip_buffer_pct: float = 0.10,
        dem_snap_search_cells: int = 3,
        dry_run: bool = False,
) -> Path:
    """
    Full, self-contained pipeline for ONE outlet: sjoin against every level's
    grid, trace upstream at every level, write every layer, then derive
    bracketing catchment estimates — all before moving on to the next outlet.

    catchment_lower: dissolved union of every level's *upstream-only* basins
        (local basin excluded everywhere). This always UNDERestimates the
        true catchment, since it's missing the local basin's own
        contribution entirely.

    catchment_upper: dissolved union of the FIRST (coarsest) level's local
        basin + that same level's upstream. Because every finer level's
        search space is restricted to the coarsest level's own PFAF prefix,
        the coarsest local basin already contains all finer-level detail
        near the outlet, and its own upstream trace covers everything
        beyond it - so this always OVERestimates (or exactly equals) the
        true catchment.

    When area info is available, an approx_pct = 100 * lower/upper is
    attached to both layers: the closer to 100%, the tighter the bracket.

    If dem_path is given, the LAST (finest) level's local basin is used to
    clip a small DEM extract for this outlet, from which a raster-derived
    catchment is delineated (fill -> D8 flow direction -> accumulation ->
    pour-point snap -> trace -> polygonize) and unioned with catchment_lower
    to produce catchment_middle - a tighter, terrain-following estimate
    than catchment_upper's coarse local-basin polygon. The raster catchment
    is truncated at the DEM clip's edge, which is fine: it's guaranteed to
    still be a more detailed delineation of the local basin than the vector
    polygon alone.

    This is intentionally not split into level-wide batches: outlets don't
    interact with each other, so each one runs start to finish on its own.
    """
    outlet_id = outlet_geom_row.iloc[0][field_outlet]
    print(f"\n=== Outlet {outlet_id} ===")

    outlet_dir = dst_dir / f"outlet_{outlet_id}"
    fo = outlet_dir / f"outlet_{outlet_id}.gpkg"

    if not dry_run:
        outlet_dir.mkdir(parents=True, exist_ok=True)
        outlet_geom_row.to_file(fo, layer="outlet", driver="GPKG")

    dissolved_pieces = []
    first_level_local = None
    first_level_upstream_dissolved = None
    last_level_local = None

    for i, lvl in enumerate(levels_spec):
        label = lvl["label"]
        gdf_grid = grids[label]

        gdf_point_joined = gpd.sjoin(
            left_df=outlet_geom_row, right_df=gdf_grid, how="left"
        )
        outlet_row = gdf_point_joined.iloc[0].to_dict()

        gdf_local, gdf_upstream = trace_upstream_at_level(
            outlet_row=outlet_row,
            gdf_grid=gdf_grid,
            label=label,
            field_id=field_id,
            field_id_down=field_id_down,
            pfaf_field=pfaf_field,
            search_depth=lvl["search_depth"],
            area_field=area_field,
        )
        n = len(gdf_upstream)

        gdf_upstream_dissolved = None
        if n > 0:
            gdf_upstream_dissolved = dissolve_with_area(
                gdf_upstream, area_field=area_field,
                compute_area_crs=compute_area_crs, area_units=area_units,
            )
            dissolved_pieces.append(gdf_upstream_dissolved)

        if i == 0:
            first_level_local = gdf_local
            first_level_upstream_dissolved = gdf_upstream_dissolved
        last_level_local = gdf_local  # overwritten each loop - ends up as the finest level's

        if dry_run:
            print(f"[dry-run] would write layers: {label}_local"
                  + (f", {label}_upstream, {label}_upstream_dissolved" if n > 0 else ""))
            continue

        gdf_local.to_file(fo, layer=f"{label}_local", driver="GPKG")

        if n > 0:
            gdf_upstream.to_file(fo, layer=f"{label}_upstream", driver="GPKG")
            gdf_upstream_dissolved.to_file(fo, layer=f"{label}_upstream_dissolved", driver="GPKG")

    # ---- catchment_lower: union of every level's upstream-only pieces ----
    gdf_catchment_lower = None
    if dissolved_pieces:
        gdf_pieces = pd.concat(dissolved_pieces).reset_index(drop=True)
        gdf_catchment_lower = dissolve_with_area(
            gdf_pieces, area_field=area_field,
            compute_area_crs=compute_area_crs, area_units=area_units,
        )

    # ---- catchment_upper: coarsest (first) level's local basin + its own upstream ----
    upper_pieces = []
    if first_level_local is not None and len(first_level_local) > 0:
        upper_pieces.append(first_level_local)
    if first_level_upstream_dissolved is not None:
        upper_pieces.append(first_level_upstream_dissolved)

    gdf_catchment_upper = None
    if upper_pieces:
        gdf_upper_input = pd.concat(upper_pieces).reset_index(drop=True)
        gdf_catchment_upper = dissolve_with_area(
            gdf_upper_input, area_field=area_field,
            compute_area_crs=compute_area_crs, area_units=area_units,
        )

    # ---- approximation quality: how close is the lower bound to the upper bound? ----
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
        if gdf_catchment_upper is not None:
            print(f"[dry-run] would write layer '{catchment_upper_layer}'")
    else:
        if gdf_catchment_lower is not None:
            gdf_catchment_lower.to_file(fo, layer=catchment_lower_layer, driver="GPKG")
        else:
            print("  no upstream area at any level, skipping catchment_lower layer")

        if gdf_catchment_upper is not None:
            gdf_catchment_upper.to_file(fo, layer=catchment_upper_layer, driver="GPKG")
        else:
            print("  no local basin found, skipping catchment_upper layer")

    # ---- DEM-refined "middle" estimate: clip -> raster catchment -> merge with catchment_lower ----
    # Uses the FINEST level's local basin (last_level_local) - the tightest extent
    # available - to keep the raster work small and per-outlet.
    if dem_path is not None:
        dem_clip_fo = outlet_dir / f"outlet_{outlet_id}_dem_clip.tif"
        if dry_run:
            print(f"[dry-run] would clip DEM to finest-level local basin -> {dem_clip_fo}")
            print(f"[dry-run] would derive raster catchment and write layer '{catchment_middle_layer}'")
        elif last_level_local is not None and len(last_level_local) > 0:
            try:
                clip_dem_to_bbox(dem_path, last_level_local, dem_clip_fo, buffer_pct=dem_clip_buffer_pct)
                print(f"  DEM clipped to finest-level local basin -> {dem_clip_fo}")

                try:
                    gdf_raster_catchment = derive_raster_catchment(
                        dem_clip_fo, outlet_geom_row, snap_search_cells=dem_snap_search_cells,
                    ).to_crs(outlet_geom_row.crs)
                    print(f"  raster catchment derived ({len(gdf_raster_catchment)} polygon)")

                    middle_pieces = [gdf_raster_catchment]
                    if gdf_catchment_lower is not None:
                        middle_pieces.append(gdf_catchment_lower)
                    gdf_middle_input = pd.concat(middle_pieces).reset_index(drop=True)
                    gdf_catchment_middle = dissolve_with_area(
                        gdf_middle_input, area_field=area_field,
                        compute_area_crs=compute_area_crs, area_units=area_units,
                    )
                    gdf_catchment_middle["geometry"] = gdf_catchment_middle["geometry"].apply(remove_holes)
                    if area_field and not compute_area_crs:
                        # area_field is an attribute SUM inherited from the vector basins;
                        # the raster piece has no such attribute, so that sum silently
                        # excludes its contribution. Only compute_area_crs (fresh geometric
                        # area on the actual merged polygon) is trustworthy for this layer.
                        print("  NOTE: area_field on catchment_middle excludes the raster "
                              "piece's area - set compute_area_crs for an accurate area here")
                    gdf_catchment_middle.to_file(fo, layer=catchment_middle_layer, driver="GPKG")
                    print(f"  catchment_middle written")
                except RasterCatchmentError as e:
                    print(f"  ERROR: raster catchment derivation failed for outlet {outlet_id}: {e}")
            except DEMExtentError as e:
                print(f"  ERROR: DEM clip failed for outlet {outlet_id}, skipping DEM step: {e}")
        else:
            print("  no local basin available - skipping DEM clip")

    return fo


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def run(spec: dict, outlet_id=None, dry_run: bool = False) -> None:
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
    dem_path = Path(spec["dem_path"]) if spec["dem_path"] else None
    dem_clip_buffer_pct = spec["dem_clip_buffer_pct"]
    dem_snap_search_cells = spec["dem_snap_search_cells"]

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

    outputs = []
    failed = []
    for idx in gdf_outlets.index:
        outlet_geom_row = gdf_outlets.loc[[idx]]
        outlet_id = outlet_geom_row.iloc[0][outlet_field]
        try:
            fo = process_outlet(
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
                dem_path=dem_path,
                dem_clip_buffer_pct=dem_clip_buffer_pct,
                dem_snap_search_cells=dem_snap_search_cells,
                dry_run=dry_run,
            )
            outputs.append(fo)
        except Exception as e:
            # Outlets are independent (see process_outlet's docstring) - one
            # failing shouldn't stop the rest of the batch. Fail loudly, then
            # move on.
            print(f"\nERROR: outlet {outlet_id} failed and was skipped: {e}")
            failed.append(outlet_id)

    if failed:
        print(f"\n{len(failed)} outlet(s) failed: {failed}")

    return outputs


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