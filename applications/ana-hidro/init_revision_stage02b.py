"""
Initialize empty Stage 2b revision files (revision_stage02b.toml), one per state.

The file goes to the state's out_dir (next to ana_hidro_stage_02_<run>.gpkg).
By design this script NEVER overwrites: a state that already has the file is
skipped, whatever its content. To start a state over, delete its file by hand.

Usage:
    python init_revision_stage02b.py                          # every specs/stage_02/*.json
    python init_revision_stage02b.py specs/stage_02/43_rs.json specs/stage_02/31_mg.json
"""
import argparse
from pathlib import Path

from stage_02 import load_config

HERE = Path(__file__).resolve().parent
SPECS_DIR = HERE / "specs" / "stage_02"
FILE_NAME = "revision_stage02b.toml"

TEMPLATE = '''# Stage 2b revision for {run}. Codes are ANA station codes (quoted or bare).
# Apply: python stage_02b.py --config specs/stage_02/{run}.json
#
# [status]: final status, whatever the automatic one is.
#   keys: "ok", "probable overestimate", "probable underestimate",
#         "multiparts", "no ana area", "pending" (drop the catchment, back to pending)
#   A code goes under one status only.
#
# [force]: re-run get_catchments_fast for these codes.
#   force_headwaters: own fine-level basin only, no upstream trace
#                     (trace pulled in area that is not upstream -> overestimate)
#   force_trunk:      full upstream trace + coarse level, even if detected headwater
#                     (nothing found upstream by mistake -> underestimate)
#   force_upper:      vector upper approximation, no DEM refinement
#                     (DEM snapped to a small nearby creek -> underestimate)
#   force_headwaters and force_trunk exclude each other.
#
# [copy]: "target" = ["source", ...] - take the catchment of other station(s);
#   several are dissolved into one. Sources are looked up in this state (final
#   geometry), then the other states' Stage 2, then Stage 1 (CABra/CAMELS-BR).
#   A source must not be a [copy]/[replace] target itself.
# [replace]: "target" = ["path.fgb", ...] - polygon files, relative to this
#   folder; each one feature, single-part polygon, with a CRS. Several are dissolved.
# [outlet]: "code" = "point.fgb" - re-run the algorithm from this point instead of
#   the ANA coordinates (one point feature with a CRS, path relative to this
#   folder). May be combined with [force]; works for "no hydrobasin" stations too.
#   Moving the station point in place in the Stage 2 file does the same.
#
# A code is the target of only one of [force], [copy], [replace], and targets
# must not have manual vertex edits (they would be discarded).
# Status: every curated geometry (vertex edit, copy, replace, force, moved
# outlet) becomes "ok" automatically; list it in [status] only to set otherwise.
# status_auto and area_ratio_ana keep what the area check says.

[status]
"ok" = []
"pending" = []

[force]
force_headwaters = []
force_upper = []
force_trunk = []

[copy]
# "87510000" = ["87510001"]

[replace]
# "86340000" = ["edits/86340000.fgb"]

[outlet]
# "87510000" = "edits/87510000_outlet.fgb"
'''


def init_one(config: Path) -> tuple[str, str]:
    cfg = load_config(config)
    out_dir = cfg["out_dir"]
    path = out_dir / FILE_NAME
    if path.exists():
        return cfg["run_name"], f"exists, not touched: {path}"
    if not out_dir.exists():
        return cfg["run_name"], f"no output folder yet (Stage 2 not run): {out_dir}"
    # exclusive create: fails instead of overwriting, even if the file appears meanwhile
    with open(path, "x", encoding="utf-8") as f:
        f.write(TEMPLATE.format(run=cfg["run_name"]))
    return cfg["run_name"], f"created: {path}"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create empty revision_stage02b.toml files (never overwrites).")
    parser.add_argument("configs", nargs="*", type=Path, help="stage_02 configs (default: all in specs/stage_02)")
    args = parser.parse_args()
    configs = args.configs or sorted(SPECS_DIR.glob("*.json"))
    for c in configs:
        run, msg = init_one(c)
        print(f"{run}: {msg}")
