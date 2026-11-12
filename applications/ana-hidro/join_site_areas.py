"""
Copy catchment area and ANA ratio into the sites layers, in place.

For each GeoPackage, the sites layers get (left join by station `name`)
from the catchments layer of the same stage:
    area_geod_km2    geodesic area of the station's catchment
    area_ratio_ana   area_geod_km2 / area_ana_km2

Stage 2 files  (ana_hidro_stage_02_*.gpkg):  catchments_stage02  -> sites, sites_stage02, sites_pending_stage_02
Stage 2b files (ana_hidro_stage_02b_*.gpkg): catchments_stage02b -> sites, sites_stage02b, sites_pending_stage_02b

Done with plain SQL (ALTER TABLE / UPDATE): features, fids, embedded styles
and the catchment geometries are untouched. Columns are added when missing
and refreshed on every run; stations without a catchment get NULL. Close the
files in QGIS first.

Usage:
    python join_site_areas.py outputs/stage_02/*/ana_hidro_stage_02*_*.gpkg
"""
import argparse
import sqlite3
import struct
from pathlib import Path

import shapely

COLUMNS = {"area_geod_km2": "REAL", "area_ratio_ana": "REAL"}

LAYERS = {
    "catchments_stage02": ["sites", "sites_stage02", "sites_pending_stage_02"],
    "catchments_stage02b": ["sites", "sites_stage02b", "sites_pending_stage_02b"],
}


def table_columns(con, table):
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def st_isempty(blob):
    """GeoPackage ST_IsEmpty: the empty-geometry flag (bit 4) of the GPKG header flags byte.

    GDAL's rtree triggers reference it; plain sqlite does not have it, so it is
    registered here. Only attribute columns are updated, so the geometry
    triggers never actually change anything.
    """
    if blob is None:
        return 1
    b = bytes(blob)
    if len(b) < 4 or b[:2] != b"GP":
        return 0
    return 1 if b[3] & 0b10000 else 0


def _bounds(blob):
    """(minx, maxx, miny, maxy) of a GeoPackage geometry blob: header envelope, else the WKB."""
    if blob is None:
        return None
    b = bytes(blob)
    if len(b) < 8 or b[:2] != b"GP":
        return None
    flags = b[3]
    little = flags & 1
    env = (flags >> 1) & 0b111
    fmt = "<" if little else ">"
    if env >= 1:
        return struct.unpack(fmt + "4d", b[8:40])
    size = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
    g = shapely.from_wkb(b[8 + size:])
    if g is None or g.is_empty:
        return None
    minx, miny, maxx, maxy = g.bounds
    return minx, maxx, miny, maxy


def _coord(i):
    def f(blob):
        bb = _bounds(blob)
        return None if bb is None else bb[i]
    return f


def register_gpkg_functions(con) -> None:
    """The SQL functions GDAL's GeoPackage rtree triggers call."""
    con.create_function("ST_IsEmpty", 1, st_isempty, deterministic=True)
    for i, name in enumerate(["ST_MinX", "ST_MaxX", "ST_MinY", "ST_MaxY"]):
        con.create_function(name, 1, _coord(i), deterministic=True)


def join_areas(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    register_gpkg_functions(con)
    done = []
    try:
        tables = {r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")}
        for catch, site_layers in LAYERS.items():
            if catch not in tables:
                continue
            ccols = table_columns(con, catch)
            missing = [c for c in COLUMNS if c not in ccols]
            if missing:
                raise ValueError(f"{path.name}: {catch} has no column(s) {missing}")
            for layer in site_layers:
                if layer not in tables:
                    continue
                cols = table_columns(con, layer)
                for col, typ in COLUMNS.items():
                    if col not in cols:
                        con.execute(f'ALTER TABLE "{layer}" ADD COLUMN "{col}" {typ}')
                sets = ", ".join(
                    f'"{col}" = (SELECT c."{col}" FROM "{catch}" c WHERE c.name = "{layer}".name)'
                    for col in COLUMNS
                )
                con.execute(f'UPDATE "{layer}" SET {sets}')
                n = con.execute(f'SELECT count(*) FROM "{layer}" WHERE area_geod_km2 IS NOT NULL').fetchone()[0]
                total = con.execute(f'SELECT count(*) FROM "{layer}"').fetchone()[0]
                con.execute("UPDATE gpkg_contents SET last_change = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                            "WHERE table_name = ?", (layer,))
                done.append(f"{layer} {n}/{total}")
        con.commit()
    finally:
        con.close()
    return done


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Copy catchment area and ANA ratio into sites layers (in place).")
    parser.add_argument("gpkg", nargs="+", type=Path)
    args = parser.parse_args()
    for g in args.gpkg:
        done = join_areas(g)
        print(f"{g}: {', '.join(done) if done else 'no matching layers'}")
