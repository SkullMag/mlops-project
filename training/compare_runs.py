import sys

import mlflow
import pandas as pd

TRACKING_URI = "http://mlflow:5000"
EXPERIMENT = "immich-auto-tagging"
K = 3

mlflow.set_tracking_uri(TRACKING_URI)
exp = mlflow.get_experiment_by_name(EXPERIMENT)
if not exp:
    print(f"Experiment '{EXPERIMENT}' not found")
    sys.exit(1)

runs = mlflow.search_runs(
    experiment_ids=[exp.experiment_id],
    filter_string="status = 'FINISHED'",
    order_by=[f"metrics.precision_at_{K} DESC"],
)

if runs.empty:
    print("No finished runs.")
    sys.exit(1)

cols = {
    "tags.mlflow.runName": "run_name",
    "params.model_type": "model",
    "params.optimizer": "optimizer",
    "params.learning_rate": "lr",
    "params.batch_size": "batch_size",
    f"metrics.precision_at_{K}": f"P@{K}",
    f"metrics.recall_at_{K}": f"R@{K}",
    f"metrics.f1_at_{K}": f"F1@{K}",
    "metrics.validation_loss": "val_loss",
    "metrics.total_training_time": "train_time_s",
}

available = {k: v for k, v in cols.items() if k in runs.columns}
table = runs[list(available.keys())].rename(columns=available)

for c in [f"P@{K}", f"R@{K}", f"F1@{K}", "val_loss"]:
    if c in table.columns:
        table[c] = table[c].map(lambda x: f"{x:.4f}" if pd.notna(x) else "-")
if "train_time_s" in table.columns:
    table["train_time_s"] = table["train_time_s"].map(
        lambda x: f"{x:.1f}" if pd.notna(x) else "-")

best = runs.loc[runs[f"metrics.precision_at_{K}"].idxmax(), "tags.mlflow.runName"]

print(f"\n--- {EXPERIMENT} ---\n")
print(table.to_string(index=False))
print(f"\nBest by precision_at_{K}: {best}\n")

table.to_csv("training_runs_comparison.csv", index=False)
print("Saved to training_runs_comparison.csv")
