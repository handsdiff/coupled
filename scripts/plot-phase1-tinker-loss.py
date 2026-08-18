#!/usr/bin/env python3
"""Plot the Phase 1 mechanical-overfit loss directly from its run report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter  # noqa: E402


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--scale", choices=["log", "linear"], default="log")
    parser.add_argument("--verified-cost-usd", required=True)
    return parser.parse_args()


def readable_log_tick(value: float, _: int) -> str:
    if value >= 1:
        return f"{value:g}"
    if value >= 0.01:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{value:.3f}".rstrip("0").rstrip(".")


def main() -> int:
    arguments = parse_arguments()
    report = json.loads(arguments.input.read_text(encoding="utf-8"))
    if report.get("status") != "passed":
        raise SystemExit("input is not a passing Tinker smoke report")

    epochs = report["training"]["epochs"]
    epoch_numbers = [item["epoch"] for item in epochs]
    losses = [item["meanPreUpdateNLL"] for item in epochs]
    baseline = report["evaluations"]["baseline"]["meanNLL"]
    final = report["evaluations"]["trainedNLL"]["meanNLL"]
    exact = report["evaluations"]["trainedGeneration"]["exactTargets"]
    examples = report["evaluations"]["trainedGeneration"]["examples"]
    target_tokens = report["evaluations"]["trainedNLL"]["weightedTokens"]
    reduction = (1 - final / baseline) * 100

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 12,
            "axes.titlesize": 20,
            "axes.labelsize": 13,
            "axes.edgecolor": "#CBD5E1",
            "axes.linewidth": 1.0,
            "xtick.color": "#475569",
            "ytick.color": "#475569",
            "text.color": "#0F172A",
        }
    )

    figure, axis = plt.subplots(figsize=(12, 7.2), dpi=180)
    figure.patch.set_facecolor("white")
    axis.set_facecolor("white")

    training_color = "#2563EB"
    baseline_color = "#64748B"
    final_color = "#059669"

    axis.plot(
        epoch_numbers,
        losses,
        color=training_color,
        linewidth=2.4,
        marker="o",
        markersize=5.5,
        markerfacecolor="white",
        markeredgewidth=1.8,
        label="Epoch mean (pre-update)",
        zorder=3,
    )
    axis.scatter(
        [0],
        [baseline],
        s=78,
        facecolors="white",
        edgecolors=baseline_color,
        linewidths=2.2,
        label="Base model evaluation",
        zorder=4,
    )
    axis.scatter(
        [20.55],
        [final],
        s=92,
        marker="D",
        color=final_color,
        edgecolors="white",
        linewidths=1.2,
        label="Final checkpoint evaluation",
        zorder=5,
    )

    if arguments.scale == "log":
        axis.set_yscale("log")
        axis.yaxis.set_major_locator(LogLocator(base=10))
        axis.yaxis.set_major_formatter(FuncFormatter(readable_log_tick))
        axis.yaxis.set_minor_formatter(NullFormatter())
        axis.set_ylabel("Weighted token NLL (log scale)")
        scale_note = "Log scale"
    else:
        axis.set_ylim(bottom=0)
        axis.set_ylabel("Weighted token NLL")
        scale_note = "Linear scale"

    axis.set_xlim(-0.65, 21.35)
    axis.set_xticks([0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20])
    axis.set_xlabel("Training epoch")
    axis.grid(axis="y", which="major", color="#E2E8F0", linewidth=0.9)
    axis.grid(axis="y", which="minor", color="#F1F5F9", linewidth=0.6)
    axis.spines["top"].set_visible(False)
    axis.spines["right"].set_visible(False)

    figure.suptitle(
        "Phase 1 Training Loss — Mechanical Overfit Smoke Test",
        x=0.08,
        y=0.96,
        ha="left",
        fontweight="bold",
    )
    axis.set_title(
        "Qwen3.5-9B Base · Run 8 · 28 training examples",
        loc="left",
        fontsize=12.5,
        color="#475569",
        pad=18,
    )

    axis.annotate(
        f"Base NLL  {baseline:.3f}",
        xy=(0, baseline),
        xytext=(14, 6),
        textcoords="offset points",
        color=baseline_color,
        fontsize=11,
        fontweight="bold",
    )
    axis.annotate(
        f"Final NLL  {final:.4f}\n{reduction:.2f}% lower",
        xy=(20.55, final),
        xytext=(-14, 16),
        textcoords="offset points",
        ha="right",
        color=final_color,
        fontsize=11,
        fontweight="bold",
    )

    legend = axis.legend(
        loc="upper right",
        frameon=False,
        fontsize=10.5,
        handlelength=2.5,
    )
    for text in legend.get_texts():
        text.set_color("#334155")

    figure.text(
        0.08,
        0.025,
        (
            f"{scale_note} · {target_tokens} loss-bearing tokens · "
            f"{exact}/{examples} exact greedy targets · "
            f"verified cost ${arguments.verified_cost_usd}"
        ),
        ha="left",
        color="#64748B",
        fontsize=10.5,
    )
    figure.subplots_adjust(left=0.09, right=0.97, top=0.84, bottom=0.14)

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(arguments.output, bbox_inches="tight", facecolor="white")
    plt.close(figure)
    print(arguments.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
