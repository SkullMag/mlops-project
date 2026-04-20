# Implementation Plan — Remaining Work

## Current State (verified 2026-04-18)

**Cluster is live** — 3-node K8s, ArgoCD GitOps, Argo Workflows, staging/canary/production
with HPA, automated rollback via AlertManager + Argo Events. All pods healthy.

### What's Done

| Area | Status | Evidence |
|------|--------|----------|
| Core training pipeline (MinIO, COCO-80, MLflow URI) | DONE | `training/flow.py`, `train-model.yaml` |
| Prediction logging to MinIO | DONE | `app/main.py:_log_prediction_to_minio()` |
| `/feedback` endpoint | DONE | `app/main.py:feedback()` |
| Custom Prometheus metrics (predictions, confidence, latency, feedback) | DONE | `app/main.py` lines 70-95 |
| Grafana model quality dashboards (3 dashboards: infra, serving, model quality) | DONE | `grafana.yaml` — `ml-model-quality.json` with prediction distribution, confidence, feedback panels |
| Prometheus model quality alerts (LowPredictionConfidence, HighInferenceLatency, HighFeedbackDeletionRate, NoPredictions) | DONE | `prometheus.yaml` lines 185-228 |
| Infra alerts (NodeHighCPU, PodCrashLooping, DeploymentReplicasMismatch) | DONE | `prometheus.yaml` lines 116-142 |
| Serving alerts + rollback (ProductionServingDown, HighErrorRate, HighLatency) | DONE | `prometheus.yaml` lines 147-182 |
| Immich integration via auto-tagger sidecar | DONE | `app/auto_tagger.py` + `mlops/k8s/production/templates/auto-tagger.yaml` — polls Immich for new assets, classifies via tagger, writes `ml/*` tags. Running in cluster. |
| Data quality checks on feedback/upload events | DONE | `data/soda-checks/checks.py` — validates fields, timestamps, duplicates, skew |
| Drift monitor (PSI on image brightness) | DONE | `data/drift-monitor/drift_monitor.py` |
| Data pipeline K8s deployment | DONE | `mlops/k8s/data-pipeline/templates/` — batch-pipeline, ingestion, drift-monitor, soda-checks |

---

## What's Left (3 items)

### 1. Model Quality Gates (Training role — 3 pts)

**Problem**: `flow.py` registers every trained model unconditionally. The workflow's
`set-development-alias` and `trigger-build` steps always fire. No gating.

**Required**:

**Step 1.1: Add quality gate to `training/flow.py`**
- After training completes, check if best metric (F1 or mAP) meets a minimum threshold
- Only call `register_model()` if threshold is met
- Log `quality_gate_passed` = true/false as an MLflow metric
- If gate fails: write empty string to `/tmp/model_version`, log reason, exit early

**Step 1.2: Add comparison against production model**
- Before registering: query MLflow for the current `production` alias model's metrics
- New model must meet or exceed production model's key metric
- If first model (no production alias yet): skip comparison, just apply minimum threshold
- Log comparison results to MLflow (`production_metric`, `improvement`)

**Step 1.3: Add per-class metrics to `training/train.py`**
- During evaluation: compute and log per-class precision/recall to MLflow
- This also feeds the safeguarding plan (fairness)

**Files to modify**: `training/flow.py`, `training/train.py`
(Workflow yaml already handles empty `modelversion` correctly — steps 4/5 have `when` guards)

---

### 2. Training Data Quality Check (Data role — 3 pts, partial) — DONE

**Implemented** in `training/flow.py` (`validate_training_data()`, lines 76-163).

Runs after `download_coco()` and before training. Checks:
- Annotation file is valid JSON with required COCO structure (`images`, `annotations`, `categories`)
- Minimum image count (≥100)
- Minimum annotation count (≥100)
- All COCO classes represented in annotations (flags missing classes by name)
- No zero-byte image files

On failure: logs validation failure details to MLflow (experiment `ImmichTagger`, run name
`data-validation-failed`), writes empty `/tmp/model_version` so downstream workflow steps
skip, and aborts training.

---

### 3. Safeguarding Plan (Joint — required for full credit)

Completed:
- **Step 1.3 (per-class metrics)**: `training/metrics.py` — `per_class_precision_recall()`, `training/train.py` — logs `class_precision/<name>` and `class_recall/<name>` to MLflow after training
- **Step 3.1**: `SAFEGUARDING.md` written — documents fairness, explainability, transparency, privacy, accountability, robustness with concrete file/mechanism references
- **Step 3.2**: `app/main.py` — added `DEFAULT_MIN_SCORE` env var for `/predict/legacy` confidence threshold, added `tagger_filtered_predictions_total` Prometheus counter for filtered predictions

---

## Effort Summary

| Item | Effort | Impact |
|------|--------|--------|
| 1. Quality gates | Medium | 3 pts (training role) |
| 2. Training data validation | Low | Completes data role (3 pts) |
| ~~3. Safeguarding plan + doc~~ | ~~Low-Medium~~ | ~~Required for joint credit~~ DONE |

## Open Questions

1. **What minimum metric threshold to use for quality gate?** Depends on baseline model performance on COCO subset. Start with F1 >= 0.3 (multi-label COCO is hard) and adjust.
2. **Should quality gate comparison be relative or absolute?** Recommend: absolute minimum + must not regress >5% vs production.
