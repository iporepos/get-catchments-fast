"""
Stage 2 batch - one get_catchments_fast run per state, in separate folders.

  make  write one stage_02.py config per state (specs/stage_02/<run>.json),
        copied from a template config (the calibration run), changing only
        run_name, ufs, out_dir and the output file names.
  run   run stage_02.py (all steps) for every config, `workers` at a time.
        A run whose report already exists is skipped, so an interrupted
        batch can be resumed; pass --force to redo everything.

Folders per run (<run> = "<IBGE code>_<uf>" for Brazilian states, e.g.
"43_rs", or "xx_<country>" for stations abroad):
    outputs/stage_02/<run>/ana_hidro_stage_02_<run>.gpkg
    outputs/stage_02/<run>/report_stage_02_<run>.md
    outputs/stage_02/intermediate/<run>/   (outlets, algorithm spec, run.log, results)

Usage:
    python stage_02_batch.py make --template specs/stage_02_rs.json
    python stage_02_batch.py run --workers 4
"""
import argparse
import json
import subprocess
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd

HERE = Path(__file__).resolve().parent
SPECS_DIR = HERE / "specs" / "stage_02"
LOG_DIR = HERE / "outputs" / "stage_02" / "batch_logs"

UF_CODES = {
    "RONDÔNIA": (11, "ro"), "ACRE": (12, "ac"), "AMAZONAS": (13, "am"), "RORAIMA": (14, "rr"),
    "PARÁ": (15, "pa"), "AMAPÁ": (16, "ap"), "TOCANTINS": (17, "to"), "MARANHÃO": (21, "ma"),
    "PIAUÍ": (22, "pi"), "CEARÁ": (23, "ce"), "RIO GRANDE DO NORTE": (24, "rn"), "PARAÍBA": (25, "pb"),
    "PERNAMBUCO": (26, "pe"), "ALAGOAS": (27, "al"), "SERGIPE": (28, "se"), "BAHIA": (29, "ba"),
    "MINAS GERAIS": (31, "mg"), "ESPÍRITO SANTO": (32, "es"), "RIO DE JANEIRO": (33, "rj"),
    "SÃO PAULO": (35, "sp"), "PARANÁ": (41, "pr"), "SANTA CATARINA": (42, "sc"),
    "RIO GRANDE DO SUL": (43, "rs"), "MATO GROSSO DO SUL": (50, "ms"), "MATO GROSSO": (51, "mt"),
    "GOIÁS": (52, "go"), "DISTRITO FEDERAL": (53, "df"),
}


def run_name(uf: str) -> str:
    if uf in UF_CODES:
        code, abbr = UF_CODES[uf]
        return f"{code}_{abbr}"
    ascii_name = unicodedata.normalize("NFKD", uf).encode("ascii", "ignore").decode()
    return "xx_" + "_".join(ascii_name.lower().split())


def cmd_make(args) -> None:
    template_path = Path(args.template).resolve()
    with open(template_path, encoding="utf-8") as f:
        template = json.load(f)

    stage01 = (template_path.parent / template["stage01_gpkg"]).resolve()
    pending = gpd.read_file(stage01, layer="sites_pending_stage_01", ignore_geometry=True)
    counts = pending["uf"].value_counts()
    if pending["uf"].isna().any():
        print(f"WARNING: {pending['uf'].isna().sum()} pending station(s) without uf are not in any run")

    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    for uf, n in counts.items():
        name = run_name(uf)
        cfg = dict(template)
        cfg.update({
            "run_name": name,
            "ufs": [uf],
            "stage01_gpkg": "../../outputs/stage_01/ana_hidro_stage_01.gpkg",
            "out_dir": f"../../outputs/stage_02/{name}",
            "intermediate_dir": "../../outputs/stage_02/intermediate",
            "out_gpkg": f"ana_hidro_stage_02_{name}.gpkg",
            "out_report": f"report_stage_02_{name}.md",
            "gcf_script": "../../../../get_catchments_fast.py",
        })
        out = SPECS_DIR / f"{name}.json"
        with open(out, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
        print(f"{out.relative_to(HERE)}  {uf}: {n} station(s)")


def run_one(config: Path, force: bool) -> tuple[str, str, float]:
    with open(config, encoding="utf-8") as f:
        cfg = json.load(f)
    report = (config.parent / cfg["out_dir"] / cfg["out_report"]).resolve()
    if report.exists() and not force:
        return config.stem, "skipped (done)", 0.0
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    with open(LOG_DIR / f"{config.stem}.log", "w", encoding="utf-8") as log:
        cmd = [sys.executable, str(HERE / "stage_02.py"), "--config", str(config)]
        if force:
            cmd.append("--force")
        proc = subprocess.run(cmd,
                              stdout=log, stderr=subprocess.STDOUT)
    status = "ok" if proc.returncode == 0 else f"FAILED (exit {proc.returncode})"
    return config.stem, status, time.monotonic() - t0


def cmd_run(args) -> None:
    configs = sorted(SPECS_DIR.glob("*.json"))
    if args.only:
        configs = [c for c in configs if c.stem in args.only]
    # largest runs first, so the long ones don't end up running alone at the end
    pending = gpd.read_file(HERE / "outputs" / "stage_01" / "ana_hidro_stage_01.gpkg",
                            layer="sites_pending_stage_01", ignore_geometry=True)["uf"].value_counts()
    sizes = {}
    for c in configs:
        with open(c, encoding="utf-8") as f:
            sizes[c] = pending.get(json.load(f)["ufs"][0], 0)
    configs.sort(key=lambda c: -sizes[c])

    print(f"{len(configs)} run(s), {args.workers} at a time; logs in {LOG_DIR}")
    t0 = time.monotonic()
    failed = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, c, args.force): c for c in configs}
        for i, fut in enumerate(as_completed(futures), 1):
            name, status, secs = fut.result()
            print(f"[{i}/{len(configs)}] {name}: {status} in {secs / 60:.1f} min "
                  f"(elapsed {(time.monotonic() - t0) / 60:.1f} min)", flush=True)
            if status.startswith("FAILED"):
                failed.append(name)
    print(f"Done in {(time.monotonic() - t0) / 60:.1f} min. Failed: {failed or 'none'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 2 batch: one run per state.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--template", required=True)
    r = sub.add_parser("run")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--only", nargs="*", help="run names to run (default: all)")
    r.add_argument("--force", action="store_true")
    args = parser.parse_args()
    {"make": cmd_make, "run": cmd_run}[args.cmd](args)
