"""
Embed QGIS styles into ANA-Hidro GeoPackages, so layers open styled in QGIS.

Writes the style as the layer's default in the GeoPackage `layer_styles`
table (the table QGIS itself uses for "Save style > In database"). Any
earlier style of the same layer is replaced. Pure sqlite: QGIS is not
needed to run this.

Styles:
  catchments  no fill, light green border 0.3 mm
  sites       2.5 mm circles, white border 0.26 mm, colored by
              status_stage02b (stage 02b files) or status_stage02:
                ok                      green
                no ana area             yellow-green
                multiparts              orange
                probable overestimate   red
                probable underestimate  magenta
                (any other value)       grey

Layers styled (when present in the file):
  catchments_stage01, catchments_stage02, catchments_stage02b,
  catchments_stage02b_<status> (per-status split)               -> catchments style
  sites_stage02(b), sites, sites_pending_stage_02(b)            -> sites style
    (sites layers are styled only if they have a status column)

Note: stage_02.py rewrites its GeoPackage from scratch, so run this again
after any collect.

Usage:
    python style_gpkg.py outputs/stage_02/*/ana_hidro_stage_02_*.gpkg
    python style_gpkg.py some.gpkg --layers catchments_stage02 sites_stage02
"""
import argparse
import sqlite3
from pathlib import Path

STATUS_FIELDS = ["status_stage02b", "status_stage02"]  # first one present is used

STATUS_COLORS = [  # (value, label, rgb)
    ("ok", "ok", "51,160,44"),
    ("no ana area", "no ana area", "173,212,0"),
    ("multiparts", "multiparts", "255,127,0"),
    ("probable overestimate", "probable overestimate", "227,26,28"),
    ("probable underestimate", "probable underestimate", "230,0,230"),
]
OTHER_COLOR = "150,150,150"

CATCHMENT_BORDER = "120,220,120"  # light green

CATCHMENT_LAYERS = ["catchments_stage01", "catchments_stage02", "catchments_stage02b"]
SITE_LAYERS = ["sites_stage02", "sites_stage02b", "sites", "sites_pending_stage_02", "sites_pending_stage_02b"]

QML_HEAD = "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" \
           '<qgis version="3.34.4-Prizren" styleCategories="Symbology">\n'


def opt(name: str, value) -> str:
    return f'        <Option name="{name}" type="QString" value="{value}"/>\n'


def marker_symbol(idx: int, rgb: str) -> str:
    return (
        f'    <symbol type="marker" name="{idx}" alpha="1" clip_to_extent="1" force_rhr="0" frame_rate="10" is_animated="0">\n'
        '      <layer class="SimpleMarker" enabled="1" locked="0" pass="0">\n'
        '       <Option type="Map">\n'
        + opt("name", "circle")
        + opt("size", "2.5") + opt("size_unit", "MM")
        + opt("color", f"{rgb},255")
        + opt("outline_color", "255,255,255,255")
        + opt("outline_style", "solid")
        + opt("outline_width", "0.26") + opt("outline_width_unit", "MM")
        + opt("joinstyle", "bevel")
        + opt("scale_method", "diameter")
        + opt("angle", "0")
        + opt("offset", "0,0") + opt("offset_unit", "MM")
        + opt("horizontal_anchor_point", "1") + opt("vertical_anchor_point", "1")
        + '       </Option>\n'
        '      </layer>\n'
        '    </symbol>\n'
    )


def sites_qml(field: str) -> str:
    cats = [(v, lbl, rgb) for v, lbl, rgb in STATUS_COLORS] + [("", "other", OTHER_COLOR)]
    categories = "".join(
        f'    <category symbol="{i}" value="{v}" label="{lbl}" render="true" type="string"/>\n'
        for i, (v, lbl, _) in enumerate(cats)
    )
    symbols = "".join(marker_symbol(i, rgb) for i, (_, _, rgb) in enumerate(cats))
    return (
        QML_HEAD
        + f' <renderer-v2 type="categorizedSymbol" attr="{field}" symbollevels="0" '
          'enableorderby="0" forceraster="0" referencescale="-1">\n'
        + '  <categories>\n' + categories + '  </categories>\n'
        + '  <symbols>\n' + symbols + '  </symbols>\n'
        + ' </renderer-v2>\n'
        + '</qgis>\n'
    )


