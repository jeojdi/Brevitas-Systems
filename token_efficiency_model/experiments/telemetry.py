import csv
import os
from typing import List

try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None


def save_metrics_csv(path: str, episodes: List[int], values: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        # header
        headers = ["episode"] + list(values.keys())
        writer.writerow(headers)
        for i, ep in enumerate(episodes):
            row = [ep] + [values[k][i] for k in values.keys()]
            writer.writerow(row)


def plot_savings(episodes: List[int], savings: List[float], outpath: str):
    if plt is None:
        print("matplotlib not available; skipping plots")
        return
    plt.figure(figsize=(8, 3))
    plt.plot(episodes, savings, label="Savings %")
    plt.xlabel("Episode")
    plt.ylabel("Savings (%)")
    plt.title("Token Savings over Episodes")
    plt.grid(True)
    plt.legend()
    os.makedirs(os.path.dirname(outpath), exist_ok=True)
    plt.tight_layout()
    plt.savefig(outpath)
    plt.close()
