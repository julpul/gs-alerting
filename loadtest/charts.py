"""PNG charts for the load test report (matplotlib, headless)."""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any


def histogram_bins(values: list[float], bins: int = 40) -> tuple[list[float], list[int]]:
    """Equal-width bins up to the p99 (outliers are clipped into the last bin)."""
    if not values:
        return [], []
    ordered = sorted(values)
    hi = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    hi = hi if hi > 0 else 1.0
    width = hi / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, max(0, int(v / width)))] += 1
    return [i * width for i in range(bins)], counts


def render_charts(data: Any, outdir: Path) -> list[str]:
    """Write PNG charts into outdir, return their file names (relative)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    written: list[str] = []

    def save(fig: Any, name: str) -> None:
        fig.tight_layout()
        fig.savefig(outdir / name, dpi=130)
        plt.close(fig)
        written.append(name)

    samples = {c: v for c, v in (data.delivery_samples or {}).items() if v}
    if samples:
        fig, axes = plt.subplots(1, len(samples), figsize=(4.2 * len(samples), 3.6), squeeze=False)
        for ax, (channel, values) in zip(axes[0], sorted(samples.items()), strict=True):
            edges, counts = histogram_bins(values)
            width = edges[1] - edges[0] if len(edges) > 1 else 1.0
            ax.bar(edges, counts, width=width, align="edge", color="#2a6fdb")
            ax.axvline(statistics.median(values), color="#e8590c", linestyle="--", label="p50")
            ax.set_title(f"{channel}  (n={len(values):,})")
            ax.set_xlabel("ingest -> delivered (ms, clipped at p99)")
            ax.legend(frameon=False)
        axes[0][0].set_ylabel("deliveries")
        save(fig, "latency_histogram.png")

    if data.t5:
        fig, ax = plt.subplots(figsize=(5, 3.6))
        pts = [(r["workers"], r.get("maxSustainedEvPerSec") or 0) for r in data.t5]
        ax.bar([str(w) for w, _ in pts], [v for _, v in pts], color="#2a6fdb")
        for i, (_, v) in enumerate(pts):
            ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom")
        ax.set_xlabel("worker replicas")
        ax.set_ylabel("max sustained ev/s")
        ax.set_title("Throughput vs replicas")
        save(fig, "throughput_vs_replicas.png")

    if data.prom and data.prom.queue_depth:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        t0 = min(t for pts in data.prom.queue_depth.values() for t, _ in pts)
        for queue, pts in sorted(data.prom.queue_depth.items()):
            ax.plot([t - t0 for t, _ in pts], [v for _, v in pts], label=queue)
        ax.set_xlabel("seconds since window start")
        ax.set_ylabel("messages ready")
        ax.set_title("Queue depth")
        ax.legend(frameon=False, fontsize=8)
        save(fig, "queue_depth.png")

    if data.prom and data.prom.ingest_rate:
        fig, ax = plt.subplots(figsize=(8, 3.2))
        t0 = data.prom.ingest_rate[0][0]
        ax.plot(
            [t - t0 for t, _ in data.prom.ingest_rate], [v for _, v in data.prom.ingest_rate], color="#2a6fdb"
        )
        ax.set_xlabel("seconds since window start")
        ax.set_ylabel("ingested ev/s")
        ax.set_title("Ingest rate")
        save(fig, "ingest_rate.png")
    return written
