"""
Stage 1 - catchments for ANA-Hidro fluviometric stations from published datasets.

For every ANA station, look for a catchment polygon already delineated by:
  1. CABra (priority)
  2. CAMELS-BR (second option)

Match is by ANA station code (`name` in all three databases, 8-digit text;
CABra also has it as the integer `ana_id`). A code match is only accepted
when the ANA station point lies inside the source polygon, or within
`buffer_km` of it, to absorb small coordinate inconsistencies. If the
CABra polygon fails that check, CAMELS-BR is tried.

CAMELS-BR polygons come from GSIM, a coarse delineation that degrades on
small catchments, so a CAMELS-BR polygon under `camelsbr_min_area_km2`
(geodesic area) is not used: the station goes to pending for Stage 2.

Outputs (in out_dir from the config):
  ana_hidro_stage_01.gpkg
    sites                   all ANA stations, with the areas of every source
    sites_stage01           stations that got a catchment here
    catchments_stage01      the catchment polygons (one per station)
    sites_pending_stage_01  stations still without a catchment
  report_stage_01.md        assessment highlights

Usage:
    python stage_01.py --config specs/stage_01.json

Config keys (JSON; relative paths resolve against the config file's folder):
    ana_db, cabra_db, camelsbr_db   required, input GeoPackages
    out_dir                         required, output folder (created if missing)
    out_gpkg                        "ana_hidro_stage_01.gpkg"
    out_report                      "report_stage_01.md"
    buffer_km                       2.0    point-to-polygon acceptance distance
    camelsbr_min_area_km2           500.0  smaller CAMELS-BR polygons go to pending
    area_flag_ratio                 1.5    area mismatch flagged in the report
    out_crs                         "EPSG:4674"
    metric_crs                      "EPSG:5880"  used for distances
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Geod

# ---------------------------------------------------------------------
# Configuration (filled from the JSON file given with --config)
# ---------------------------------------------------------------------

REQUIRED_KEYS = ["ana_db", "cabra_db", "camelsbr_db", "out_dir"]
DEFAULTS = {
    "out_gpkg": "ana_hidro_stage_01.gpkg",
    "out_report": "report_stage_01.md",
    "buffer_km": 2.0,
    "camelsbr_min_area_km2": 500.0,
    "area_flag_ratio": 1.5,
    "out_crs": "EPSG:4674",
    "metric_crs": "EPSG:5880",
}

ana_db = cabra_db = camelsbr_db = None
out_dir = out_gpkg = out_report = None
buffer_km = camelsbr_min_area_km2 = area_flag_ratio = None
out_crs = metric_crs = None


def load_config(config_path: Path) -> None:
    """Read the JSON config and set the module settings.

    Relative paths are resolved against the folder of the config file, so a
    config can live next to its outputs and be moved with them.
    out_gpkg and out_report are file names inside out_dir.
    """
    global ana_db, cabra_db, camelsbr_db, out_dir, out_gpkg, out_report
    global buffer_km, camelsbr_min_area_km2, area_flag_ratio, out_crs, metric_crs

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

    def path(key):
        p = Path(cfg[key]).expanduser()
        return p if p.is_absolute() else (config_path.parent / p).resolve()

    ana_db, cabra_db, camelsbr_db = path("ana_db"), path("cabra_db"), path("camelsbr_db")
    for p in (ana_db, cabra_db, camelsbr_db):
        if not p.exists():
            raise FileNotFoundError(p)
    out_dir = path("out_dir")
    out_gpkg = out_dir / cfg["out_gpkg"]
    out_report = out_dir / cfg["out_report"]

    buffer_km = float(cfg["buffer_km"])
    camelsbr_min_area_km2 = float(cfg["camelsbr_min_area_km2"])
    area_flag_ratio = float(cfg["area_flag_ratio"])
    out_crs = cfg["out_crs"]
    metric_crs = cfg["metric_crs"]


# source label (value in the `source` columns) -> column-name suffix
SOURCES = {"cabra": "cabra", "camelsbr": "camelsbr"}

GEOD = Geod(ellps="WGS84")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def geodesic_area_km2(geom) -> float:
    return abs(GEOD.geometry_area_perimeter(geom)[0]) / 1e6


def point_to_polygon_km(points: gpd.GeoSeries, polys: gpd.GeoSeries) -> pd.Series:
    """Distance (km) from each point to its aligned polygon; 0 when inside."""
    p = points.to_crs(metric_crs)
    g = polys.to_crs(metric_crs).buffer(0)  # repair invalid polygons for the test
    return (p.distance(g, align=True) / 1000.0).round(3)


def load_source(db: Path, site_cols: dict, catch_cols: dict) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """Read a source's site attributes and catchment polygons, renaming columns."""
    sites = gpd.read_file(db, layer="sites", columns=list(site_cols), ignore_geometry=True)
    sites = sites.rename(columns=site_cols).set_index("name")
    catch = gpd.read_file(db, layer="catchments", columns=list(catch_cols))
    catch = catch.rename(columns=catch_cols).set_index("name").to_crs(out_crs)
    catch["area_geod_km2"] = catch.geometry.apply(geodesic_area_km2).round(2)
    return sites, catch


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Reading ANA stations ...")
    ana = gpd.read_file(
        ana_db, layer="sites",
        columns=["name", "nome", "rio", "uf", "area_drenagem", "operando", "estacao_telemetrica"],
    ).to_crs(out_crs)
    ana = ana.rename(columns={
        "nome": "title", "rio": "river", "area_drenagem": "area_ana_km2",
        "operando": "operating", "estacao_telemetrica": "telemetric",
    }).set_index("name")
    assert ana.index.is_unique

    print("Reading CABra ...")
    cabra_sites, cabra_catch = load_source(
        cabra_db,
        {"name": "name", "area_header_km2": "area_cabra_header_km2"},
        {"name": "name", "cabra_id": "cabra_id", "area_boundaries_km2": "area_cabra_km2"},
    )
    print("Reading CAMELS-BR ...")
    camelsbr_sites, camelsbr_catch = load_source(
        camelsbr_db,
        {"name": "name", "area_gsim": "area_camelsbr_gsim_km2"},
        {"name": "name", "area": "area_camelsbr_km2"},
    )
    catchs = {"cabra": cabra_catch, "camelsbr": camelsbr_catch}

    # Reported areas of every source, on all ANA stations
    sites = ana.join(cabra_sites).join(cabra_catch[["cabra_id", "area_cabra_km2"]])
    sites = sites.join(camelsbr_sites).join(camelsbr_catch[["area_camelsbr_km2"]])

    # Spatial check per source: distance from the ANA point to the source polygon
    for label, sfx in SOURCES.items():
        catch = catchs[label]
        common = catch.index.intersection(sites.index)
        dist = point_to_polygon_km(sites.geometry.loc[common], catch.geometry.loc[common])
        sites[f"distance_{sfx}_km"] = dist.reindex(sites.index)
        sites[f"has_{sfx}"] = sites.index.isin(common)
        sites[f"ok_{sfx}"] = sites[f"distance_{sfx}_km"] <= buffer_km

    # CAMELS-BR (GSIM) polygons under the minimum area are not usable
    camelsbr_area = camelsbr_catch["area_geod_km2"].reindex(sites.index)
    sites["small_camelsbr"] = sites["ok_camelsbr"] & (camelsbr_area < camelsbr_min_area_km2)

    # Pick the source: CABra first, then CAMELS-BR (if large enough)
    sites["source_stage01"] = None
    sites.loc[sites["ok_camelsbr"] & ~sites["small_camelsbr"], "source_stage01"] = "camelsbr"
    sites.loc[sites["ok_cabra"], "source_stage01"] = "cabra"
    # small CAMELS-BR polygons that would have been used (no valid CABra)
    sites["skipped_small_camelsbr"] = sites["small_camelsbr"] & ~sites["ok_cabra"]
    found = sites["source_stage01"].notna()

    # Catchments
    parts = []
    for label, sfx in SOURCES.items():
        catch = catchs[label]
        names = sites.index[sites["source_stage01"] == label]
        part = gpd.GeoDataFrame(
            {"source": label,
             "distance_km": sites.loc[names, f"distance_{sfx}_km"],
             "area_geod_km2": catch.loc[names, "area_geod_km2"],
             "area_source_km2": catch.loc[names, f"area_{sfx}_km2"]},
            index=names, geometry=catch.geometry.loc[names], crs=out_crs,
        )
        parts.append(part)
    catchments = pd.concat(parts).sort_index()
    catchments.index.name = "name"
    catchments = catchments.join(sites[["title", "river", "uf", "area_ana_km2", "area_cabra_km2", "area_camelsbr_km2"]])
    catchments["area_ratio_ana"] = (catchments["area_geod_km2"] / catchments["area_ana_km2"]).round(3)
    catchments = catchments[[
        "title", "river", "uf", "source", "distance_km",
        "area_geod_km2", "area_source_km2", "area_ana_km2", "area_cabra_km2", "area_camelsbr_km2",
        "area_ratio_ana", "geometry",
    ]]

    site_cols = [
        "title", "river", "uf", "operating", "telemetric",
        "area_ana_km2", "area_cabra_km2", "area_cabra_header_km2",
        "area_camelsbr_km2", "area_camelsbr_gsim_km2",
        "cabra_id", "distance_cabra_km", "distance_camelsbr_km",
        "skipped_small_camelsbr", "source_stage01", "geometry",
    ]
    out_sites = sites[site_cols].reset_index()

    print(f"Writing {out_gpkg} ...")
    if out_gpkg.exists():
        out_gpkg.unlink()
    out_sites.to_file(out_gpkg, layer="sites", driver="GPKG")
    out_sites[found.values].to_file(out_gpkg, layer="sites_stage01", driver="GPKG")
    catchments.reset_index().to_file(out_gpkg, layer="catchments_stage01", driver="GPKG")
    out_sites[~found.values].drop(columns="source_stage01").to_file(
        out_gpkg, layer="sites_pending_stage_01", driver="GPKG")

    write_report(sites, catchments, camelsbr_area)
    print(f"Done. {found.sum()} found, {(~found).sum()} pending.")


