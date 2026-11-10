"""Plot the four audited single-image pilot conditions; never aggregate AP."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    result = json.loads((args.results / "pilot.json").read_text())
    rows = [row for row in result["attempts"] if row["condition"] in ("A", "B", "C", "D")
            and row["status"] == "complete"]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6), constrained_layout=True)
    for objective, label, color in (("image", "C3", "#176fa6"), ("combined", "C3 prime", "#c45b29")):
        points = sorted((row for row in rows if row["objective"] == objective), key=lambda row: row["noise_steps"])
        for ax, key, ylabel in zip(axes, ("estimated_bpp", "psnr_saved_png_db", "worker_wall_seconds"),
                                  ("Entropy-estimated bits / pixel", "Saved PNG PSNR (dB)", "Worker wall time (seconds)")):
            ax.plot([row["noise_steps"] for row in points], [row[key] for row in points],
                    "o-", color=color, label=label, linewidth=1.8)
            ax.set_xlabel("Noise steps (+ up to 50 STE)")
            ax.set_ylabel(ylabel)
            ax.set_xticks([500, 1000])
            ax.grid(alpha=0.22)
    axes[0].legend(frameon=False)
    fig.suptitle("One fixed calibration image; fixed lambda per method; rates are not matched", fontsize=11)
    fig.savefig(args.results / "budget_pilot.png", dpi=180)
    fig.savefig(args.results / "budget_pilot.pdf")
    plt.close(fig)


if __name__ == "__main__":
    main()
