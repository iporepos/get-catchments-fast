"""
Stage 3 - merge Stage 1 and the Stage 2b results of all runs into one GeoPackage.

Read only: this script does not touch the ANA database (appending into it is
for a later stage). It only reads:
  <stage01_gpkg>                                   catchments_stage01, sites
  <stage02_dir>/<run>/ana_hidro_stage_02b_<run>.gpkg   catchments_stage02b, sites,
                                                       outlets_moved (if any)

Config (JSON, paths relative to the config's folder):
  stage01_gpkg       Stage 1 GeoPackage
  stage02_dir        folder holding one sub-folder per Stage 2 run
  out_dir            output folder
  out_gpkg           output GeoPackage name     (default ana_hidro_stage_03.gpkg)
  out_report         report name                (default report_stage_03.md)
  area_flag_ratio    tolerance of the area check (same as Stage 2)
  area_flag_bands    looser tolerance by ANA area (same as Stage 2)
  multipart_drop_pct drop Stage 1 polygon parts under this % of the area, as
                     Stage 2 does (default 5.0; published polygons carry slivers)

Sites and catchments carry the SAME attribute columns, so a station and its
catchment can be matched at a glance (and styled the same way):

  name, title, river, uf, operating, telemetric
  stage            stage01 | stage02b | none (no catchment)
  run              Stage 2 run (e.g. 43_rs); Stage 1 stations get the run of their state
  source           cabra, camelsbr (Stage 1) | gcf, copy, replace (Stage 2b)
  catchment_source algorithm detail (Stage 2b) | source dataset (Stage 1)
  revision         Stage 2b revision note | Stage 1: "minor parts dropped (...)" ("" if none)
  status_stage03   final status
  status_auto      what the area check says about the final geometry
  status_origin    auto | revision | manual (from Stage 2b) | auto (Stage 1)
  has_catchment    true / false
  area_ana_km2, area_geod_km2, area_ratio_ana, area_flag_ratio, n_polygons
  area_source_km2  area reported by the published dataset (Stage 1 only)

Final status:
  Stage 2b catchments  status_stage02b (already reviewed)
  Stage 1 catchments   the area check, with the same tolerance as Stage 2
                       (published catchments are not reviewed by hand)
  no catchment         the station's Stage 2b status (e.g. "no hydrobasin", "pending")

Layers of <out_dir>/<out_gpkg> (rebuilt on every run, styled):
  sites_stage03                    all stations (ANA points)
  catchments_stage03               all catchments
  catchments_stage03_<status>      the same, one layer per final status
  sites_pending_stage_03           stations without a catchment
  outlets_moved                    moved outlet points of all runs (Stage 2b)

Usage:
    python stage_03.py --config specs/stage_03.json
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from stage_02 import OUT_CRS, auto_status, clean_parts, flag_ratios, md_table, measure
from style_gpkg import style_gpkg

DEFAULTS = {
    "out_gpkg": "ana_hidro_stage_03.gpkg",
    "out_report": "report_stage_03.md",
    "area_flag_bands": None,
    "multipart_drop_pct": 5.0,
}
REQUIRED = ["stage01_gpkg", "stage02_dir", "out_dir", "area_flag_ratio"]

COLUMNS = [
    "name", "title", "river", "uf", "operating", "telemetric",
    "stage", "run", "source", "catchment_source", "revision",
    "status_stage03", "status_auto", "status_origin", "has_catchment",
    "area_ana_km2", "area_geod_km2", "area_ratio_ana", "area_flag_ratio", "n_polygons",
    "area_source_km2",
]
STATUS_ORDER = ["ok", "no ana area", "multiparts", "probable overestimate",
                "probable underestimate", "no hydrobasin", "failed", "pending"]


def load_config(path: Path) -> dict:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    missing = [k for k in REQUIRED if cfg.get(k) is None]
    if missing:
        raise ValueError(f"{path}: missing key(s) {missing}")
    unknown = sorted(set(cfg) - set(REQUIRED) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"{path}: unknown key(s) {unknown}")
    cfg = {**DEFAULTS, **cfg}

    def p(v):
        v = Path(v).expanduser()
        return v if v.is_absolute() else (path.parent / v).resolve()

    for k in ["stage01_gpkg", "stage02_dir", "out_dir"]:
        cfg[k] = p(cfg[k])
    cfg["out_gpkg"] = cfg["out_dir"] / cfg["out_gpkg"]
    cfg["out_report"] = cfg["out_dir"] / cfg["out_report"]
    return cfg


def status_layer(status: str) -> str:
    return "catchments_stage03_" + "_".join(str(status).split())


def read_runs(stage02_dir: Path) -> dict[str, Path]:
    runs = {}
    for d in sorted(p for p in stage02_dir.iterdir() if p.is_dir()):
        f = d / f"ana_hidro_stage_02b_{d.name}.gpkg"
        if f.exists():
            runs[d.name] = f
    if not runs:
        raise FileNotFoundError(f"no ana_hidro_stage_02b_<run>.gpkg under {stage02_dir}")
    return runs


def main(config: Path) -> None:
    cfg = load_config(config)
    flag, bands = cfg["area_flag_ratio"], cfg["area_flag_bands"]

    # ---- Stage 1
    s1_sites = gpd.read_file(cfg["stage01_gpkg"], layer="sites").set_index("name").to_crs(OUT_CRS)
    s1 = gpd.read_file(cfg["stage01_gpkg"], layer="catchments_stage01").set_index("name").to_crs(OUT_CRS)
    s1 = clean_parts(s1, cfg["multipart_drop_pct"])
    s1["revision"] = [f"minor parts dropped ({n}, {a:.3f} km2)" if n else ""
                      for n, a in zip(s1["parts_dropped"], s1["area_dropped_km2"])]
    s1 = measure(s1)
    s1["status_auto"] = auto_status(s1, flag, bands)
    s1["area_flag_ratio"] = flag_ratios(s1["area_ana_km2"], flag, bands)
    s1["status_stage03"] = s1["status_auto"]
    s1["status_origin"] = "auto"
    s1["stage"] = "stage01"
    s1["catchment_source"] = s1["source"]

    # ---- Stage 2b, all runs
    runs = read_runs(cfg["stage02_dir"])
    cats, sites2, moved = [], [], []
    for run, f in runs.items():
        c = gpd.read_file(f, layer="catchments_stage02b").to_crs(OUT_CRS)
        c["run"] = run
        cats.append(c)
        s = gpd.read_file(f, layer="sites", ignore_geometry=True)
        s["run"] = run
        sites2.append(s)
        if "outlets_moved" in pyogrio.list_layers(f)[:, 0]:
            m = gpd.read_file(f, layer="outlets_moved").to_crs(OUT_CRS)
            m["run"] = run
            moved.append(m)
    s2 = pd.concat(cats, ignore_index=True)
    sites2 = pd.concat(sites2, ignore_index=True)

    errors = []
    dup = sorted(s2.loc[s2["name"].duplicated(), "name"])
    if dup:
        errors.append(f"codes with a catchment in more than one Stage 2b run: {dup[:20]}")
    dup = sorted(sites2.loc[sites2["name"].duplicated(), "name"])
    if dup:
        errors.append(f"codes in the sites layer of more than one Stage 2b run: {dup[:20]}")
    both = sorted(set(s1.index) & set(s2["name"]))
    if both:
        errors.append(f"codes with a catchment in Stage 1 and Stage 2b: {both[:20]}")
    unknown = sorted(set(s2["name"]) - set(s1_sites.index))
    if unknown:
        errors.append(f"Stage 2b codes not in the Stage 1 sites layer: {unknown[:20]}")
    if errors:
        raise ValueError("stage 03:\n  - " + "\n  - ".join(errors))

    s2 = s2.set_index("name")
    s2["stage"] = "stage02b"
    s2["status_stage03"] = s2["status_stage02b"]
    s2["area_source_km2"] = None
    sites2 = sites2.set_index("name")

    # ---- Merge catchments
    catch = gpd.GeoDataFrame(pd.concat([s1, s2]), geometry="geometry", crs=OUT_CRS)
    catch.index.name = "name"

    # ---- Sites: every ANA station, with the same columns as the catchments
    sites = s1_sites[["title", "river", "uf", "operating", "telemetric", "area_ana_km2", "geometry"]].copy()
    sites["run"] = sites2["run"].reindex(sites.index)
    for col in ["operating", "telemetric"]:
        catch[col] = sites[col].reindex(catch.index)
    catch["run"] = catch["run"].fillna(sites["run"].reindex(catch.index))
    catch["has_catchment"] = True
    attr = [c for c in COLUMNS if c not in ("name", "title", "river", "uf", "operating", "telemetric",
                                            "area_ana_km2", "run")]
    sites = sites.join(catch[attr], how="left")
    none = sites["has_catchment"].isna()
    sites.loc[none, "has_catchment"] = False
    sites.loc[none, "stage"] = "none"
    sites.loc[none, "status_stage03"] = sites2["status_stage02b"].reindex(sites.index[none]).fillna("pending")
    sites.loc[none, "status_origin"] = sites2["status_origin"].reindex(sites.index[none])
    sites.loc[none, "revision"] = ""
    sites["has_catchment"] = sites["has_catchment"].astype(bool)
    sites["n_polygons"] = sites["n_polygons"].astype("Int64")
    sites.index.name = "name"

    catch = catch.reset_index()[COLUMNS + ["geometry"]].sort_values("name")
    catch["n_polygons"] = catch["n_polygons"].astype("Int64")
    sites = gpd.GeoDataFrame(sites.reset_index()[COLUMNS + ["geometry"]], geometry="geometry",
                             crs=OUT_CRS).sort_values("name")

    # ---- Write
    out = cfg["out_gpkg"]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    sites.to_file(out, layer="sites_stage03", driver="GPKG")
    catch.to_file(out, layer="catchments_stage03", driver="GPKG")
    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    for st in sorted(catch["status_stage03"].unique(), key=lambda s: order.get(s, 99)):
        catch[catch["status_stage03"] == st].to_file(out, layer=status_layer(st), driver="GPKG")
    sites[~sites["has_catchment"]].to_file(out, layer="sites_pending_stage_03", driver="GPKG")
    if moved:
        gpd.GeoDataFrame(pd.concat(moved, ignore_index=True), geometry="geometry",
                         crs=OUT_CRS).to_file(out, layer="outlets_moved", driver="GPKG")
    styled = style_gpkg(out)

    write_report(cfg, runs, catch, sites)
    n_ok = (catch["status_stage03"] == "ok").sum()
    print(f"stage 03: {len(sites)} stations, {len(catch)} catchments "
          f"({(catch['stage'] == 'stage01').sum()} Stage 1, {(catch['stage'] == 'stage02b').sum()} Stage 2b "
          f"from {len(runs)} runs), {n_ok} ok, {(~sites['has_catchment']).sum()} without catchment")
    print(f"stage 03: {out} (styled: {len(styled)} layers)")
    print(f"stage 03: report -> {cfg['out_report']}")


def write_report(cfg, runs, catch, sites) -> None:
    def pct(n, d):
        return f"{100 * n / d:.1f}%" if d else "-"

    order = {s: i for i, s in enumerate(STATUS_ORDER)}
    lines = [
        "# ANA-Hidro catchments - Stage 3 (merge)",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}. Not written into the ANA database.",
        "",
        f"- Stage 1: `{cfg['stage01_gpkg']}`",
        f"- Stage 2b: {len(runs)} runs under `{cfg['stage02_dir']}`",
        f"- Output: `{cfg['out_gpkg']}`",
        f"- Area tolerance: ratio {cfg['area_flag_ratio']}, bands {cfg['area_flag_bands']}",
        "",
        "## Totals",
        "",
        f"- Stations: {len(sites)}",
        f"- With catchment: {sites['has_catchment'].sum()} ({pct(sites['has_catchment'].sum(), len(sites))})",
        f"- Without catchment: {(~sites['has_catchment']).sum()}",
        "",
    ]
    tab = pd.crosstab(sites["status_stage03"], sites["stage"], margins=True, margins_name="total")
    tab = tab.loc[sorted(tab.index, key=lambda s: (s == "total", order.get(s, 99)))]
    tab["share"] = [pct(v, len(sites)) for v in tab["total"]]
    tab.index.name = "status_stage03"
    lines += ["## Final status by stage (stations)", ""] + md_table(tab) + [""]

    tab = pd.crosstab(catch["source"], catch["status_stage03"], margins=True, margins_name="total")
    tab = tab[sorted(tab.columns, key=lambda s: (s == "total", order.get(s, 99)))]
    tab.index.name = "source"
    lines += ["## Catchments by source and final status", ""] + md_table(tab) + [""]

    s1 = catch[catch["stage"] == "stage01"]
    flagged = s1[s1["status_stage03"] != "ok"].sort_values("area_ratio_ana")
    lines += [
        "## Stage 1 catchments flagged by the area check",
        "",
        f"{len(flagged)} of {len(s1)} published catchments are not `ok` with the Stage 2 tolerance "
        "(not reviewed by hand; listed for inspection). "
        f"{(s1['revision'] != '').sum()} had minor parts dropped "
        f"(< {cfg['multipart_drop_pct']}% of the area, as in Stage 2).",
        "",
    ]
    if len(flagged):
        t = flagged.set_index("name")[["title", "uf", "source", "area_ana_km2", "area_geod_km2",
                                       "area_ratio_ana", "area_flag_ratio", "status_stage03"]]
        lines += md_table(t) + [""]

    sites_r = sites.copy()
    sites_r["run"] = sites_r["run"].fillna("-")
    tab = pd.crosstab(sites_r["run"], sites_r["status_stage03"])
    tab = tab[sorted(tab.columns, key=lambda s: order.get(s, 99))]
    tab.insert(0, "stations", tab.sum(axis=1))
    tab["ok share"] = [pct(o, n) for o, n in zip(tab.get("ok", 0), tab["stations"])]
    tab.index.name = "run"
    lines += ["## Final status by run (stations, Stage 1 included)", ""] + md_table(tab) + [""]

    pend = sites[~sites["has_catchment"]].set_index("name")[["title", "uf", "run", "status_stage03"]]
    lines += ["## Stations without catchment", ""] + (md_table(pend) if len(pend) else ["None."]) + [""]
    cfg["out_report"].write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Stage 3: merge Stage 1 + Stage 2b into one GeoPackage.")
    ap.add_argument("--config", required=True, type=Path)
    main(ap.parse_args().config)
