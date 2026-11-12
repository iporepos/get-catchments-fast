"""
Stage 2b - revision of a state's Stage 2 catchments.

Inputs, per state (same config as stage_02.py):
  <out_dir>/ana_hidro_stage_02_<run>.gpkg   Stage 2 result. You may edit the
                                            vertices of catchments_stage02 in
                                            QGIS (or delete a catchment); this
                                            script never writes to this file.
  <out_dir>/revision_stage02b.toml          your revision (create the empty file
                                            with init_revision_stage02b.py):

      [status]                  # set the final status, whatever it is now
      "ok"      = ["87010000"]
      "pending" = ["87450000"]  # drop the catchment, back to pending

      [force]                   # re-run get_catchments_fast with these options
      force_headwaters = ["86710000"]
      force_upper      = ["85200000"]
      force_trunk      = []

      [copy]                    # target = [source codes]; several are dissolved
      "87510000" = ["87510001"]
      "70700000" = ["70690000", "70695000"]

      [replace]                 # target = [polygon files]; several are dissolved
      "86340000" = ["edits/86340000.fgb"]

      [outlet]                  # code = point file: re-run the algorithm from this point
      "87510000" = "edits/87510000_outlet.fgb"

Rules (violations stop the script):
  - codes are ANA station codes (quoted or bare);
  - [status] keys: ok, probable overestimate, probable underestimate,
    multiparts, no ana area, pending;
  - a code appears at most once in [status];
  - a code is the target of only one of [copy], [replace], and of neither of
    them if it is in [force] or [outlet]; [force] and [outlet] may be combined
    (a new point re-run with a force option);
  - every curated geometry becomes "ok" (status_origin = revision): manual
    edits, [copy], [replace], [force] and moved outlets ([outlet] or a point
    moved in place); a failed re-run is not curated. [status] overrides any of
    them. status_auto and area_ratio_ana keep what the area check says;
  - force_headwaters and force_trunk exclude each other;
  - a [force]/[copy]/[replace] target must not have manual edits (they would be
    discarded);
  - [status] codes must have a catchment in Stage 2; forced codes must be
    outlets of this run; [copy]/[replace] targets must be Stage 2 stations of
    this state (with or without a catchment - e.g. "no hydrobasin" stations);
  - [copy] sources are looked up in this state (final geometry: edits and
    re-runs included), then in the other states' Stage 2 files (as edited), then
    in Stage 1 (catchments_stage01). A source must not be a [copy]/[replace]
    target itself (no chains);
  - [replace] files: paths relative to the revision file's folder; each holds
    exactly one feature, a single-part polygon with a CRS (any vector format
    GDAL reads, FlatGeobuf expected).
  - [outlet] files: one per code, path relative to the revision file's folder,
    exactly one point feature with a CRS. The code must be a Stage 2 outlet of
    this run or a "no hydrobasin" station (a moved point can fix those).
  - a station point moved in place in the Stage 2 file (layers sites_stage02,
    sites, sites_pending_stage_02) is detected against the ANA point of Stage 1
    and works exactly like an [outlet] entry; moving it in two layers to
    different places, or moving it and also listing it in [outlet], is an error.

What it does:
  1. detects manual edits: each Stage 2 catchment is compared with the
     algorithm's own output (cleaned the same way); differences -> manual edit,
     a deleted catchment -> pending;
  2. re-runs get_catchments_fast for the forced and moved codes only, in
     <intermediate>/<run>/rerun_02b/ (moved codes from their new point), and
     takes their new geometry; moved points are written to layer outlets_moved;
  2b. applies [copy] and [replace] (union + dissolve of the listed geometries);
  3. recomputes area, ratio and status_auto from the final geometry;
  4. status_stage02b = the [status] entry when listed (status_origin = manual),
     else "ok" for any curated geometry - edits, copies, replacements, forced
     and moved-outlet re-runs (origin = revision),
     else status_auto (origin = auto). status_auto always keeps what the area
     check says about the final geometry;
  5. writes <out_dir>/ana_hidro_stage_02b_<run>.gpkg (styled) and
     <out_dir>/report_stage_02b_<run>.md.

Layers: sites, sites_stage02b, catchments_stage02b, sites_pending_stage_02b,
outlets_moved (when [outlet] is used), and the catchments split by final
status: catchments_stage02b_ok, catchments_stage02b_probable_underestimate, ...

Usage:
    python stage_02b.py --config specs/stage_02/43_rs.json
"""
import argparse
import json
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon, Polygon

