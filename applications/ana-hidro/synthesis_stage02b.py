"""
Synthesis of Stage 2 / 2b across states: status shares and a difficulty rank.

Reads, for every state config in specs/stage_02/:
  - the Stage 2b file (current status, after revision), and
  - the Stage 2 file (the algorithm's own geometry, before revision).

Two measures per state:
  - shares of the current status (status_stage02b; stations left pending are
    counted as "pending");
  - difficulty: share of the algorithm's catchments that pass the area check
    (auto status of the Stage 2 geometry, with the config's tolerance bands),
    among stations that HAVE an ANA area. This is not changed by the revision,
    so states can be compared fairly whether revised or not.

Outputs (outputs/stage_02/):
  synthesis_stage02b.png   figure: stacked shares + difficulty rank
  synthesis_stage02b.csv   the table behind it
  synthesis_stage02b.md    ranked table

Usage:
    python synthesis_stage02b.py
"""
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from stage_02 import auto_status, load_config, measure

HERE = Path(__file__).resolve().parent
SPECS_DIR = HERE / "specs" / "stage_02"
OUT_DIR = HERE / "outputs" / "stage_02"
SMALL_N = 30  # fewer stations than this: rank shown but flagged as noisy

# status palette (reference instance: good / serious / critical + neutrals)
STATUS = [  # (status, label, color)
    ("ok", "ok", "#0ca30c"),
    ("probable overestimate", "probable overestimate", "#ec835a"),
    ("probable underestimate", "probable underestimate", "#d03b3b"),
    ("multiparts", "multiparts", "#fab219"),
    ("no ana area", "no ANA area", "#c3c2b7"),
    ("pending", "pending (no catchment)", "#52514e"),
]
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"


def state_row(config: Path) -> dict | None:
    cfg = load_config(config)
    run, out_dir = cfg["run_name"], cfg["out_dir"]
    f02 = cfg["out_gpkg"]
    f02b = out_dir / f"ana_hidro_stage_02b_{run}.gpkg"
    if not (f02.exists() and f02b.exists()):
        return None

    sites = gpd.read_file(f02b, layer="sites", ignore_geometry=True)
    sites = sites[sites["source_stage01"].isna()]  # Stage 2 stations only
    st = sites["status_stage02b"].where(sites["source_stage02b"].notna(), "pending")
    counts = st.value_counts()

    c02 = gpd.read_file(f02, layer="catchments_stage02")
    for col in ["n_polygons", "area_ana_km2"]:
        c02[col] = pd.to_numeric(c02[col], errors="coerce")
    c02 = measure(c02)
    auto = auto_status(c02, cfg["area_flag_ratio"], cfg["area_flag_bands"])
    judged = auto[auto != "no ana area"]

    row = {"run": run, "state": cfg["ufs"][0].title() if cfg["ufs"] else run, "n": len(sites)}
    for s, _, _ in STATUS:
        row[s] = int(counts.get(s, 0))
    row["n_judged_02"] = len(judged)
    row["ok_02"] = int((judged == "ok").sum())
    row["difficulty_ok_pct"] = round(100 * row["ok_02"] / len(judged), 1) if len(judged) else None
    row["revised"] = int((sites["status_origin"].isin(["manual", "revision"])).sum())
    return row


def main() -> None:
    rows = [r for c in sorted(SPECS_DIR.glob("*.json")) if (r := state_row(c))]
    df = pd.DataFrame(rows)
    df["code"] = df["run"].str.split("_").str[-1].str.upper()
    df.loc[df["run"].str.startswith("xx_"), "code"] = df["state"]
    df = df.sort_values(["difficulty_ok_pct", "n"], ascending=[False, False]).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_DIR / "synthesis_stage02b.csv", index=False)
    write_md(df)
    plot(df)


