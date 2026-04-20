# Safeguarding Plan

This document describes the concrete mechanisms in place to ensure responsible deployment of the Immich Tagger ML system across fairness, explainability, transparency, privacy, accountability, and robustness.

---

## 1. Fairness

**Goal**: Ensure the model performs equitably across all 80 COCO object classes and detect class-level performance degradation.

### Mechanisms

- **Per-class precision/recall logged to MLflow** (`training/train.py`): After each training run, per-class precision and recall are computed on the validation set and logged as `class_precision/<class_name>` and `class_recall/<class_name>`. This allows tracking which classes the model struggles with across training runs.

- **Prediction distribution monitoring** (`app/main.py`): The `tagger_predictions_total` Prometheus counter is labeled by `label` and `environment`, enabling Grafana dashboards to visualize prediction distribution across classes. Anomalous concentration or absence of specific classes is visible in real time.

- **Feedback-based detection**: The `/feedback` endpoint records user corrections (tag additions/deletions). The `HighFeedbackDeletionRate` alert fires when >50% of feedback is deletions, indicating the model is systematically producing poor predictions for certain inputs.

### Limitations

- COCO is object-centric; fairness across demographic groups (e.g., skin tone for "person" class) is not directly measured. For a production system processing people's photos, this would require dedicated fairness benchmarks.

---

## 2. Explainability

**Goal**: Provide insight into why the model made a specific prediction.

### Mechanisms

- **Top-k class probabilities per prediction** (`app/main.py`): Every prediction returns the top-k classes with their confidence scores, not just a single label. This is logged to MinIO via `_log_prediction_to_minio()` with the full `confidence_scores` dictionary.

- **Confidence score histograms**: The `tagger_prediction_confidence` Prometheus histogram tracks the distribution of confidence scores across all predictions, making it visible when the model is uncertain (many predictions clustered near the threshold).

- **Configurable confidence threshold** (`DEFAULT_MIN_SCORE` env var, `MIN_SCORE` in auto-tagger): Low-confidence predictions are filtered out and tracked separately via `tagger_filtered_predictions_total`. This makes the filtering decision explicit and auditable.

### Limitations

- No gradient-based attribution (e.g., Grad-CAM) is implemented. For a ResNet50 multi-label classifier, this would be a natural next step to explain which image regions drive each class prediction.

---

## 3. Transparency

**Goal**: Maintain a complete audit trail from data to prediction to deployment.

### Mechanisms

- **Prediction audit log in MinIO**: Every prediction from `/predict/legacy` is logged to `feedback/uploads/<request_id>.json` in MinIO, containing: `request_id`, `image_uri`, `model_version`, `environment`, `timestamp`, `predicted_tags`, and `confidence_scores`.

- **Model lineage via MLflow**: Each training run records full hyperparameters, metrics, and artifacts. The model registry tracks `run_id -> model_version -> alias (staging/production)`. Quality gate decisions (`quality_gate_passed`, comparison metrics) are logged as MLflow metrics.

- **Deployment history via ArgoCD**: All deployments are git-based (GitOps). ArgoCD syncs from the `main` branch, providing a complete history of what was deployed and when via `git log` on `mlops/k8s/*/`.

- **Argo Workflows audit trail**: Training workflows are recorded in Argo Workflows with full step logs, inputs, outputs, and timing.

---

## 4. Privacy

**Goal**: Minimize personal data exposure and control access to stored data.

### Mechanisms

- **No PII in prediction logs**: Prediction logs contain only `request_id` (UUID), not user identity. The `/feedback` endpoint accepts a `user_id` but this is an opaque identifier, not a name or email.

- **Image data isolation**: Images are stored in MinIO with access controls (credentials via Kubernetes secrets). Images are not included in prediction logs — only the `image_uri` reference is stored.

- **Network policies**: Kubernetes network policies restrict inter-namespace communication. The tagger pods can only reach MinIO and MLflow within `immich-platform`, not arbitrary external services.

- **Secret management**: MinIO credentials, Immich API keys, and other secrets are stored as Kubernetes Secrets (referenced via `secretKeyRef`), not hardcoded in deployment manifests or application code.

---

## 5. Accountability

**Goal**: Ensure all actions — training, deployment, model promotion — are traceable to a decision.

### Mechanisms

- **MLflow experiment tracking**: Every training run is logged with a unique `run_id`, full configuration, and metrics. The model registry provides a versioned history of all registered models.

- **Quality gates** (`training/flow.py`): Models must pass minimum metric thresholds and outperform the current production model before registration. Gate decisions are logged to MLflow (`quality_gate_passed`, `production_metric`, `improvement`).

- **Git-based deployment**: ArgoCD enforces that all deployment changes go through git commits on `main`. There is no manual `kubectl apply` path — all changes are auditable via `git log`.

- **Argo Workflows**: Training pipelines run as Argo Workflows with step-level logging. Each workflow run is persisted with its full DAG, parameters, and outcomes.

---

## 6. Robustness

**Goal**: Ensure the system degrades gracefully under failures and recovers automatically.

### Mechanisms

- **Automated rollback** (`prometheus.yaml`, AlertManager + Argo Events): Critical serving alerts (`ProductionServingDown`, `ProductionHighErrorRate` >5%) carry `action: rollback` labels. AlertManager triggers Argo Events, which initiates an automated rollback to the previous known-good model version.

- **Confidence threshold filtering**:
  - Auto-tagger sidecar (`app/auto_tagger.py`): `MIN_SCORE=0.5` filters out low-confidence tags before writing to Immich.
  - Serving app (`app/main.py`): `DEFAULT_MIN_SCORE` env var provides a configurable threshold for the `/predict/legacy` endpoint. The `/predict` (Immich protocol) endpoint respects the `minScore` parameter from each request.
  - Filtered predictions are tracked via `tagger_filtered_predictions_total` for monitoring.

- **Horizontal Pod Autoscaler** (`mlops/k8s/production/templates/immich-tagger.yaml`): Production deployment scales from `minReplicas` to `maxReplicas` based on CPU utilization, with stabilization windows to prevent flapping.

- **Staged rollout pipeline**: New models progress through staging -> canary -> production namespaces, each with independent health checks and Prometheus monitoring. The canary environment catches issues before full production rollout.

- **Infrastructure alerts**: `NodeHighCPU`, `PodCrashLooping`, and `DeploymentReplicasMismatch` alerts detect infrastructure-level failures before they impact serving.

- **Data quality checks** (`data/soda-checks/checks.py`): Validates feedback events and uploaded data for field completeness, timestamp validity, duplicates, and skew. Training data is validated before retraining (minimum image count, annotation validity, class coverage).

- **Drift monitoring** (`data/drift-monitor/drift_monitor.py`): Monitors PSI (Population Stability Index) on image brightness distribution to detect data drift between training and production data.

---

## Summary

| Principle | Key Mechanism | Location |
|-----------|--------------|----------|
| Fairness | Per-class precision/recall in MLflow | `training/train.py`, `training/metrics.py` |
| Explainability | Top-k confidence scores per prediction | `app/main.py` |
| Transparency | Prediction audit log + MLflow lineage | MinIO `feedback/uploads/`, MLflow |
| Privacy | No PII in logs, secrets in K8s Secrets | `app/main.py`, K8s manifests |
| Accountability | Quality gates + GitOps + Argo Workflows | `training/flow.py`, ArgoCD |
| Robustness | Auto-rollback + HPA + staged rollout | Prometheus alerts, K8s configs |