def write_report(sites: pd.DataFrame, catchments: gpd.GeoDataFrame, camelsbr_area: pd.Series) -> None:
    n = len(sites)
    found = sites["source_stage01"].notna()
    counts = sites["source_stage01"].value_counts()

    def row(label, sfx):
        has, ok = sites[f"has_{sfx}"], sites[f"ok_{sfx}"]
        return f"| {label} | {has.sum()} | {ok.sum()} | {(has & ~ok).sum()} | {counts.get(label, 0)} |"

    both = sites["ok_cabra"] & sites["ok_camelsbr"]
    camelsbr_rescue = (sites["has_cabra"] & ~sites["ok_cabra"]) & (sites["source_stage01"] == "camelsbr")
    skipped = sites[sites["skipped_small_camelsbr"]]
    small_covered = (sites["small_camelsbr"] & sites["ok_cabra"]).sum()

    lo, hi = 1 / area_flag_ratio, area_flag_ratio
    r = catchments["area_ratio_ana"]
    flagged = catchments[(r < lo) | (r > hi)]
    no_ana_area = catchments["area_ana_km2"].isna() | (catchments["area_ana_km2"] <= 0)

    rejected = sites[(sites["has_cabra"] & ~sites["ok_cabra"]) | (sites["has_camelsbr"] & ~sites["ok_camelsbr"])]

    def fmt(v):
        return "" if pd.isna(v) else v

    lines = [
        "# ANA-Hidro catchments - Stage 1 report",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `stage_01.py`.",
        "",
        "## Rule",
        "",
        "- Match by ANA station code (`name`, 8-digit text, identical in the three databases).",
        f"- Accept a match when the ANA station point is inside the source polygon or within **{buffer_km} km** of it.",
        "- Priority: CABra, then CAMELS-BR when CABra has no polygon or fails the spatial check.",
        f"- CAMELS-BR polygons (GSIM) under **{camelsbr_min_area_km2:,.0f} km2** (geodesic) are not used; "
        "the station goes to pending.",
        "",
        "## Highlights",
        "",
        f"- ANA fluviometric stations: **{n}**",
        f"- With a catchment after Stage 1: **{found.sum()}** ({found.mean():.1%})",
        f"- Pending for Stage 2: **{(~found).sum()}**",
        "",
        "| source | code matches | spatially OK | rejected (> buffer) | used |",
        "|---|---|---|---|---|",
        row("cabra", "cabra"),
        row("camelsbr", "camelsbr"),
        "",
        f"- Stations valid in both sources (CABra used): {both.sum()}",
        f"- Stations where CABra failed the check and CAMELS-BR was used: {camelsbr_rescue.sum()}",
        f"- CAMELS-BR polygons under {camelsbr_min_area_km2:,.0f} km2: "
        f"**{len(skipped)} skipped and sent to pending**; {small_covered} more were small but CABra was used anyway",
        "",
        "## Rejected code matches (point farther than the buffer)",
        "",
        "| name | title | distance CABra km | distance CAMELS-BR km | final source |",
        "|---|---|---|---|---|",
    ]
    for name, s in rejected.sort_index().iterrows():
        lines.append(f"| {name} | {s.title} | {fmt(s.distance_cabra_km)} "
                     f"| {fmt(s.distance_camelsbr_km)} | {s.source_stage01 or 'pending'} |")

    lines += [
        "",
        f"## CAMELS-BR polygons skipped (< {camelsbr_min_area_km2:,.0f} km2)",
        "",
        f"{len(skipped)} stations. Area distribution (geodesic km2): "
        + (f"min {camelsbr_area[skipped.index].min():,.1f}, "
           f"median {camelsbr_area[skipped.index].median():,.1f}, "
           f"max {camelsbr_area[skipped.index].max():,.1f}." if len(skipped) else "-"),
        "They are listed in `sites_pending_stage_01` with `skipped_small_camelsbr = 1`.",
        "",
        "## Area check (geodesic polygon area vs ANA reported drainage area)",
        "",
        f"- Median ratio: {r.median():.3f}; 5-95%: {r.quantile(.05):.3f} - {r.quantile(.95):.3f}",
        f"- Catchments without an ANA area: {no_ana_area.sum()}",
        f"- Ratio outside [{lo:.2f}, {hi:.2f}] (kept, flagged for review): {len(flagged)}",
        "",
        "| name | title | source | area geod km2 | area ANA km2 | ratio |",
        "|---|---|---|---|---|---|",
    ]
    for name, c in flagged.sort_values("area_ratio_ana").iterrows():
        lines.append(f"| {name} | {c.title} | {c.source} | {c.area_geod_km2:,.1f} | {c.area_ana_km2:,.1f} | {c.area_ratio_ana} |")

    out_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Report: {out_report}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 1: ANA-Hidro catchments from CABra / CAMELS-BR.")
    parser.add_argument("--config", required=True, type=Path, help="path to the JSON config")
    args = parser.parse_args()
    load_config(args.config)
    print(f"Config: {args.config.resolve()}")
    main()