from stage_02 import (OUT_CRS, OUTLET_FIELD, auto_status, clean_parts, flag_ratios, load_config,
                      measure, md_table)
from style_gpkg import style_gpkg

AUTO_STATUSES = ["ok", "probable overestimate", "probable underestimate", "multiparts", "no ana area"]
MANUAL_STATUSES = AUTO_STATUSES + ["pending"]
FORCE_KEYS = ["force_headwaters", "force_upper", "force_trunk"]
EDIT_TOLERANCE_DEG = 1e-9



def status_layer(status: str) -> str:
    """Layer name for the catchments of one final status."""
    return "catchments_stage02b_" + "_".join(str(status).split())


# ---------------------------------------------------------------------
# Revision file
# ---------------------------------------------------------------------

def norm_code(c) -> str:
    s = str(c).strip()
    if not s.isdigit():
        raise ValueError(f"not an ANA code: {c!r}")
    return s.zfill(8)


SECTIONS = ["status", "force", "copy", "replace", "outlet"]


def load_revision(path: Path) -> tuple[dict, dict, dict, dict, dict]:
    """Return ({code: status}, {force_key: [codes]}, {target: [codes]}, {target: [Path]}, {code: Path})."""
    with open(path, "rb") as f:
        rev = tomllib.load(f)
    unknown = sorted(set(rev) - set(SECTIONS))
    if unknown:
        raise ValueError(f"{path.name}: unknown section(s) {unknown}; use {['[' + s + ']' for s in SECTIONS]}")

    status, errors = {}, []
    for st, codes in rev.get("status", {}).items():
        if st not in MANUAL_STATUSES:
            errors.append(f"[status] unknown status {st!r} (allowed: {MANUAL_STATUSES})")
            continue
        for c in codes:
            c = norm_code(c)
            if c in status:
                errors.append(f"[status] {c} listed under both {status[c]!r} and {st!r}")
            status[c] = st

    force = {k: [] for k in FORCE_KEYS}
    for k, codes in rev.get("force", {}).items():
        if k not in FORCE_KEYS:
            errors.append(f"[force] unknown key {k!r} (allowed: {FORCE_KEYS})")
            continue
        force[k] = [norm_code(c) for c in codes]
        dup = sorted({c for c in force[k] if force[k].count(c) > 1})
        if dup:
            errors.append(f"[force] {k}: repeated code(s) {dup}")

    both = sorted(set(force["force_headwaters"]) & set(force["force_trunk"]))
    if both:
        errors.append(f"[force] code(s) in both force_headwaters and force_trunk: {both}")

    def targets(section, convert):
        out = {}
        for t, items in rev.get(section, {}).items():
            try:
                t = norm_code(t)
            except ValueError as e:
                errors.append(f"[{section}] target: {e}")
                continue
            if isinstance(items, (str, int)) and str(items).strip():
                items = [items]  # a single source may be written without brackets
            if not isinstance(items, list) or not items:
                errors.append(f"[{section}] {t}: expected a non-empty list")
                continue
            out[t] = [convert(i) for i in items]
        return out

    copy = targets("copy", norm_code)
    replace = targets("replace", lambda p: (path.parent / str(p)).resolve())
    outlet = {}
    for t, files in targets("outlet", lambda p: (path.parent / str(p)).resolve()).items():
        if len(files) != 1:
            errors.append(f"[outlet] {t}: expected one point file, got {len(files)}")
            continue
        outlet[t] = files[0]
    for t, srcs in copy.items():
        if t in srcs:
            errors.append(f"[copy] {t} copies itself")

    forced = set().union(*force.values())
    groups = {"[force]/[outlet]": forced | set(outlet), "[copy]": set(copy), "[replace]": set(replace)}
    names = list(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            clash = sorted(groups[a] & groups[b])
            if clash:
                errors.append(f"code(s) in both {a} and {b} (one source of geometry per code): {clash}")
    chained = sorted({s for srcs in copy.values() for s in srcs} & (set(copy) | set(replace)))
    if chained:
        errors.append(f"[copy] source(s) that are themselves [copy]/[replace] targets (no chains): {chained}")
    if errors:
        raise ValueError(f"{path}:\n  - " + "\n  - ".join(errors))
    return status, force, copy, replace, outlet


# ---------------------------------------------------------------------
# Copy / replace geometry
# ---------------------------------------------------------------------

def dissolve(geoms):
    g = shapely.union_all([shapely.make_valid(x) for x in geoms])
    g = shapely.make_valid(g)
    if g.geom_type == "GeometryCollection":  # keep the polygonal part only
        g = shapely.union_all([p for p in g.geoms if p.geom_type in ("Polygon", "MultiPolygon")])
    return g


def read_polygon_file(path: Path):
    """One feature, single-part polygon, with a CRS -> geometry in OUT_CRS."""
    if not path.exists():
        raise ValueError(f"[replace] file not found: {path}")
    gdf = gpd.read_file(path)
    if len(gdf) != 1:
        raise ValueError(f"[replace] {path.name}: expected 1 feature, found {len(gdf)}")
    if gdf.crs is None:
        raise ValueError(f"[replace] {path.name}: no CRS")
    g = gdf.geometry.iloc[0]
    if isinstance(g, MultiPolygon) and len(g.geoms) == 1:
        g = g.geoms[0]
    if not isinstance(g, Polygon):
        raise ValueError(f"[replace] {path.name}: expected a single-part polygon, found "
                         f"{g.geom_type}" + (f" with {len(g.geoms)} parts" if hasattr(g, "geoms") else ""))
    if not g.is_valid:
        print(f"note: [replace] {path.name}: invalid polygon repaired with make_valid")
    return gpd.GeoSeries([g], crs=gdf.crs).to_crs(OUT_CRS).iloc[0]


def read_point_file(path: Path):
    """One point feature with a CRS -> point in EPSG:4326 (the CRS of the outlets)."""
    if not path.exists():
        raise ValueError(f"[outlet] file not found: {path}")
    gdf = gpd.read_file(path)
    if len(gdf) != 1:
        raise ValueError(f"[outlet] {path.name}: expected 1 feature, found {len(gdf)}")
    if gdf.crs is None:
        raise ValueError(f"[outlet] {path.name}: no CRS")
    g = gdf.geometry.iloc[0]
    if g.geom_type == "MultiPoint" and len(g.geoms) == 1:
        g = g.geoms[0]
    if g.geom_type != "Point":
        raise ValueError(f"[outlet] {path.name}: expected a point, found {g.geom_type}")
    return gpd.GeoSeries([g], crs=gdf.crs).to_crs("EPSG:4326").iloc[0]


SITE_LAYERS_02 = ["sites_stage02", "sites", "sites_pending_stage_02"]
MOVE_TOLERANCE_DEG = 1e-9


def detect_moved_points(stage01_gpkg: Path, stage02_gpkg: Path, codes) -> tuple[dict, list]:
    """Station points moved in place in the Stage 2 file, vs the ANA point of Stage 1.

    Returns ({code: (point in OUT_CRS, layer)}, errors).
    """
    ref = gpd.read_file(stage01_gpkg, layer="sites", columns=["name"]).set_index("name").to_crs(OUT_CRS)
    present = set(pyogrio.list_layers(stage02_gpkg)[:, 0])
    found, errors = {}, []
    for layer in SITE_LAYERS_02:
        if layer not in present:
            continue
        g = gpd.read_file(stage02_gpkg, layer=layer, columns=["name"]).set_index("name").to_crs(OUT_CRS)
        g = g[g.index.isin(ref.index)]
        for c, pt in g.geometry.items():
            if pt is None or pt.equals_exact(ref.geometry[c], MOVE_TOLERANCE_DEG):
                continue
            if c in found and not found[c][0].equals_exact(pt, MOVE_TOLERANCE_DEG):
                errors.append(f"station {c} moved to different places in {found[c][1]} and {layer}")
                continue
            found.setdefault(c, (pt, layer))
    bad = sorted(set(found) - set(codes))
    if bad:
        errors.append(f"station point(s) moved that are not Stage 2 stations of this state: {bad}")
    return found, errors


class SourceLookup:
    """Find a catchment geometry by ANA code: this state, other states' Stage 2, Stage 1."""

    def __init__(self, cfg: dict, own: gpd.GeoDataFrame):
        self.cfg, self.own = cfg, own
        self._others = None
        self._stage1 = None

    def _load(self):
        if self._others is None:
            frames = []
            for f in sorted(self.cfg["out_dir"].parent.glob("*/ana_hidro_stage_02_*.gpkg")):
                if f.resolve() == self.cfg["out_gpkg"].resolve():
                    continue
                g = gpd.read_file(f, layer="catchments_stage02", columns=["name"]).to_crs(OUT_CRS)
                g["from"] = f"stage 02 {f.parent.name}"
                frames.append(g)
            self._others = (pd.concat(frames).drop_duplicates("name").set_index("name") if frames
                            else gpd.GeoDataFrame(columns=["from", "geometry"], geometry="geometry"))
            g = gpd.read_file(self.cfg["stage01_gpkg"], layer="catchments_stage01",
                              columns=["name", "source"]).to_crs(OUT_CRS)
            g["from"] = "stage 01 " + g["source"]
            self._stage1 = g.set_index("name")

    def get(self, code: str):
        if code in self.own.index:
            return self.own.geometry[code], f"stage 02b {self.cfg['run_name']}"
        self._load()
        for table in (self._others, self._stage1):
            if code in table.index:
                return table.geometry[code], table.at[code, "from"]
        return None, None


# ---------------------------------------------------------------------
# Geometry comparison
# ---------------------------------------------------------------------

def _norm_geom(g):
    if isinstance(g, MultiPolygon) and len(g.geoms) == 1:
        g = g.geoms[0]
    return g.normalize()


def detect_edits(current: gpd.GeoDataFrame, original: gpd.GeoDataFrame) -> pd.Series:
    """True where the current geometry differs from the algorithm's (cleaned) geometry."""
    common = current.index.intersection(original.index)
    edited = pd.Series(False, index=current.index)
    for c in common:
        a, b = _norm_geom(current.geometry[c]), _norm_geom(original.geometry[c])
        edited[c] = not a.equals_exact(b, EDIT_TOLERANCE_DEG)
    return edited


# ---------------------------------------------------------------------
# Forced re-run
# ---------------------------------------------------------------------

def rerun_forced(cfg: dict, force: dict, points: dict | None = None) -> tuple[gpd.GeoDataFrame, pd.DataFrame, list]:
    """Run get_catchments_fast for the forced and moved codes only.

    points: {code: new outlet point (EPSG:4326)} for [outlet] codes.
    Returns (catchments, outlets, failed).
    """
    points = points or {}
    codes = sorted(set().union(*force.values()) | set(points))
    rerun_dir = cfg["run_dir"] / "rerun_02b"
    rerun_dir.mkdir(parents=True, exist_ok=True)

    outlets = pd.concat([gpd.read_file(cfg["outlets_gpkg"], layer=lyr)
                         for lyr in ("outlets", "outlets_no_hydrobasin")], ignore_index=True)
    sub = outlets[outlets["name"].isin(codes)].copy()
    for c, pt in points.items():
        sub.loc[sub["name"] == c, "geometry"] = pt
    outlets_gpkg = rerun_dir / "outlets.gpkg"
    if outlets_gpkg.exists():
        outlets_gpkg.unlink()
    sub.to_file(outlets_gpkg, layer="outlets", driver="GPKG")

    with open(cfg["gcf_spec"], encoding="utf-8") as f:
        spec = json.load(f)
    spec.update({
        "dst_dir": str(rerun_dir / "gcf"),
        "outlets_db": str(outlets_gpkg),
        "outlets_layer": "outlets",
        "outlet_field": OUTLET_FIELD,
        "discrete_output": None,
    })
    for k in FORCE_KEYS:
        spec[k] = [int(c) for c in force[k]]
    spec_path = rerun_dir / "gcf_spec.json"
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=4, ensure_ascii=False)

    log_path = rerun_dir / "rerun.log"
    print(f"rerun: {len(codes)} code(s) ({len(points)} with a moved outlet), log -> {log_path}")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.run([sys.executable, str(cfg["gcf_script"]), "--spec", str(spec_path)],
                              stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"get_catchments_fast exited with {proc.returncode}; see {log_path}")

    agg = Path(spec["dst_dir"]) / spec.get("aggregated_output", "catchments.gpkg")
    if not agg.exists():
        return gpd.GeoDataFrame(columns=["catchment_source", "geometry"], geometry="geometry",
                                crs=OUT_CRS), pd.DataFrame(), codes
    catch = gpd.read_file(agg, layer="catchment").to_crs(OUT_CRS)
    res_out = gpd.read_file(agg, layer="outlets", ignore_geometry=True)
    for df in (catch, res_out):
        df["name"] = df[OUTLET_FIELD].astype(int).astype(str).str.zfill(8)
    catch = catch.set_index("name")[["catchment_source", "geometry"]]
    res_out = res_out.set_index("name")[["area", "area_upper", "area_lower"]].rename(columns={
        "area": "area_gcf_km2", "area_upper": "area_upper_km2", "area_lower": "area_lower_km2"})
    failed = sorted(set(codes) - set(catch.index))
    return catch, res_out, failed


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main(config: Path) -> None:
    cfg = load_config(config)
    run = cfg["run_name"]
    out_dir = cfg["out_dir"]
    stage02_gpkg = cfg["out_gpkg"]
    out_gpkg = out_dir / f"ana_hidro_stage_02b_{run}.gpkg"
    out_report = out_dir / f"report_stage_02b_{run}.md"
    toml_path = out_dir / "revision_stage02b.toml"
    flag = cfg["area_flag_ratio"]

    if not toml_path.exists():
        raise FileNotFoundError(f"{toml_path} not found; create it with: "
                                f"python init_revision_stage02b.py {Path(config)}")
    manual_status, force, copy, replace, outlet = load_revision(toml_path)
    forced = set().union(*force.values())
    retargeted = set(copy) | set(replace)

    # Stage 2 (possibly edited) and the algorithm's own output, cleaned the same way
    current = gpd.read_file(stage02_gpkg, layer="catchments_stage02").set_index("name").to_crs(OUT_CRS)
    num_cols = ["n_polygons", "n_polygons_raw", "parts_dropped", "area_dropped_km2", "area_geod_km2",
                "area_gcf_km2", "area_upper_km2", "area_lower_km2", "area_ana_km2", "area_ratio_ana"]
    for col in num_cols:  # older Stage 2 files store area_lower_km2 as text
        current[col] = pd.to_numeric(current[col], errors="coerce")
    sites = gpd.read_file(stage02_gpkg, layer="sites").set_index("name")
    with open(cfg["gcf_spec"], encoding="utf-8") as f:
        spec = json.load(f)
    agg = Path(spec["dst_dir"]) / spec.get("aggregated_output", "catchments.gpkg")
    original = gpd.read_file(agg, layer="catchment").to_crs(OUT_CRS)
    original["name"] = original[OUTLET_FIELD].astype(int).astype(str).str.zfill(8)
    original = clean_parts(original.set_index("name")[["geometry"]], cfg["multipart_drop_pct"])

    edited = detect_edits(current, original)
    produced = set(sites.index[sites["source_stage02"].notna()])
    deleted = sorted(produced - set(current.index))
    outlet_codes = set(gpd.read_file(cfg["outlets_gpkg"], layer="outlets", ignore_geometry=True)["name"])
    unlinked_codes = set(gpd.read_file(cfg["outlets_gpkg"], layer="outlets_no_hydrobasin",
                                       ignore_geometry=True)["name"])

    # Station points moved in place in the Stage 2 file act like [outlet] entries
    moved, errors = detect_moved_points(cfg["stage01_gpkg"], stage02_gpkg, outlet_codes | unlinked_codes)
    for name, codes in (("[outlet]", set(outlet)), ("[copy]", set(copy)), ("[replace]", set(replace))):
        clash = sorted(set(moved) & codes)
        if clash:
            errors.append(f"station point(s) moved in the Stage 2 file and also listed in {name}: {clash}")
    outlet_label = {c: pth.name for c, pth in outlet.items()}
    outlet_label.update({c: f"moved in {layer}" for c, (_, layer) in moved.items()})
    rerun_codes = forced | set(outlet_label)

    # Cross-checks that need the data
    bad = sorted(set(manual_status) - set(current.index))
    if bad:
        errors.append(f"[status] code(s) without a Stage 2 catchment in {run}: {bad}")
    bad = sorted((forced - set(outlet_label)) - outlet_codes)
    if bad:
        errors.append(f"[force] code(s) that are not outlets of {run} (a no-hydrobasin station needs "
                      f"an [outlet] point): {bad}")
    bad = sorted(set(outlet) - outlet_codes - unlinked_codes)
    if bad:
        errors.append(f"[outlet] code(s) that are not Stage 2 stations of {run}: {bad}")
    bad = sorted(c for c in rerun_codes if c in edited.index and edited[c])
    if bad:
        errors.append(f"[force]/[outlet] code(s) with manual edits (the re-run would discard them): {bad}")
    points = {}
    for c, pth in outlet.items():
        try:
            points[c] = read_point_file(pth)
        except ValueError as e:
            errors.append(str(e))
    for c, (pt, _) in moved.items():
        points[c] = gpd.GeoSeries([pt], crs=OUT_CRS).to_crs("EPSG:4326").iloc[0]
    bad = sorted(c for c in retargeted if c in edited.index and edited[c])
    if bad:
        errors.append(f"[copy]/[replace] target(s) with manual edits (they would be discarded): {bad}")
    stage02_codes = set(sites.index[sites["source_stage01"].isna()])
    bad = sorted(retargeted - stage02_codes)
    if bad:
        errors.append(f"[copy]/[replace] target(s) that are not Stage 2 stations of {run}: {bad}")
    if errors:
        raise ValueError(f"{toml_path}:\n  - " + "\n  - ".join(errors))

    # Final geometry: current (edited or not), replaced by the re-run for forced codes
    catch = current.copy()
    catch["status_stage02"] = catch["status_stage02"]
    catch["area_geod_stage02_km2"] = catch["area_geod_km2"]
    catch["revision"] = ""
    catch.loc[edited[edited].index, "revision"] = "manual edit"
    rerun_failed = []
    if rerun_codes:
        new, new_out, rerun_failed = rerun_forced(cfg, force, points)
        new_out = new_out.apply(pd.to_numeric, errors="coerce")
        new = clean_parts(new, cfg["multipart_drop_pct"])
        ok_codes = new.index
        for c in ok_codes:  # moved no-hydrobasin stations get a row
            if c not in catch.index:
                srow = sites.loc[c]
                catch.loc[c, ["title", "river", "uf", "area_ana_km2", "status_stage02", "source", "revision"]] = [
                    srow["title"], srow["river"], srow["uf"], srow["area_ana_km2"], srow["status_stage02"], "gcf", ""]
        for col in ["catchment_source", "n_polygons_raw", "parts_dropped", "area_dropped_km2"]:
            catch.loc[ok_codes, col] = new[col]
        catch.loc[ok_codes, "geometry"] = new.geometry
        if len(new_out):
            for col in ["area_gcf_km2", "area_upper_km2", "area_lower_km2"]:
                catch.loc[ok_codes, col] = new_out.loc[ok_codes, col]
        labels = {c: [] for c in rerun_codes}
        for c in outlet_label:
            labels[c].append(f"outlet {outlet_label[c]}")
        for k in FORCE_KEYS:
            for c in force[k]:
                labels[c].append(k)
        for c, parts in labels.items():
            if c in catch.index:
                catch.loc[c, "revision"] = " + ".join(parts) + (
                    "" if c in ok_codes else " (re-run failed, Stage 2 kept)")
    catch = gpd.GeoDataFrame(catch, geometry="geometry", crs=OUT_CRS)

    # Copy / replace: union + dissolve of the listed geometries
    if retargeted:
        lookup = SourceLookup(cfg, catch)
        new_geoms, notes, errors = {}, {}, []
        for t, srcs in copy.items():
            geoms, froms = [], []
            for s_ in srcs:
                g, where = lookup.get(s_)
                if g is None:
                    errors.append(f"[copy] {t}: source {s_} has no catchment in this state, "
                                  f"the other states' Stage 2 or Stage 1")
                    continue
                geoms.append(g)
                froms.append(f"{s_} ({where})")
            new_geoms[t] = ("copy", geoms, "copy from " + " + ".join(froms))
        for t, paths in replace.items():
            try:
                geoms = [read_polygon_file(pth) for pth in paths]
            except ValueError as e:
                errors.append(str(e))
                continue
            new_geoms[t] = ("replace", geoms, "replace from " + " + ".join(p.name for p in paths))
        if errors:
            raise ValueError(f"{toml_path}:\n  - " + "\n  - ".join(errors))

        for t, (kind, geoms, note) in new_geoms.items():
            if t not in catch.index:  # station without a Stage 2 catchment (e.g. no hydrobasin)
                srow = sites.loc[t]
                catch.loc[t, ["title", "river", "uf", "area_ana_km2", "status_stage02"]] = [
                    srow["title"], srow["river"], srow["uf"], srow["area_ana_km2"], srow["status_stage02"]]
            g = dissolve(geoms)
            catch.loc[t, "geometry"] = g
            catch.loc[t, ["source", "catchment_source", "revision"]] = [kind, kind, note]
            catch.loc[t, ["n_polygons_raw", "parts_dropped", "area_dropped_km2"]] = [
                len(g.geoms) if hasattr(g, "geoms") else 1, 0, 0.0]
            catch.loc[t, ["area_gcf_km2", "area_upper_km2", "area_lower_km2"]] = [None, None, None]
        deleted = [d for d in deleted if d not in new_geoms]
        catch = gpd.GeoDataFrame(catch, geometry="geometry", crs=OUT_CRS)

    # Measure and classify
    catch = measure(catch)
    catch["status_auto"] = auto_status(catch, flag, cfg["area_flag_bands"])
    # the tolerance actually applied to each station (size bands on the ANA area)
    catch["area_flag_ratio"] = flag_ratios(catch["area_ana_km2"], flag, cfg["area_flag_bands"])
    catch["status_stage02b"] = catch["status_auto"]
    catch["status_origin"] = "auto"
    # every curated geometry is ok: edits, copies, replacements, forced / moved-outlet re-runs
    # (a failed re-run keeps the Stage 2 geometry, so it is not curated)
    curated = (catch["revision"].ne("") & ~catch["revision"].str.contains("re-run failed", regex=False))
    catch.loc[curated, "status_stage02b"] = "ok"
    catch.loc[curated, "status_origin"] = "revision"
    for c, st in manual_status.items():
        catch.loc[c, "status_stage02b"] = st
        catch.loc[c, "status_origin"] = "manual"

    to_pending = sorted(c for c, st in manual_status.items() if st == "pending")
    final = catch.drop(index=to_pending)
    final = final[[
        "title", "river", "uf", "source", "status_stage02", "status_auto", "status_stage02b",
        "status_origin", "revision", "catchment_source",
        "n_polygons", "n_polygons_raw", "parts_dropped", "area_dropped_km2",
        "area_geod_km2", "area_geod_stage02_km2", "area_gcf_km2", "area_upper_km2", "area_lower_km2",
        "area_ana_km2", "area_ratio_ana", "area_flag_ratio", "geometry",
    ]].sort_index()
    final.index.name = "name"

    # Sites
    sites["source_stage02b"] = None
    sites.loc[final.index, "source_stage02b"] = final["source"]
    sites["status_stage02b"] = sites["status_stage02"]
    sites.loc[catch.index, "status_stage02b"] = catch["status_stage02b"]
    sites.loc[deleted, "status_stage02b"] = "pending"
    sites.loc[catch.index, "status_stage02"] = catch["status_stage02"]
    sites["status_origin"] = None
    sites.loc[catch.index, "status_origin"] = catch["status_origin"]
    sites.loc[deleted, "status_origin"] = "manual"
    # the final catchment's area, tolerance and method, on the sites
    for col in ["area_geod_km2", "area_ratio_ana", "area_flag_ratio", "catchment_source", "revision"]:
        sites[col] = final[col].reindex(sites.index)
    found = sites["source_stage02b"].notna()
    pending = sites["source_stage01"].isna() & ~found

    if out_gpkg.exists():
        out_gpkg.unlink()
    out_sites = sites.reset_index()
    out_sites.to_file(out_gpkg, layer="sites", driver="GPKG")
    out_sites[found.values].to_file(out_gpkg, layer="sites_stage02b", driver="GPKG")
    final.reset_index().to_file(out_gpkg, layer="catchments_stage02b", driver="GPKG")
    # one layer per final status, for visual inspection (empty statuses are skipped)
    for st, part in final.groupby("status_stage02b"):
        part.reset_index().to_file(out_gpkg, layer=status_layer(st), driver="GPKG")
    out_sites[pending.values].to_file(out_gpkg, layer="sites_pending_stage_02b", driver="GPKG")
    if points:
        moved = gpd.GeoDataFrame(
            {"name": list(points), "title": [sites.at[c, "title"] for c in points],
             "file": [outlet_label[c] for c in points],
             "rerun_ok": [c not in rerun_failed for c in points]},
            geometry=list(points.values()), crs="EPSG:4326").to_crs(OUT_CRS)
        ana_pts = sites.geometry.loc[moved["name"]].to_crs("EPSG:5880").values
        moved["distance_km"] = (moved.to_crs("EPSG:5880").distance(
            gpd.GeoSeries(ana_pts, crs="EPSG:5880"), align=False) / 1000).round(3).values
        moved.to_file(out_gpkg, layer="outlets_moved", driver="GPKG")
    style_gpkg(out_gpkg)

    write_report(out_report, run, toml_path, catch, final, manual_status, force, edited,
                 deleted, to_pending, rerun_failed, flag, copy, replace, outlet_label)
    print(f"stage 02b: {len(final)} catchments ({edited.sum()} edited, {len(forced)} forced, "
          f"{len(outlet_label)} outlet moved, "
          f"{len(copy)} copied, {len(replace)} replaced, "
          f"{len(manual_status)} manual status, {len(deleted) + len(to_pending)} to pending)")
    print(f"stage 02b: {out_gpkg}")
    print(f"stage 02b: report -> {out_report}")


# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------

def write_report(path, run, toml_path, catch, final, manual_status, force, edited,
                 deleted, to_pending, rerun_failed, flag, copy, replace, outlet) -> None:
    def num(v):
        return "" if pd.isna(v) else f"{v:,.1f}"

    def row(c, r):
        return (f"| {c} | {r.title} | {r.status_stage02} | {r.status_auto} | {r.status_stage02b} | "
                f"{num(r.area_geod_stage02_km2)} | {num(r.area_geod_km2)} | {num(r.area_ana_km2)} |")

    head = ["| name | title | status 02 | status_auto | status 02b | area 02 km2 | area 02b km2 | area ANA km2 |",
            "|---|---|---|---|---|---|---|---|"]
    trans = pd.crosstab(catch["status_stage02"], catch["status_stage02b"])

    L = [
        f"# ANA-Hidro catchments - Stage 2b revision ({run})",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `stage_02b.py`, from `{toml_path.name}`.",
        "",
        "## Highlights",
        "",
        f"- Catchments in Stage 2b: **{len(final)}**",
        f"- Manual vertex edits detected: **{int(edited.sum())}**",
        f"- Forced re-runs: " + ", ".join(f"{k} {len(v)}" for k, v in force.items())
        + (f"; failed (Stage 2 geometry kept): {rerun_failed}" if rerun_failed else ""),
        f"- Outlets moved (re-run from a new point): **{len(outlet)}**",
        f"- Copied from other stations: **{len(copy)}**; replaced from files: **{len(replace)}**",
        f"- Set to ok as curated geometry (edit/copy/replace): **{int((catch['status_origin'] == 'revision').sum())}**",
        f"- Manual status entries: **{len(manual_status)}**",
        f"- Sent to pending: {len(to_pending)} by [status]" + (f", {len(deleted)} deleted in QGIS: {deleted}"
                                                               if deleted else ""),
        "",
        "| status_stage02b | n |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in final["status_stage02b"].value_counts().items()],
        "",
        "Status change (rows Stage 2, columns Stage 2b):",
        "",
        *md_table(trans),
    ]

    ed = catch[catch["revision"] == "manual edit"]
    if len(ed):
        L += ["", "## Manual edits", "", *head, *[row(c, r) for c, r in ed.iterrows()]]

    sub = catch[catch["revision"].str.startswith("outlet")]
    if len(sub):
        L += ["", "## outlet (re-run from a moved point)", "",
              head[0] + " revision |", head[1] + "---|", *[row(c, r) + f" {r.revision} |" for c, r in sub.iterrows()]]
    for k in FORCE_KEYS:
        sub = catch[catch["revision"].str.startswith(k)]
        if len(sub):
            L += ["", f"## {k}", "", *head, *[row(c, r) for c, r in sub.iterrows()]]

    for kind in ("copy", "replace"):
        sub = catch[catch["revision"].str.startswith(kind)]
        if len(sub):
            L += ["", f"## {kind}", "",
                  "| name | title | status 02 | status_auto | status 02b | area 02 km2 | area 02b km2 "
                  "| area ANA km2 | from |",
                  "|---|---|---|---|---|---|---|---|---|"]
            for c, r in sub.iterrows():
                L.append(row(c, r) + f" {r.revision} |")

    man = catch[catch["status_origin"] == "manual"]
    if len(man):
        L += ["", "## Manual status (status_auto is what the geometry says)", "",
              "| name | title | status_auto | status 02b | agrees | area km2 | area ANA km2 |",
              "|---|---|---|---|---|---|---|"]
        for c, r in man.sort_values("status_stage02b").iterrows():
            agree = "yes" if r.status_auto == r.status_stage02b else "**no**"
            ana = "" if pd.isna(r.area_ana_km2) else f"{r.area_ana_km2:,.1f}"
            L.append(f"| {c} | {r.title} | {r.status_auto} | {r.status_stage02b} | {agree} "
                     f"| {r.area_geod_km2:,.1f} | {ana} |")

    path.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2b: revise a state's Stage 2 catchments.")
    parser.add_argument("--config", required=True, type=Path, help="the state's stage_02 config")
    args = parser.parse_args()
    main(args.config)