def catchments_qml() -> str:
    return (
        QML_HEAD
        + ' <renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0" referencescale="-1">\n'
        '  <symbols>\n'
        '    <symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0" frame_rate="10" is_animated="0">\n'
        '      <layer class="SimpleFill" enabled="1" locked="0" pass="0">\n'
        '       <Option type="Map">\n'
        + opt("style", "no")
        + opt("color", "0,0,0,0")
        + opt("outline_color", f"{CATCHMENT_BORDER},255")
        + opt("outline_style", "solid")
        + opt("outline_width", "0.3") + opt("outline_width_unit", "MM")
        + opt("joinstyle", "bevel")
        + opt("offset", "0,0") + opt("offset_unit", "MM")
        + '       </Option>\n'
        '      </layer>\n'
        '    </symbol>\n'
        '  </symbols>\n'
        ' </renderer-v2>\n'
        '</qgis>\n'
    )


LAYER_STYLES_DDL = """
CREATE TABLE IF NOT EXISTS layer_styles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    f_table_catalog TEXT(256),
    f_table_schema TEXT(256),
    f_table_name TEXT(256),
    f_geometry_column TEXT(256),
    styleName TEXT(30),
    styleQML TEXT,
    styleSLD TEXT,
    useAsDefault BOOLEAN,
    description TEXT,
    owner TEXT(30),
    ui TEXT(30),
    update_time DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
)
"""


def ensure_layer_styles(con: sqlite3.Connection) -> None:
    con.execute(LAYER_STYLES_DDL)
    # register as a GeoPackage attributes table, as QGIS does
    con.execute(
        "INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, last_change) "
        "VALUES ('layer_styles', 'attributes', 'layer_styles', strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
    )


def layer_info(con: sqlite3.Connection) -> dict:
    """{layer: (geometry column, [field names])} for the feature layers."""
    out = {}
    for name, geom_col in con.execute("SELECT table_name, column_name FROM gpkg_geometry_columns"):
        fields = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
        out[name] = (geom_col, fields)
    return out


def style_gpkg(path: Path, only_layers: list[str] | None = None) -> list[str]:
    con = sqlite3.connect(path)
    styled = []
    try:
        layers = layer_info(con)
        ensure_layer_styles(con)
        split = sorted(l for l in layers if l.startswith("catchments_stage02b_"))  # per-status layers
        plan = [(lyr, "catchments") for lyr in CATCHMENT_LAYERS + split] + [(lyr, "sites") for lyr in SITE_LAYERS]
        for lyr, kind in plan:
            if lyr not in layers or (only_layers and lyr not in only_layers):
                continue
            geom_col, fields = layers[lyr]
            if kind == "catchments":
                qml = catchments_qml()
            else:
                field = next((f for f in STATUS_FIELDS if f in fields), None)
                if field is None:
                    continue
                qml = sites_qml(field)
            con.execute("DELETE FROM layer_styles WHERE f_table_name = ?", (lyr,))
            con.execute(
                "INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name, f_geometry_column, "
                "styleName, styleQML, styleSLD, useAsDefault, description, owner, ui) "
                "VALUES ('', '', ?, ?, ?, ?, '', 1, ?, '', NULL)",
                (lyr, geom_col, lyr, qml, f"ana-hidro {kind} style (style_gpkg.py)"),
            )
            styled.append(lyr)
        con.commit()
    finally:
        con.close()
    return styled


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Embed QGIS styles into ANA-Hidro GeoPackages.")
    parser.add_argument("gpkg", nargs="+", type=Path)
    parser.add_argument("--layers", nargs="*", help="only these layers (default: all known)")
    args = parser.parse_args()
    for g in args.gpkg:
        styled = style_gpkg(g, args.layers)
        print(f"{g}: {', '.join(styled) if styled else 'nothing styled'}")
