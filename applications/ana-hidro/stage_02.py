"""
Stage 2 - delineate the stations left pending by Stage 1 with get_catchments_fast.

Three steps, all driven by one JSON config:

  prepare  read sites_pending_stage_01, keep the selected states (`ufs`, or
           all when null), drop stations that fall in no basin of the finest
           HydroBASINS level (lagoons, open water - they stay pending with
           status "no hydrobasin"), write the rest as outlet points to
           <intermediate_dir>/<run_name>/outlets.gpkg and write the
           get_catchments_fast spec next to them (gcf_spec.json).
  run      call get_catchments_fast.py on that spec; the full log goes to
           <intermediate_dir>/<run_name>/run.log.
  collect  read the algorithm's aggregated output, join the station
           attributes, compute geodesic areas, compare with the reported
           areas and write the Stage 2 GeoPackage and report.

Outputs (in out_dir from the config):
  <out_gpkg>
    sites                   ANA stations of the selected states (all when ufs
                            is null), with Stage 1 and Stage 2 status
    sites_stage02           stations that got a catchment in this run
    catchments_stage02      the catchment polygons
    sites_pending_stage_02  stations still without a catchment
  <out_report>              assessment highlights

Status (column `status_stage02`, on sites and catchments), from the ratio
geodesic area / ANA reported area and the polygon count:
  ok                      ratio within [1/area_flag_ratio, area_flag_ratio], one part
  probable overestimate   ratio > area_flag_ratio
  probable underestimate  ratio < 1/area_flag_ratio
  multiparts              area ok, but the catchment has more than one polygon
                          after minor parts were dropped (see multipart_drop_pct)
  no ana area             ANA reports no drainage area to compare with
Sites only:
  no hydrobasin           station outside every finest-level basin, not run
  failed                  the algorithm produced no catchment

Usage:
    python stage_02.py --config specs/stage_02_rs.json             # all steps
    python stage_02.py --config specs/stage_02_rs.json --step collect --force

collect refuses to overwrite an existing stage 02 GeoPackage (it may hold
manual edits for stage 02b) unless --force is given.

Config keys (JSON; relative paths resolve against the config file's folder):
    run_name          required, name of the run folder under intermediate_dir
    stage01_gpkg      required, Stage 1 GeoPackage
    out_dir           required, Stage 2 output folder
    intermediate_dir  required, folder for the algorithm's results
    out_gpkg          "ana_hidro_stage_02.gpkg"
    out_report        "report_stage_02.md"
    ufs               null (all) or a list of state names as in the `uf` column
    area_flag_ratio   1.5   area mismatch flagged in the report
    area_flag_bands   null  looser ratios for small catchments, by ANA area:
                            [[max_area_km2, ratio], ...], e.g. [[100, 2.0]] =
                            below 100 km2 ok within [0.5, 2.0]; above the last
                            band area_flag_ratio applies
    multipart_drop_pct 5.0  in a multi-part catchment, drop parts smaller than
                            this % of the total area (stray raster pixels);
                            the largest part is always kept. null disables.
    gcf_script        path to get_catchments_fast.py
    gcf               dict of get_catchments_fast spec keys (src_dir, src_db,
                      levels, dem_path, tuning, ...). outlets_db, outlets_layer,
                      outlet_field and dst_dir are filled in by this script.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Geod
from shapely.geometry import MultiPolygon

REQUIRED_KEYS = ["run_name", "stage01_gpkg", "out_dir", "intermediate_dir", "gcf_script", "gcf"]
DEFAULTS = {
    "out_gpkg": "ana_hidro_stage_02.gpkg",
    "out_report": "report_stage_02.md",
    "ufs": None,
    "area_flag_ratio": 1.5,
    "area_flag_bands": None,
    "multipart_drop_pct": 5.0,
}
OUTLET_FIELD = "outlet_id"   # integer ANA code, as get_catchments_fast wants int ids
OUT_CRS = "EPSG:4674"
GEOD = Geod(ellps="WGS84")


def geod_area_km2(geom) -> float:
    return abs(GEOD.geometry_area_perimeter(geom)[0]) / 1e6


def drop_minor_parts(geom, min_pct):
    """Drop polygon parts under min_pct of the total area (largest always kept).

    Returns (geometry, n_parts_dropped, area_dropped_km2).
    """
    if min_pct is None or not isinstance(geom, MultiPolygon) or len(geom.geoms) < 2:
        return geom, 0, 0.0
    parts = sorted(geom.geoms, key=geod_area_km2, reverse=True)
    areas = [geod_area_km2(g) for g in parts]
    total = sum(areas)
    keep = [g for i, (g, a) in enumerate(zip(parts, areas)) if i == 0 or a >= total * min_pct / 100.0]
    dropped = len(parts) - len(keep)
    if not dropped:
        return geom, 0, 0.0
    area_dropped = total - sum(geod_area_km2(g) for g in keep)
    new = keep[0] if len(keep) == 1 else MultiPolygon(keep)
    return new, dropped, round(area_dropped, 3)


def clean_parts(catch: gpd.GeoDataFrame, min_pct) -> gpd.GeoDataFrame:
    """Drop minor parts; sets geometry, n_polygons_raw, parts_dropped, area_dropped_km2, n_polygons."""
    catch = catch.copy()
    catch["n_polygons_raw"] = [len(g.geoms) if isinstance(g, MultiPolygon) else 1 for g in catch.geometry]
    cleaned = [drop_minor_parts(g, min_pct) for g in catch.geometry]
    catch["geometry"] = [c[0] for c in cleaned]
    catch["parts_dropped"] = [c[1] for c in cleaned]
    catch["area_dropped_km2"] = [c[2] for c in cleaned]
    catch["n_polygons"] = [len(g.geoms) if isinstance(g, MultiPolygon) else 1 for g in catch.geometry]
    return catch


def measure(catch: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Geodesic area, polygon count and ratio to the ANA area (needs area_ana_km2)."""
    catch = catch.copy()
    catch["n_polygons"] = [len(g.geoms) if isinstance(g, MultiPolygon) else 1 for g in catch.geometry]
    catch["area_geod_km2"] = catch.geometry.apply(geod_area_km2).round(2)
    catch["area_ratio_ana"] = (catch["area_geod_km2"]
                               / catch["area_ana_km2"].where(catch["area_ana_km2"] > 0)).round(3)
    return catch


