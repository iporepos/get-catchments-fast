"""
Copy the Stage 2 intermediate folder without the heavy, rebuildable parts,
for moving the results to another machine.

Kept, per run (<src>/<run>/):
  gcf/catchments.gpkg   algorithm output - Stage 2b needs it to detect manual
                        edits and does NOT regenerate it
  gcf_spec.json, outlets.gpkg, run.log
                        small; rebuilt by stage_02.py --step prepare, kept anyway
Skipped:
  gcf/outlets/          per-outlet working folders (~10 GB, nothing reads them)
  rerun_02b/            recreated by every stage_02b.py run

Usage:
    python copy_intermediate.py                       # outputs/stage_02/intermediate -> outputs/copy/intermediate
    python copy_intermediate.py --dst /some/where --force
"""
import argparse
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {("gcf", "outlets"), ("rerun_02b",)}  # relative to <src>/<run>/


def ignore_for(run_dir: Path):
    def ignore(folder: str, names: list[str]) -> list[str]:
        rel = Path(folder).relative_to(run_dir).parts
        return [n for n in names if (*rel, n) in SKIP_DIRS]
    return ignore


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def main(src: Path, dst: Path, force: bool) -> None:
    if not src.is_dir():
        raise FileNotFoundError(src)
    if dst.exists():
        if not force:
            raise FileExistsError(f"{dst} exists; use --force to replace it")
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    runs = sorted(p for p in src.iterdir() if p.is_dir())
    missing = []
    for run in runs:
        shutil.copytree(run, dst / run.name, ignore=ignore_for(run))
        if not (dst / run.name / "gcf" / "catchments.gpkg").exists():
            missing.append(run.name)
    for f in src.iterdir():  # loose files at the top level, if any
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    print(f"copied {len(runs)} runs: {src} -> {dst} ({dir_size(dst) / 1e6:.0f} MB)")
    if missing:
        print(f"WARNING: no gcf/catchments.gpkg in {len(missing)} run(s): {missing}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Copy the Stage 2 intermediate folder without the heavy parts.")
    ap.add_argument("--src", type=Path, default=HERE / "outputs/stage_02/intermediate")
    ap.add_argument("--dst", type=Path, default=HERE / "outputs/copy/intermediate")
    ap.add_argument("--force", action="store_true", help="replace an existing destination")
    a = ap.parse_args()
    main(a.src, a.dst, a.force)
