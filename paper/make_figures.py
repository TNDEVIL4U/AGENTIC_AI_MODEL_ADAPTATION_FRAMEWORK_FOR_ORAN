"""Figures 2-5 of the paper, drawn from the results.json that scripts/paper_experiments.py
writes. Vector PDFs at IEEE single-column width (3.5 in), fonts embedded as TrueType.

Usage:
    python scripts/paper_experiments.py --out paper
    python paper/make_figures.py paper/results.json paper/figures
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "font.family": "serif", "font.serif": ["STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix", "font.size": 8, "axes.labelsize": 8, "axes.titlesize": 8,
    "legend.fontsize": 7, "xtick.labelsize": 7, "ytick.labelsize": 7,
    "pdf.fonttype": 42, "ps.fonttype": 42, "axes.linewidth": 0.6,
    "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
})
WIDTH = 3.5
GREY, BLUE, ORANGE = "0.55", "#1f5aa6", "#d9822b"


def rmse_by_action(rem: list[dict], out: Path) -> None:
    ids = [r["id"] for r in rem]
    x = np.arange(len(ids))
    fig, ax = plt.subplots(figsize=(WIDTH, 1.9))
    for k, (key, label, color) in enumerate((("rmse_none", "No remediation", GREY),
                                             ("rmse_finetune", "Finetune", BLUE),
                                             ("rmse_regenerate", "Regenerate", ORANGE))):
        ax.bar(x + (k - 1) * 0.27, [r[key] for r in rem], 0.27, label=label, color=color,
               linewidth=0)
    ax.set_yscale("log")
    ax.set_xticks(x, ids)
    ax.set_ylabel("Held-out RMSE (log)")
    ax.set_xlabel("Drift scenario")
    ax.axvline(2.5, color="0.3", linewidth=0.5, linestyle=":")
    ax.text(1.0, 0.97, "$P(y|X)$ unchanged", transform=ax.get_xaxis_transform(),
            ha="center", va="top", fontsize=6.5)
    ax.text(5.0, 0.97, "$P(y|X)$ changed", transform=ax.get_xaxis_transform(),
            ha="center", va="top", fontsize=6.5)
    ax.legend(ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.22), frameon=False)
    ax.grid(axis="y", linewidth=0.3, alpha=0.5)
    fig.savefig(out / "fig2_rmse.pdf")
    plt.close(fig)


def gain_vs_psi(rem: list[dict], out: Path) -> None:
    x = np.arange(len(rem))
    gains = [r["gain_pct"] for r in rem]
    fig, ax = plt.subplots(figsize=(WIDTH, 1.8))
    bars = ax.bar(x, gains, 0.6, color=[ORANGE if r["concept"] else GREY for r in rem],
                  linewidth=0.4, edgecolor="0.2")
    for bar, r in zip(bars, rem, strict=True):
        if not r["concept"]:
            bar.set_hatch("////")
        ax.text(bar.get_x() + bar.get_width() / 2, max(r["gain_pct"], 0) + 2,
                f'{r["gain_pct"]:.1f}', ha="center", va="bottom", fontsize=6)
    ax.set_xticks(x, [f'{r["id"]}\n{r["max_psi"]:.2f}' for r in rem])
    ax.text(-0.02, -0.215, "PSI", transform=ax.transAxes, ha="right", va="center", fontsize=7)
    ax.set_ylabel("RMSE reduction (%)")
    ax.set_ylim(min(-8, min(gains) - 4), 112)
    ax.axhline(0, color="0.2", linewidth=0.5)
    ax.grid(axis="y", linewidth=0.3, alpha=0.5)
    fig.savefig(out / "fig3_gain.pdf")
    plt.close(fig)


def bootstrap(gate: list[dict], out: Path) -> None:
    picks = [g for g in gate if g["candidate"] in ("SGD, other seed", "boosted stumps")]
    fig, axes = plt.subplots(len(picks), 1, figsize=(WIDTH, 2.4), sharex=True)
    for ax, g in zip(axes, picks, strict=True):
        d = 1e3 * np.asarray(g["diffs"])
        ax.hist(d, bins=40, color=BLUE if g["verdict"] == "ACCEPT" else GREY, linewidth=0)
        for v in (1e3 * g["ci_low"], 1e3 * g["ci_high"]):
            ax.axvline(v, color="0.1", linewidth=0.8)
        ax.axvline(0, color="#b22222", linewidth=0.8, linestyle="--")
        ax.set_ylabel("Resamples")
        ax.set_title(f'{g["candidate"]}: $\\Delta$ = {1e3 * g["delta"]:+.2f}, '
                     f'{100 * g["share_above_zero"]:.1f} % of resamples > 0, {g["verdict"]}',
                     loc="left", fontsize=7)
        ax.grid(axis="y", linewidth=0.3, alpha=0.5)
    axes[-1].set_xlabel("Improvement $\\Delta$ over incumbent ($\\times 10^{-3}$ RMSE)")
    fig.tight_layout(h_pad=0.4)
    fig.savefig(out / "fig4_bootstrap.pdf")
    plt.close(fig)


def cost(costs: dict[str, dict[str, float]], out: Path) -> None:
    stages = list(costs)
    means = [costs[s]["mean_ms"] for s in stages]
    stds = [costs[s]["std_ms"] for s in stages]
    total = sum(means)
    fig, ax = plt.subplots(figsize=(WIDTH, 1.5))
    y = np.arange(len(stages))[::-1]
    ax.barh(y, means, xerr=stds, color=BLUE, height=0.55,
            error_kw={"linewidth": 0.6, "capsize": 2})
    for yi, m, sd in zip(y, means, stds, strict=True):
        ax.text(m + sd + max(means) * 0.04, yi, f"{m:.2f} ms ({100 * m / total:.1f} %)",
                va="center", fontsize=6.5)
    ax.set_yticks(y, [s.capitalize() for s in stages])
    ax.set_xlabel("Wall time per run (ms), mean $\\pm$ s.d. of 5 runs")
    ax.set_xlim(0, max(means) * 1.75)
    ax.grid(axis="x", linewidth=0.3, alpha=0.5)
    fig.savefig(out / "fig5_cost.pdf")
    plt.close(fig)


def main() -> int:
    results = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    rmse_by_action(results["remediation"], out)
    gain_vs_psi(results["remediation"], out)
    bootstrap(results["gate"], out)
    cost(results["cost"], out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