def flag_ratios(area_ana: pd.Series, flag_ratio: float, bands=None) -> pd.Series:
    """Tolerated ratio per station: from the first band whose limit exceeds the ANA area.

    bands: [[max_ana_area_km2, ratio], ...] - e.g. [[100, 2.0]] means stations
    with an ANA area below 100 km2 are ok within [0.5, 2.0]; larger ones use
    flag_ratio.
    """
    f = pd.Series(float(flag_ratio), index=area_ana.index)
    for limit, ratio in sorted(bands or [], reverse=True):  # smallest band applied last, wins
        f[area_ana < float(limit)] = float(ratio)
    return f


def auto_status(catch: pd.DataFrame, flag_ratio: float, bands=None) -> pd.Series:
    """Automatic status from area_ratio_ana and n_polygons (see module docstring)."""
    r = catch["area_ratio_ana"]
    f = flag_ratios(catch["area_ana_km2"], flag_ratio, bands)
    status = pd.Series("ok", index=catch.index, dtype=object)
    status[catch["n_polygons"] > 1] = "multiparts"
    status[r > f] = "probable overestimate"
    status[r < 1 / f] = "probable underestimate"
    status[r.isna()] = "no ana area"
    return status


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------

def load_config(config_path: Path) -> dict:
    config_path = Path(config_path).resolve()
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED_KEYS if not cfg.get(k)]
    if missing:
        raise ValueError(f"Config {config_path} is missing required key(s): {missing}")
    unknown = sorted(set(cfg) - set(REQUIRED_KEYS) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"Config {config_path} has unknown key(s): {unknown}")
    cfg = {**DEFAULTS, **cfg}

    def path(v):
        p = Path(v).expanduser()
        return p if p.is_absolute() else (config_path.parent / p).resolve()

    cfg["config_path"] = config_path
    bands = cfg["area_flag_bands"] or []
    if not all(isinstance(b, list) and len(b) == 2 and all(isinstance(v, (int, float)) for v in b)
               and b[0] > 0 and b[1] >= 1 for b in bands):
        raise ValueError(f"area_flag_bands must be [[max_area_km2, ratio>=1], ...], got {bands!r}")
    cfg["stage01_gpkg"] = path(cfg["stage01_gpkg"])
    cfg["out_dir"] = path(cfg["out_dir"])
    cfg["gcf_script"] = path(cfg["gcf_script"])
    cfg["run_dir"] = path(cfg["intermediate_dir"]) / cfg["run_name"]
    cfg["out_gpkg"] = cfg["out_dir"] / cfg["out_gpkg"]
    cfg["out_report"] = cfg["out_dir"] / cfg["out_report"]
    cfg["outlets_gpkg"] = cfg["run_dir"] / "outlets.gpkg"
    cfg["gcf_spec"] = cfg["run_dir"] / "gcf_spec.json"
    cfg["gcf_dst_dir"] = cfg["run_dir"] / "gcf"
    cfg["run_log"] = cfg["run_dir"] / "run.log"
    return cfg