def write_md(df: pd.DataFrame) -> None:
    tot = df[[s for s, _, _ in STATUS] + ["n", "n_judged_02", "ok_02", "revised"]].sum()
    L = ["# Stage 2 / 2b synthesis by state", "",
         "Difficulty = share of the algorithm's Stage 2 catchments that pass the area check "
         "(tolerance bands of the config), among stations with an ANA area. Rank 1 = easiest. "
         f"States with fewer than {SMALL_N} stations are marked * (noisy).", "",
         f"All states: {tot['n']} Stage 2 stations; algorithm ok {tot['ok_02']} / {tot['n_judged_02']} "
         f"({100 * tot['ok_02'] / tot['n_judged_02']:.1f}%); currently ok {tot['ok']}; revised {tot['revised']}.", "",
         "| rank | state | n | algorithm ok % | ok | over | under | multi | no ANA area | pending | revised |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in df.iterrows():
        star = "*" if r["n"] < SMALL_N else ""
        L.append(f"| {r['rank']} | {r['state']}{star} | {r['n']} | {r['difficulty_ok_pct']} | {r['ok']} | "
                 f"{r['probable overestimate']} | {r['probable underestimate']} | {r['multiparts']} | "
                 f"{r['no ana area']} | {r['pending']} | {r['revised']} |")
    (OUT_DIR / "synthesis_stage02b.md").write_text("\n".join(L) + "\n", encoding="utf-8")


def plot(df: pd.DataFrame) -> None:
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9,
                         "axes.edgecolor": GRID, "text.color": INK})
    n = len(df)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 0.32 * n + 2.2), sharey=True,
                                   gridspec_kw={"width_ratios": [3, 2], "wspace": 0.08})
    fig.patch.set_facecolor(SURFACE)
    y = list(range(n))[::-1]  # rank 1 at the top
    labels = [f"{r.rank:>2}  {r.code}{'*' if r.n < SMALL_N else ''}  (n={r.n})" for r in df.itertuples()]

    # Panel 1: current status shares (100% stacked), 2px surface gap between segments
    left = pd.Series(0.0, index=df.index)
    for s, lab, col in STATUS:
        share = 100 * df[s] / df["n"]
        if share.sum() == 0:
            continue
        ax1.barh(y, share, left=left, height=0.72, color=col, edgecolor=SURFACE, linewidth=1.5, label=lab)
        left += share
    for yi, r in zip(y, df.itertuples()):
        ax1.text(100 * r.ok / r.n - 1.2, yi, f"{100 * r.ok / r.n:.0f}%", va="center", ha="right",
                 fontsize=7.5, color="#ffffff")
    ax1.set_xlim(0, 100)
    ax1.set_xlabel("share of Stage 2 stations (%) - current status after revision", color=INK2)
    ax1.set_title("Current status (Stage 2b)", loc="left", fontsize=11, color=INK)

    # Panel 2: difficulty rank (algorithm ok %), dot + stem from the national value
    nat = 100 * df["ok_02"].sum() / df["n_judged_02"].sum()
    ax2.axvline(nat, color=MUTED, linewidth=1, linestyle=(0, (3, 3)))
    ax2.text(nat, n - 0.2, f" all states {nat:.0f}%", color=MUTED, fontsize=8, va="bottom")
    for yi, r in zip(y, df.itertuples()):
        small = r.n < SMALL_N
        ax2.plot([nat, r.difficulty_ok_pct], [yi, yi], color=GRID, linewidth=2, zorder=1)
        ax2.scatter(r.difficulty_ok_pct, yi, s=46, color=MUTED if small else INK,
                    edgecolor=SURFACE, linewidth=1.5, zorder=2)
        right = r.difficulty_ok_pct >= nat  # label on the side away from the reference line
        ax2.text(r.difficulty_ok_pct + (1.2 if right else -1.2), yi, f"{r.difficulty_ok_pct:.0f}%",
                 va="center", ha="left" if right else "right", fontsize=7.5, color=MUTED if small else INK2)
    lo = min(40, df["difficulty_ok_pct"].min() - 8)
    ax2.set_xlim(lo, 104)
    ax2.set_xlabel("algorithm catchments passing the area check (%)", color=INK2)
    ax2.set_title("Difficulty rank (Stage 2 algorithm, before revision)", loc="left", fontsize=11, color=INK)

    for ax in (ax1, ax2):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        ax.tick_params(colors=MUTED, length=0)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
    ax1.set_yticks(y)
    ax1.set_yticklabels(labels, color=INK2, fontfamily="monospace", fontsize=8)

    h = fig.get_figheight()
    fig.subplots_adjust(left=0.14, right=0.985, top=1 - 0.95 / h, bottom=0.75 / h)
    handles, labs = ax1.get_legend_handles_labels()
    fig.legend(handles, labs, loc="upper left", ncol=len(labs), frameon=False, fontsize=8.5,
               bbox_to_anchor=(0.135, 1 - 0.08 / h))
    fig.text(0.01, 0.12 / h, f"Rank 1 = easiest. * fewer than {SMALL_N} stations (noisy). "
             "Area check: ratio to ANA area within 1.5x (2x below 100 km2); stations without an ANA area "
             "are not ranked.", color=MUTED, fontsize=7.5)
    fig.savefig(OUT_DIR / "synthesis_stage02b.png", dpi=160, facecolor=SURFACE)
    print(f"figure: {OUT_DIR / 'synthesis_stage02b.png'}")


if __name__ == "__main__":
    main()
