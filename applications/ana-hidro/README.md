# ANA-Hidro catchments

Catchment polygons for the ANA-Hidro fluviometric stations of Brazil
(7,471 stations, `ANA_HIDRO-WS1_FLU_DAILY.gpkg`), built in stages:

| Stage | What | Script |
|---|---|---|
| 1 | reuse published catchments (CABra, then CAMELS-BR) | `stage_01.py` |
| 2 | delineate the rest with `get_catchments_fast.py`, one run per state | `stage_02.py`, `stage_02_batch.py` |
| 2b | manual revision per state (edits, forces, copies, replacements, moved outlets) | `stage_02b.py` |
| 3 | merge everything, final check, append into the ANA database | *not written yet* |

Everything is driven by JSON configs and reproducible from the command line.
Run all commands from this folder (`applications/ana-hidro/`).

> **What is versioned and what is not.** The scripts, this README and the
> example configs in `specs.example/` are in git. Your real configs (`specs/`,
> machine paths) and all results (`outputs/`: GeoPackages with your manual
> edits, revision files, hand-made polygons) are ignored (see `.gitignore`)
> and live only on disk. See [Backup and moving](#7-backup-and-moving).

---

## 1. Setup

### Python

Python 3.12 (3.11+ needed for `tomllib`). From the repo root:

```bash
python3.12 -m venv ~/Venvs/ana-hidro
source ~/Venvs/ana-hidro/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins the versions used for the current results. `plans`
(for `plans.geo.get_upstream_features_iterative`) is the author's library; if
PyPI does not have it, install it from GitHub (see the comment in the file).

QGIS is only needed to look at and edit the results. No script needs it.

### Input data

| Data | Config key | Used by |
|---|---|---|
| ANA-Hidro stations, GeoPackage with a `sites` layer (`name` = 8-digit ANA code, `uf`, ANA drainage area) | `ana_db` | stage 1 |
| CABra catchments (GeoPackage, `name` = ANA code) | `cabra_db` | stage 1 |
| CAMELS-BR catchments (GeoPackage, `name` = ANA code) | `camelsbr_db` | stage 1 |
| HydroBASINS South America lev08 + lev12 (`hybas_sa_lev08_v1c`, `hybas_sa_lev12_v1c`) in one GeoPackage | `gcf.src_dir` + `gcf.src_db` | stage 2 (read only) |
| DEM, HydroSHEDS 3" conditioned (`sa_con_3s.tif`) | `gcf.dem_path` | stage 2 |

### Configs

`specs.example/` holds the configs used for the published run, with mock
data paths (`/path/to/data/...`). Copy it to `specs/` (ignored by git) and
set your paths:

```bash
cp -r specs.example specs
# edit the data paths in specs/stage_01.json and specs/stage_02_rs.json
python stage_02_batch.py make --template specs/stage_02_rs.json   # state configs, 3.1
```

Relative paths in the configs resolve from the config's folder, so keep
`specs/` next to the scripts. If a data path changes later, change it in
`specs/stage_01.json` and the template, then regenerate the state configs.

---

## 2. Stage 1 - published catchments

```bash
python stage_01.py --config specs/stage_01.json
```

- Match by ANA code (`name` in all three databases).
- Accepted when the ANA point is inside the source polygon or within
  `buffer_km` (2 km) of it.
- Priority CABra, then CAMELS-BR. CAMELS-BR polygons (GSIM) smaller than
  `camelsbr_min_area_km2` (500 km2) are not used: those stations go to pending.
- Output `outputs/stage_01/ana_hidro_stage_01.gpkg`: `sites`, `sites_stage01`,
  `catchments_stage01`, `sites_pending_stage_01`, plus `report_stage_01.md`.
- Source values: `cabra`, `camelsbr`. Distances in `distance_*_km`.

Result: 970 catchments (731 CABra, 239 CAMELS-BR), 6,501 pending.

---

## 3. Stage 2 - get_catchments_fast, per state

### 3.1 Configs

`specs/stage_02_rs.json` is the template (it was the RS calibration run).
One config per state is generated from it:

```bash
python stage_02_batch.py make --template specs/stage_02_rs.json
```

-> `specs/stage_02/<run>.json`, `<run>` = IBGE code + UF (`43_rs`, `31_mg`, ...)
or `xx_<country>` for stations abroad. Only `run_name`, `ufs` and the output
paths differ. **Re-running `make` overwrites these configs** - edit the
template, not the generated files, for settings that apply to all states.

Config keys (see the docstring of `stage_02.py`):

| Key | Value | Meaning |
|---|---|---|
| `ufs` | `["RIO GRANDE DO SUL"]` | states (the `uf` column), null = all |
| `area_flag_ratio` | 1.5 | ok if area / ANA area within [1/1.5, 1.5] |
| `area_flag_bands` | `[[100, 2.0]]` | looser ratio by ANA area: below 100 km2 ok within [0.5, 2.0] |
| `multipart_drop_pct` | 5.0 (default) | drop catchment parts < 5% of the area (stray pixels) |
| `gcf` | dict | the `get_catchments_fast` spec (basins, levels, DEM, tuning) |

`gcf` tuning in use: `catchment_acceptable_error_pct` 5,
`dem_clip_buffer_pct` 2.0 (was 1.5 for the original Stage 2 run),
`dem_clip_square` true, `dem_snap_search_cells` 10,
`raster_only_if_disconnected` / `raster_only_if_low_overlap` true with
threshold 0.1, `headwater_detection` trace, `compute_area_crs` ESRI:102033.

### 3.2 Run

All states, 4 in parallel (~33 min on 24 cores):

```bash
python stage_02_batch.py run --workers 4             # skips states whose report exists
python stage_02_batch.py run --only 43_rs 31_mg      # some states
python stage_02_batch.py run --force                 # redo everything (overwrites edits!)
```

One state, step by step:

```bash
python stage_02.py --config specs/stage_02/43_rs.json --step prepare
python stage_02.py --config specs/stage_02/43_rs.json --step run
python stage_02.py --config specs/stage_02/43_rs.json --step collect [--force]
```

- **prepare**: pending stations of the state -> `intermediate/<run>/outlets.gpkg`
  (stations outside every lev12 basin go to layer `outlets_no_hydrobasin`
  and stay pending as `no hydrobasin`), and writes `gcf_spec.json`.
- **run**: `get_catchments_fast.py` -> `intermediate/<run>/gcf/` (log `run.log`).
- **collect**: builds `outputs/stage_02/<run>/ana_hidro_stage_02_<run>.gpkg`
  and the report. **Refuses to overwrite an existing file without
  `--force`**, because that file holds your manual edits.

Layers: `sites`, `sites_stage02`, `catchments_stage02`, `sites_pending_stage_02`.

### 3.3 Status (`status_stage02`)

From `area_ratio_ana` = geodesic area / ANA reported area and the tolerance
of the station (`area_flag_ratio`, or a band of `area_flag_bands`):

| Status | Rule |
|---|---|
| `ok` | ratio within tolerance, one part |
| `probable overestimate` / `probable underestimate` | ratio above / below tolerance |
| `multiparts` | area ok but more than one polygon after dropping minor parts |
| `no ana area` | ANA reports no drainage area |
| `no hydrobasin` / `failed` | sites only: outside every lev12 basin / algorithm failed |

`catchment_source` (from the algorithm): `vector_upper`, `raster_refined`,
`raster_refined_disconnected_vector_dropped`,
`raster_refined_low_overlap_vector_dropped`, `vector_upper_forced`, ...

---

## 4. Stage 2b - revision, per state

The working loop for a state (`<run>` e.g. `43_rs`):

1. Open `outputs/stage_02/<run>/ana_hidro_stage_02_<run>.gpkg` (or the 02b
   file of the previous round) in QGIS.
2. Optional, directly in the **stage 02 file**:
   - edit vertices of `catchments_stage02` (or delete a catchment -> pending);
   - move a station point in `sites_stage02` (or `sites_pending_stage_02`
     for `no hydrobasin` stations) -> the algorithm re-runs from there.
3. Write entries in `outputs/stage_02/<run>/revision_stage02b.toml`.
4. `python stage_02b.py --config specs/stage_02/<run>.json`
5. Look at `ana_hidro_stage_02b_<run>.gpkg` and `report_stage_02b_<run>.md`.

Create the empty revision files (never overwrites an existing one):

```bash
python init_revision_stage02b.py                                  # all states
python init_revision_stage02b.py specs/stage_02/43_rs.json        # one state
```

### 4.1 revision_stage02b.toml

Codes are ANA codes (quoted or bare). Paths are relative to the toml's folder.
A single item may be written without brackets.

```toml
[status]                       # final status, whatever the automatic one is
"ok" = ["87010000", "85890000"]
"pending" = []                 # drop the catchment, back to pending

[force]                        # re-run get_catchments_fast with an option
force_headwaters = ["86710000"]   # own lev12 basin only (trace pulled in a big river)
force_upper      = ["85200000"]   # vector result, no DEM (DEM snapped to a small creek)
force_trunk      = ["87040000"]   # full trace + coarse level, even if detected headwater

[copy]                         # take another station's catchment (several are dissolved)
"85735000" = "85740000"
"87450000" = ["87460120", "87450005"]

[replace]                      # polygon file(s), one single-part feature each, with CRS
"87450000" = ["guaiba.fgb"]

[outlet]                       # re-run from this point (one point feature, with CRS)
"87510000" = "itapoa_outlet.fgb"
```

Rules (the script stops with a clear error otherwise):

- a code is in `[status]` at most once;
- one geometry source per code: `[copy]`, `[replace]` and `[force]`/`[outlet]`
  exclude each other (`[force]` + `[outlet]` may be combined);
- `force_headwaters` and `force_trunk` exclude each other;
- `[force]`/`[outlet]`/`[copy]`/`[replace]` codes must not have manual vertex
  edits (they would be discarded);
- copy sources: this state (final geometry), then other states' stage 02
  files, then Stage 1; a source cannot itself be a copy/replace target;
- a point moved in place in the stage 02 file counts as an `[outlet]` entry:
  moving it and listing it in `[outlet]`, or moving it to different places in
  two layers, is an error;
- `no hydrobasin` stations can only be re-run with a new point
  (`[outlet]` or a moved point), or get a `[copy]`/`[replace]`.

### 4.2 Status in 2b

- `status_auto`: what the area check says about the final geometry (always kept).
- `status_stage02b`: `[status]` entry if listed (`status_origin = manual`),
  else **`ok` for any curated geometry** - vertex edit, copy, replace, force,
  moved outlet (`status_origin = revision`) - else `status_auto`
  (`status_origin = auto`). A failed re-run keeps the stage 02 geometry and
  is not curated.
- `area_ratio_ana` and `area_flag_ratio` (the tolerance applied) stay on
  catchments and sites for later evaluation.
- Method columns: `source` (`gcf`, `copy`, `replace`), `catchment_source`,
  `revision` (e.g. `outlet moved in sites_stage02 + force_headwaters`,
  `copy from 87035000 (stage 02b 43_rs)`, `replace from guaiba.fgb`).

### 4.3 Output

`outputs/stage_02/<run>/ana_hidro_stage_02b_<run>.gpkg` (rebuilt on every run,
styled):

| Layer | Content |
|---|---|
| `sites`, `sites_stage02b`, `sites_pending_stage_02b` | stations with status, area, ratio, tolerance, method |
| `catchments_stage02b` | all final catchments |
| `catchments_stage02b_<status>` | the same, one layer per final status (visual inspection) |
| `outlets_moved` | moved outlet points, with distance to the ANA point |

Forced / moved re-runs go to `intermediate/<run>/rerun_02b/` (recreated each run).

The report lists: status counts and changes, manual edits, each force type,
moved outlets, copies, replacements, and every manual status that disagrees
with `status_auto`.

---

## 5. Helpers

```bash
# embed QGIS styles (catchments: no fill, light green 0.3 mm border;
# sites: 2.5 mm circles, white 0.26 mm border, colored by status)
python style_gpkg.py outputs/stage_02/*/ana_hidro_stage_02*_*.gpkg

# copy catchment area, ratio (in place) into the sites layers of stage 02 / 02b files
python join_site_areas.py outputs/stage_02/*/ana_hidro_stage_02*_*.gpkg

# synthesis across states: status shares + difficulty rank
# -> outputs/stage_02/synthesis_stage02b.png / .csv / .md
python synthesis_stage02b.py
```

Difficulty = share of the algorithm's own Stage 2 catchments (before
revision) that pass the area check, among stations with an ANA area.

`stage_02b.py` styles its own output. `stage_02.py` collect rewrites its file,
so run `style_gpkg.py` after a collect. Close the files in QGIS before
`join_site_areas.py` (it writes in place).

---

## 6. Files

```
applications/ana-hidro/
  README.md                  this file
  .gitignore                 keeps specs/ and outputs/ out of git
  stage_01.py                stage 1
  stage_02.py                stage 2 (prepare / run / collect), shared status helpers
  stage_02_batch.py          stage 2 for all states (make configs / run in parallel)
  stage_02b.py               stage 2b revision
  init_revision_stage02b.py  empty revision_stage02b.toml per state (no overwrite)
  style_gpkg.py              embed QGIS styles
  join_site_areas.py         area + ratio into sites layers, in place
  synthesis_stage02b.py      status shares + difficulty rank across states
  specs.example/             versioned configs, mock data paths
    stage_01.json
    stage_02_rs.json
  specs/                     your configs (not versioned)
    stage_01.json
    stage_02_rs.json         template for all states
    stage_02/<run>.json      one per state (generated)
  outputs/
    stage_01/                ana_hidro_stage_01.gpkg, report
    stage_02/<run>/          stage 02 gpkg (EDITED BY HAND), stage 02b gpkg, reports,
                             revision_stage02b.toml, hand-made *.fgb
    stage_02/intermediate/<run>/
                             outlets.gpkg, gcf_spec.json, run.log,
                             gcf/catchments.gpkg   <- needed to detect manual edits
                             gcf/outlets/          per-outlet working folders (large, rebuildable)
                             rerun_02b/            forced / moved re-runs (rebuilt by 2b)
    stage_02/batch_logs/     console log per state
    stage_02/synthesis_stage02b.*
```

`get_catchments_fast.py` (repo root) gained an optional `outlets_db` key for
this project, so outlet points need not be written into the basin database.

---

## 7. Backup and moving

Git only holds the code. What cannot be regenerated: your `specs/`, every
`revision_stage02b.toml`, the hand-made `*.fgb`/`*.qmd` files, and the stage 02
GeoPackages (they hold your vertex edits and moved points). Stage 2b also
needs the algorithm output `intermediate/<run>/gcf/catchments.gpkg` to tell
your edits apart from the algorithm's geometry.

The per-outlet folders (`gcf/outlets/`, ~10 GB) and `rerun_02b/` are not
needed. Everything else is ~600 MB on disk, ~230 MB compressed. From this
folder:

```bash
tar czf ../ana-hidro-data.tgz \
    --exclude='outputs/stage_02/intermediate/*/gcf/outlets' \
    --exclude='outputs/stage_02/intermediate/*/rerun_02b' \
    specs outputs
```

On the new machine:

1. Clone the repo, create the venv, `pip install -r requirements.txt`.
2. Unpack `ana-hidro-data.tgz` into `applications/ana-hidro/`.
3. Make the input data available (section 1). If any path differs, update
   `specs/stage_01.json` and `specs/stage_02_rs.json`.
4. `gcf_spec.json` holds **absolute paths** (input data, `dst_dir`,
   `outlets_db`). If the repo path or data paths change, regenerate the
   configs and specs without touching results or edits:
   ```bash
   python stage_02_batch.py make --template specs/stage_02_rs.json   # state configs
   for c in specs/stage_02/*.json; do python stage_02.py --config $c --step prepare; done
   ```
   `prepare` rewrites only `outlets.gpkg` and `gcf_spec.json` (same stations,
   new paths); it does not touch `gcf/` or the stage 02 files.
5. Check one state: `python stage_02b.py --config specs/stage_02/43_rs.json`
   should report the same counts as before (RS: 418 catchments, 4 edited,
   24 forced, 5 outlets moved, 8 copied, 24 replaced, 13 manual status).

---

## 8. Status (2026-11-12)

- Stage 1: done (970 catchments).
- Stage 2: done for all 33 runs (6,497 catchments, 4 `no hydrobasin`).
- Stage 2b: run for all states; RS revised (414 / 418 `ok`), other states
  have empty revision files. Algorithm pass rate 81.8% of stations with an
  ANA area (`synthesis_stage02b.md`).
- Stage 3: to do - merge stage 1 + stage 2b of all states, final check with
  the area columns of all sources, script to append the catchment layer into
  the ANA-Hidro database with automatic backup.