# ---------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------

def step_prepare(cfg: dict) -> None:
    run_dir = cfg["run_dir"]
    run_dir.mkdir(parents=True, exist_ok=True)

    pending = gpd.read_file(cfg["stage01_gpkg"], layer="sites_pending_stage_01")
    if cfg["ufs"]:
        unknown = sorted(set(cfg["ufs"]) - set(pending["uf"].dropna()))
        if unknown:
            raise ValueError(f"ufs not found in the uf column: {unknown}")
        pending = pending[pending["uf"].isin(cfg["ufs"])]
    if pending.empty:
        raise ValueError("No pending stations selected.")

    outlets = pending[["name", "title", "river", "uf", "area_ana_km2", "geometry"]].copy()
    outlets.insert(0, OUTLET_FIELD, outlets["name"].astype(int))
    assert outlets[OUTLET_FIELD].is_unique
    outlets = outlets.to_crs("EPSG:4326")  # same CRS as the HydroBASINS layers

    # Stations must fall inside (or on the edge of) a basin of the finest level,
    # otherwise there is nothing to trace from (lagoons, open water).
    gcf = cfg["gcf"]
    basins_db = Path(gcf["src_dir"]) / gcf["src_db"]
    fine_layer = gcf["levels"][-1]["layer"]
    basins = gpd.read_file(basins_db, layer=fine_layer, bbox=tuple(outlets.total_bounds),
                           columns=[gcf["id_field"]]).to_crs(outlets.crs)
    hit = gpd.sjoin(outlets[["geometry"]], basins, how="inner", predicate="intersects")
    linked = outlets.index.isin(hit.index)
    unlinked = outlets[~linked]
    outlets = outlets[linked]

    if cfg["outlets_gpkg"].exists():
        cfg["outlets_gpkg"].unlink()
    outlets.to_file(cfg["outlets_gpkg"], layer="outlets", driver="GPKG")
    unlinked.to_file(cfg["outlets_gpkg"], layer="outlets_no_hydrobasin", driver="GPKG")
    print(f"prepare: {len(unlinked)} station(s) outside every {fine_layer} basin, kept pending: "
          f"{', '.join(unlinked['name'])}")

    spec = dict(cfg["gcf"])
    spec.update({
        "dst_dir": str(cfg["gcf_dst_dir"]),
        "outlets_db": str(cfg["outlets_gpkg"]),
        "outlets_layer": "outlets",
        "outlet_field": OUTLET_FIELD,
    })
    with open(cfg["gcf_spec"], "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=4, ensure_ascii=False)
    print(f"prepare: {len(outlets)} outlets -> {cfg['outlets_gpkg']}")
    print(f"prepare: spec -> {cfg['gcf_spec']}")


def step_run(cfg: dict) -> None:
    cmd = [sys.executable, str(cfg["gcf_script"]), "--spec", str(cfg["gcf_spec"])]
    print(f"run: {' '.join(cmd)}")
    print(f"run: log -> {cfg['run_log']}")
    with open(cfg["run_log"], "w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"get_catchments_fast exited with {proc.returncode}; see {cfg['run_log']}")


def step_collect(cfg: dict, force: bool = False) -> None:
    if cfg["out_gpkg"].exists() and not force:
        raise FileExistsError(
            f"{cfg['out_gpkg']} exists and may hold manual edits (stage 02b revision). "
            "Pass --force to rebuild it from the algorithm output.")
    out_dir = cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg["gcf_spec"], encoding="utf-8") as f:
        spec = json.load(f)
    agg = Path(spec["dst_dir"]) / spec.get("aggregated_output", "catchments.gpkg")

    sites = gpd.read_file(cfg["stage01_gpkg"], layer="sites").set_index("name")
    if cfg["ufs"]:
        # per-state run: the site layers cover only the selected states
        sites = sites[sites["uf"].isin(cfg["ufs"])]
    outlets = gpd.read_file(cfg["outlets_gpkg"], layer="outlets", ignore_geometry=True).set_index("name")
    res_out = gpd.read_file(agg, layer="outlets", ignore_geometry=True)
    res_catch = gpd.read_file(agg, layer="catchment").to_crs(OUT_CRS)
    res_upper = gpd.read_file(agg, layer="catchment_upper", ignore_geometry=True)
    res_lower_ids = set()
    try:
        res_lower_ids = set(gpd.read_file(agg, layer="catchment_lower", ignore_geometry=True)[OUTLET_FIELD])
    except Exception:
        pass

    for df in (res_out, res_catch, res_upper):
        df["name"] = df[OUTLET_FIELD].astype(int).astype(str).str.zfill(8)
    res_out = res_out.set_index("name")

    catch = res_catch.set_index("name")[["catchment_source", "geometry"]]
    catch = clean_parts(catch, cfg["multipart_drop_pct"])
    catch = catch.join(sites[["title", "river", "uf", "area_ana_km2"]])
    catch = measure(catch)
    catch = catch.join(res_out[["area", "area_upper", "area_lower"]].rename(columns={
        "area": "area_gcf_km2", "area_upper": "area_upper_km2", "area_lower": "area_lower_km2"}))
    for col in ["area_gcf_km2", "area_upper_km2", "area_lower_km2"]:
        catch[col] = pd.to_numeric(catch[col], errors="coerce")  # None for headwaters -> NaN, not text
    catch["source"] = "gcf"
    catch = gpd.GeoDataFrame(catch, geometry="geometry", crs=OUT_CRS)[[
        "title", "river", "uf", "source", "catchment_source",
        "n_polygons", "n_polygons_raw", "parts_dropped", "area_dropped_km2",
        "area_geod_km2", "area_gcf_km2", "area_upper_km2", "area_lower_km2", "area_ana_km2",
        "area_ratio_ana", "geometry",
    ]].sort_index()
    catch.index.name = "name"

    catch.insert(catch.columns.get_loc("source") + 1, "status_stage02",
                 auto_status(catch, cfg["area_flag_ratio"], cfg["area_flag_bands"]))

    unlinked = gpd.read_file(cfg["outlets_gpkg"], layer="outlets_no_hydrobasin", ignore_geometry=True)["name"]
    attempted = outlets.index
    failed = attempted.difference(catch.index)

    sites["attempted_stage02"] = sites.index.isin(attempted)
    sites["source_stage02"] = None
    sites.loc[catch.index, "source_stage02"] = "gcf"
    sites["status_stage02"] = None
    sites.loc[catch.index, "status_stage02"] = catch["status_stage02"]
    sites.loc[failed, "status_stage02"] = "failed"
    sites.loc[sites.index.isin(unlinked), "status_stage02"] = "no hydrobasin"
    for col in ["area_geod_km2", "area_ratio_ana"]:  # the catchment's area, on the sites
        sites[col] = catch[col].reindex(sites.index)
    found = sites["source_stage02"].notna()
    pending = sites["source_stage01"].isna() & ~found

    if cfg["out_gpkg"].exists():
        cfg["out_gpkg"].unlink()
    out_sites = sites.reset_index()
    out_sites.to_file(cfg["out_gpkg"], layer="sites", driver="GPKG")
    out_sites[found.values].to_file(cfg["out_gpkg"], layer="sites_stage02", driver="GPKG")
    catch.reset_index().to_file(cfg["out_gpkg"], layer="catchments_stage02", driver="GPKG")
    out_sites[pending.values].to_file(cfg["out_gpkg"], layer="sites_pending_stage_02", driver="GPKG")

    write_report(cfg, spec, sites, catch, attempted, failed, res_lower_ids, unlinked)
    print(f"collect: {len(catch)} catchments, {len(failed)} failed, {pending.sum()} still pending overall")
    print(f"collect: {cfg['out_gpkg']}")


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------

def md_table(df: pd.DataFrame) -> list[str]:
    """DataFrame as markdown table lines (index as first column)."""
    head = [df.index.name or ""] + [str(c) for c in df.columns]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for idx, row in df.iterrows():
        lines.append("| " + " | ".join([str(idx)] + [str(v) for v in row]) + " |")
    return lines


def write_report(cfg, spec, sites, catch, attempted, failed, lower_ids, unlinked) -> None:
    f = cfg["area_flag_ratio"]
    lo, hi = 1 / f, f
    r = catch["area_ratio_ana"]
    flagged = catch[(r < lo) | (r > hi)].sort_values("area_ratio_ana")
    multi = catch[catch["n_polygons"] > 1]
    src = catch["catchment_source"].value_counts()
    no_ana = catch["area_ana_km2"].isna() | (catch["area_ana_km2"] <= 0)
    headwater = len(catch) - len(set(catch.index.astype(int)) & set(int(i) for i in lower_ids))

    # Ratio buckets
    bins = [0, 0.5, 0.67, 0.8, 0.9, 1.1, 1.25, 1.5, 2, float("inf")]
    buckets = pd.cut(r.dropna(), bins=bins, right=False).value_counts().sort_index()

    # By area class (ANA)
    cls = pd.cut(catch["area_ana_km2"], [0, 100, 500, 2000, 10000, float("inf")], right=False)
    within10 = ((r >= 0.9) & (r <= 1.1))
    by_cls = pd.DataFrame({"n": cls.value_counts(), "within_10pct": within10.groupby(cls, observed=False).sum(),
                           "median_ratio": r.groupby(cls, observed=False).median()}).sort_index()

    tuning_keys = ["catchment_acceptable_error_pct", "dem_clip_buffer_pct", "dem_snap_search_cells",
                   "raster_only_if_disconnected", "raster_only_if_low_overlap",
                   "raster_vector_overlap_threshold", "headwater_detection", "is_dem_conditioned"]

    L = [
        f"# ANA-Hidro catchments - Stage 2 report ({cfg['run_name']})",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `stage_02.py --config {cfg['config_path'].name}`.",
        "",
        "## Run",
        "",
        f"- States: {', '.join(cfg['ufs']) if cfg['ufs'] else 'all'}",
        f"- Algorithm spec: `{cfg['gcf_spec']}`",
        "- Tuning: " + ", ".join(f"`{k}={spec.get(k)}`" for k in tuning_keys if k in spec),
        "",
        "## Highlights",
        "",
        f"- Pending stations selected: **{len(attempted) + len(unlinked)}**",
        f"- Outside every finest-level basin (not run, kept pending as `no hydrobasin`): **{len(unlinked)}**"
        + (f": {', '.join(unlinked)}" if 0 < len(unlinked) <= 30 else ""),
        f"- Stations sent to the algorithm: **{len(attempted)}**",
        f"- Catchments delineated: **{len(catch)}** ({len(catch) / len(attempted):.1%})",
        f"- Failed (no catchment): **{len(failed)}**" + (f": {', '.join(failed)}" if 0 < len(failed) <= 30 else ""),
        f"- Fine-level headwaters (no lower bound): {headwater}",
        f"- Multi-part catchments from the algorithm: {(catch['n_polygons_raw'] > 1).sum()}; "
        f"cleaned by dropping parts < {cfg['multipart_drop_pct']}% of the area: "
        f"{((catch['n_polygons_raw'] > 1) & (catch['n_polygons'] == 1)).sum()} "
        f"({catch['parts_dropped'].sum()} parts, {catch['area_dropped_km2'].sum():,.2f} km2 in total); "
        f"still multi-part: **{len(multi)}**",
        "",
        "| catchment_source | n |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in src.items()],
        "",
        "| status_stage02 (sites in this run) | n |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sites.loc[sites["status_stage02"].notna(), "status_stage02"].value_counts().items()],
        "",
        "Status by catchment_source:",
        "",
        *md_table(pd.crosstab(catch["catchment_source"], catch["status_stage02"])),
        "",
        "## Area check (geodesic area vs ANA reported drainage area)",
        "",
        f"- Without an ANA area: {no_ana.sum()}",
        f"- Median ratio: {r.median():.3f}; 5-95%: {r.quantile(.05):.3f} - {r.quantile(.95):.3f}",
        f"- Within +-10%: {within10.sum()} ({within10.sum() / r.notna().sum():.1%} of those with an ANA area)",
        f"- Outside [{lo:.2f}, {hi:.2f}]: **{len(flagged)}**",
        "",
        "| ratio | n |",
        "|---|---|",
        *[f"| {iv.left:g} - {iv.right:g} | {n} |" for iv, n in buckets.items()],
        "",
        "By ANA area class (km2):",
        "",
        "| class | n | within +-10% | median ratio |",
        "|---|---|---|---|",
        *[f"| {iv.left:g} - {iv.right:g} | {row.n} | {row.within_10pct} | {row.median_ratio:.3f} |"
          for iv, row in by_cls.iterrows() if row.n],
        "",
        "Ratio by catchment_source:",
        "",
        "| catchment_source | n | median ratio | within +-10% | outside flag |",
        "|---|---|---|---|---|",
    ]
    for s, g in catch.groupby("catchment_source"):
        rr = g["area_ratio_ana"]
        L.append(f"| {s} | {len(g)} | {rr.median():.3f} | {((rr >= .9) & (rr <= 1.1)).sum()} "
                 f"| {((rr < lo) | (rr > hi)).sum()} |")

    L += [
        "",
        f"## Flagged catchments (ratio outside [{lo:.2f}, {hi:.2f}])",
        "",
        "| name | title | river | source | status | n_poly | area geod km2 | area ANA km2 | ratio |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, c in flagged.iterrows():
        L.append(f"| {name} | {c.title} | {c.river} | {c.catchment_source} | {c.status_stage02} | {c.n_polygons} "
                 f"| {c.area_geod_km2:,.1f} | {c.area_ana_km2:,.1f} | {c.area_ratio_ana} |")

    if len(multi):
        L += ["", "## Catchments still multi-part (after dropping minor parts)", "",
              "| name | title | source | status | n_poly (raw) | ratio |", "|---|---|---|---|---|---|"]
        for name, c in multi.iterrows():
            L.append(f"| {name} | {c.title} | {c.catchment_source} | {c.status_stage02} "
                     f"| {c.n_polygons} ({c.n_polygons_raw}) | {c.area_ratio_ana} |")

    cfg["out_report"].write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"collect: report -> {cfg['out_report']}")


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2: delineate pending ANA-Hidro catchments.")
    parser.add_argument("--config", required=True, type=Path, help="path to the JSON config")
    parser.add_argument("--step", choices=["prepare", "run", "collect", "all"], default="all")
    parser.add_argument("--force", action="store_true",
                        help="allow collect to overwrite an existing stage 02 GeoPackage")
    args = parser.parse_args()
    cfg = load_config(args.config)
    print(f"Config: {cfg['config_path']}")
    if args.step in ("prepare", "all"):
        step_prepare(cfg)
    if args.step in ("run", "all"):
        step_run(cfg)
    if args.step in ("collect", "all"):
        step_collect(cfg, force=args.force)
