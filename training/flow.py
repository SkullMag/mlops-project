"""Training orchestrator. Two modes selected via the config's data.source:
  - "coco" (default): pull COCO data from MinIO and train from scratch.
  - "feedback": pull the latest READY user-feedback dataset from MinIO,
    initialize from the current production checkpoint, and fine-tune.
Both paths share the quality gate and registration logic.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import boto3
import mlflow
import yaml


MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://minio.immich-platform.svc.cluster.local:9000")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY", "")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")
MLFLOW_TRACKING_URI = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow.immich-platform.svc.cluster.local:8000")
MODEL_NAME = "ImmichTaggerModel"
CONFIG_TEMPLATE = os.environ.get("TRAIN_CONFIG", "configs/resnet50_adam.yaml")
MIN_F1_THRESHOLD = float(os.environ.get("MIN_F1_THRESHOLD", "0.3"))
MAX_REGRESSION_PCT = float(os.environ.get("MAX_REGRESSION_PCT", "0.05"))

COCO_NUM_CLASSES = 80
MIN_IMAGE_COUNT = 100
MIN_ANNOTATION_COUNT = 100


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )


def _s3_env():
    return {
        **os.environ,
        "AWS_ACCESS_KEY_ID": MINIO_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": MINIO_SECRET_KEY,
        "AWS_DEFAULT_REGION": "us-east-1",
    }


def s3_sync(src, dest):
    """Run aws s3 sync from MinIO to local directory."""
    cmd = [
        "aws", "s3", "sync", src, dest,
        "--endpoint-url", MINIO_ENDPOINT,
    ]
    print(f"  aws s3 sync {src} {dest}", flush=True)
    result = subprocess.run(cmd, env=_s3_env(), text=True)
    if result.returncode != 0:
        raise RuntimeError(f"aws s3 sync failed (exit code {result.returncode})")


def s3_download_subset(prefix, dest, max_files):
    """Download only max_files files from an S3 prefix using boto3 (for smoke tests)."""
    os.makedirs(dest, exist_ok=True)
    s3 = get_s3()
    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix, PaginationConfig={"MaxItems": max_files}):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel_path = key[len(prefix):]
            if not rel_path:
                continue
            local_path = os.path.join(dest, rel_path)
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            s3.download_file(BUCKET_NAME, key, local_path)
            count += 1
    print(f"  downloaded {count} files from s3://{BUCKET_NAME}/{prefix}", flush=True)


def download_coco(data_dir, max_files=None):
    """Sync COCO images and annotations from MinIO.

    If max_files is set, only download that many images per split (for smoke tests).
    Annotations are always fully synced.
    """
    print(f"Syncing COCO data from MinIO ({BUCKET_NAME}) ...")
    bucket = f"s3://{BUCKET_NAME}"

    # Always sync all annotations (small)
    s3_sync(f"{bucket}/coco/annotations/", os.path.join(data_dir, "annotations"))

    if max_files:
        s3_download_subset("coco/train2017/", os.path.join(data_dir, "train2017"), max_files)
        s3_download_subset("coco/val2017/", os.path.join(data_dir, "val2017"), max_files)
    else:
        s3_sync(f"{bucket}/coco/train2017/", os.path.join(data_dir, "train2017"))
        s3_sync(f"{bucket}/coco/val2017/", os.path.join(data_dir, "val2017"))


def validate_training_data(data_dir):
    """Validate the compiled training dataset before training.

    Checks:
      1. Annotation file is valid JSON with required COCO structure
      2. Minimum image count
      3. Minimum annotation count
      4. All 80 COCO classes are represented in annotations
      5. No zero-byte images
    Returns (True, summary_dict) on success, (False, summary_dict) on failure.
    """
    img_dirs = [os.path.join(data_dir, d) for d in ("train2017", "val2017")
                if os.path.isdir(os.path.join(data_dir, d))]
    ann_dir = os.path.join(data_dir, "annotations")
    failures = []
    summary = {}

    # --- 1. Annotation file validity ---
    ann_files = [f for f in os.listdir(ann_dir) if f.endswith(".json")]
    if not ann_files:
        failures.append("No annotation JSON files found")
        summary["annotation_files"] = 0
        # Can't continue other checks without annotations
        summary["failures"] = failures
        return False, summary

    ann_path = os.path.join(ann_dir, ann_files[0])
    try:
        with open(ann_path) as f:
            coco = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        failures.append(f"Annotation file is not valid JSON: {e}")
        summary["failures"] = failures
        return False, summary

    for required_key in ("images", "annotations", "categories"):
        if required_key not in coco:
            failures.append(f"Annotation file missing required key: '{required_key}'")

    if failures:
        summary["failures"] = failures
        return False, summary

    # --- 2. Image count ---
    image_files = []
    for img_dir in img_dirs:
        image_files.extend(
            os.path.join(img_dir, f) for f in os.listdir(img_dir)
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        )
    summary["image_count"] = len(image_files)
    if len(image_files) < MIN_IMAGE_COUNT:
        failures.append(
            f"Too few images: {len(image_files)} (minimum {MIN_IMAGE_COUNT})"
        )

    # --- 3. Annotation count ---
    summary["annotation_count"] = len(coco["annotations"])
    if len(coco["annotations"]) < MIN_ANNOTATION_COUNT:
        failures.append(
            f"Too few annotations: {len(coco['annotations'])} (minimum {MIN_ANNOTATION_COUNT})"
        )

    # --- 4. Class coverage ---
    expected_cat_ids = {cat["id"] for cat in coco["categories"]}
    summary["expected_classes"] = len(expected_cat_ids)
    annotated_cat_ids = {ann["category_id"] for ann in coco["annotations"]}
    missing_cats = expected_cat_ids - annotated_cat_ids
    summary["classes_with_annotations"] = len(expected_cat_ids - missing_cats)
    if missing_cats:
        missing_names = [
            cat["name"] for cat in coco["categories"] if cat["id"] in missing_cats
        ]
        failures.append(
            f"{len(missing_cats)} classes have zero annotations: {missing_names[:10]}"
            + ("..." if len(missing_names) > 10 else "")
        )

    # --- 5. Zero-byte images ---
    zero_byte = [f for f in image_files if os.path.getsize(f) == 0]
    summary["zero_byte_images"] = len(zero_byte)
    if zero_byte:
        failures.append(f"{len(zero_byte)} zero-byte image files detected")

    # --- Result ---
    summary["failures"] = failures
    passed = len(failures) == 0
    return passed, summary


def write_config(template_path, data_dir, output_path):
    """Write training config with data paths pointing to downloaded data."""
    with open(template_path) as f:
        cfg = yaml.safe_load(f)

    train_img_dir = os.path.join(data_dir, "train2017")
    val_img_dir = os.path.join(data_dir, "val2017")
    ann_dir = os.path.join(data_dir, "annotations")

    # Find annotation files (instances_train2017.json and instances_val2017.json)
    ann_files = sorted(f for f in os.listdir(ann_dir) if f.endswith(".json"))
    if not ann_files:
        raise RuntimeError(f"No annotation JSON files in {ann_dir}")

    # Match instances annotation files to train/val splits
    train_ann = next((os.path.join(ann_dir, f) for f in ann_files if "instances" in f and "train" in f), None)
    val_ann = next((os.path.join(ann_dir, f) for f in ann_files if "instances" in f and "val" in f), None)
    # Fallback: use the first annotation file for both if split-specific files not found
    if not train_ann:
        train_ann = os.path.join(ann_dir, ann_files[0])
    if not val_ann:
        val_ann = train_ann

    cfg["data"]["train_img_dir"] = train_img_dir
    cfg["data"]["train_ann_file"] = train_ann
    cfg["data"]["val_img_dir"] = val_img_dir
    cfg["data"]["val_ann_file"] = val_ann
    cfg["mlflow"]["tracking_uri"] = MLFLOW_TRACKING_URI

    with open(output_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)

    print(f"Config written to {output_path}")
    return cfg


def write_feedback_config(template_path, feedback_dir, output_path):
    """Patch a feedback-mode config with the current MLflow URI and feedback_dir."""
    with open(template_path) as f:
        cfg = yaml.safe_load(f)
    cfg["data"]["feedback_dir"] = feedback_dir
    cfg["mlflow"]["tracking_uri"] = MLFLOW_TRACKING_URI
    with open(output_path, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"Config written to {output_path}")
    return cfg


def download_production_checkpoint(dest_dir):
    """Download the .pth artifact for the current production model. Returns the
    local path, or None if no production model is registered yet (cold start)."""
    client = mlflow.tracking.MlflowClient()
    try:
        mv = client.get_model_version_by_alias(MODEL_NAME, "production")
    except mlflow.exceptions.MlflowException:
        return None
    artifacts = client.list_artifacts(mv.run_id)
    pth = next((a.path for a in artifacts if a.path.endswith(".pth")), None)
    if pth is None:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    local = client.download_artifacts(mv.run_id, pth, dest_dir)
    print(f"  production checkpoint downloaded to {local}", flush=True)
    return local


def prepare_feedback_run(cfg):
    """Download latest READY feedback dataset, validate, fetch production weights.

    Returns (config_path, summary) on success, or (None, summary) on failure.
    """
    from feedback_dataset import (
        download_feedback_dataset,
        find_latest_ready_version,
        validate_feedback_data,
    )

    feedback_dir = cfg["data"].get("feedback_dir", "/mnt/workspace/feedback_data")
    s3 = get_s3()

    print(f"Looking for latest READY dataset in s3://{BUCKET_NAME}/datasets/ ...")
    version = find_latest_ready_version(s3, BUCKET_NAME)
    if not version:
        return None, {"failures": ["No READY dataset version found in MinIO"]}
    print(f"  using dataset version v{version}")

    download_feedback_dataset(s3, BUCKET_NAME, version, feedback_dir)

    passed, summary = validate_feedback_data(feedback_dir)
    summary["dataset_version"] = version
    if not passed:
        return None, summary

    ckpt = download_production_checkpoint("/tmp/init_ckpt")
    if ckpt:
        os.environ["INIT_CHECKPOINT"] = ckpt
        summary["init_from_production"] = True
    else:
        print("  no production model yet — training fine-tune from ImageNet weights")
        summary["init_from_production"] = False

    config_path = "/tmp/train_config.yaml"
    write_feedback_config(CONFIG_TEMPLATE, feedback_dir, config_path)
    return config_path, summary


def get_production_f1():
    """Get best_f1 metric from the current production model. Returns None if no production model."""
    client = mlflow.tracking.MlflowClient()
    try:
        mv = client.get_model_version_by_alias(MODEL_NAME, "production")
        run = client.get_run(mv.run_id)
        return run.data.metrics.get("best_f1")
    except mlflow.exceptions.MlflowException:
        return None


def quality_gate(run_id, best_f1):
    """Apply quality gate: minimum threshold + comparison against production model.

    Returns (passed: bool, reason: str).
    """
    client = mlflow.tracking.MlflowClient()

    # Gate 1: minimum F1 threshold
    if best_f1 < MIN_F1_THRESHOLD:
        reason = f"F1 {best_f1:.4f} below minimum threshold {MIN_F1_THRESHOLD}"
        client.log_metric(run_id, "quality_gate_passed", 0)
        client.log_metric(run_id, "quality_gate_min_threshold", MIN_F1_THRESHOLD)
        return False, reason

    # Gate 2: compare against production model
    prod_f1 = get_production_f1()
    if prod_f1 is not None:
        client.log_metric(run_id, "production_f1", prod_f1)
        improvement = best_f1 - prod_f1
        client.log_metric(run_id, "improvement_vs_production", improvement)
        max_allowed_regression = prod_f1 * MAX_REGRESSION_PCT
        if improvement < -max_allowed_regression:
            reason = (f"F1 {best_f1:.4f} regresses vs production {prod_f1:.4f} "
                      f"by {-improvement:.4f} (max allowed: {max_allowed_regression:.4f})")
            client.log_metric(run_id, "quality_gate_passed", 0)
            return False, reason
        print(f"  Production F1: {prod_f1:.4f}, New F1: {best_f1:.4f}, "
              f"Improvement: {improvement:+.4f}")
    else:
        print("  No production model found — skipping comparison (first model)")

    client.log_metric(run_id, "quality_gate_passed", 1)
    client.log_metric(run_id, "quality_gate_min_threshold", MIN_F1_THRESHOLD)
    return True, "passed"


def register_model(run_id):
    """Register the trained model in MLflow model registry."""
    client = mlflow.tracking.MlflowClient()

    # Ensure registered model exists
    try:
        client.get_registered_model(MODEL_NAME)
    except mlflow.exceptions.MlflowException:
        client.create_registered_model(MODEL_NAME)
        print(f"Created registered model: {MODEL_NAME}")

    # Find the logged .pth artifact
    artifacts = client.list_artifacts(run_id)
    model_artifact = None
    for a in artifacts:
        if a.path.endswith(".pth"):
            model_artifact = a.path
            break

    if model_artifact is None:
        print("ERROR: No .pth artifact found in run", file=sys.stderr)
        return None

    result = client.create_model_version(
        name=MODEL_NAME,
        source=f"runs:/{run_id}/{model_artifact}",
        run_id=run_id,
    )
    print(f"Registered model version: {result.version}")
    return result.version


def _abort_no_model_version(reason):
    """Write empty /tmp/model_version so downstream Argo steps short-circuit."""
    with open("/tmp/model_version", "w") as f:
        f.write("")
    print(f"\nAborted: {reason}")
    print("Empty /tmp/model_version written.")


def main():
    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)

    with open(CONFIG_TEMPLATE) as f:
        cfg = yaml.safe_load(f)

    data_source = cfg.get("data", {}).get("source", "coco")

    print("=" * 60)
    if data_source == "feedback":
        print("Training Flow - Fine-tune on user feedback")
    else:
        print("Training Flow - COCO Multi-Label Classification")
    print("=" * 60)

    if data_source == "feedback":
        config_path, summary = prepare_feedback_run(cfg)
        print(f"  train/val/test records: "
              f"{summary.get('train_records')}/"
              f"{summary.get('val_records')}/"
              f"{summary.get('test_records')}")
        print(f"  images: {summary.get('image_count')}, "
              f"positive in-vocab: {summary.get('positive_records_in_vocab')}")
        if config_path is None:
            mlflow.set_experiment("ImmichTagger")
            with mlflow.start_run(run_name="feedback-prep-failed"):
                mlflow.log_param("data_source", "feedback")
                for i, msg in enumerate(summary.get("failures", [])):
                    mlflow.log_param(f"failure_{i}", msg[:250])
            _abort_no_model_version("feedback data preparation failed")
            return
        run_training_and_register(config_path)
        return

    max_files = cfg.get("data", {}).get("max_files")

    # Download data (incrementally syncs — skips files already on disk)
    data_dir = os.environ.get("DATA_DIR", "/mnt/workspace/coco_data")
    os.makedirs(data_dir, exist_ok=True)
    if os.environ.get("SKIP_DOWNLOAD", "").lower() in ("1", "true"):
        print("SKIP_DOWNLOAD set — using existing data in", data_dir)
    else:
        download_coco(data_dir, max_files=max_files)

    # Validate training data before proceeding (skip full validation for subset downloads)
    print("\n" + "=" * 60)
    print("Validating training data ...")
    print("=" * 60)

    if max_files:
        print("  Subset mode (max_files set) — skipping full data validation")
        passed, summary = True, {"image_count": "subset", "annotation_count": "subset",
                                 "classes_with_annotations": "N/A", "expected_classes": "N/A",
                                 "zero_byte_images": 0, "failures": []}
    else:
        passed, summary = validate_training_data(data_dir)
    print(f"  Images:      {summary.get('image_count', 'N/A')}")
    print(f"  Annotations: {summary.get('annotation_count', 'N/A')}")
    print(f"  Classes with annotations: {summary.get('classes_with_annotations', 'N/A')}"
          f" / {summary.get('expected_classes', 'N/A')}")
    print(f"  Zero-byte images: {summary.get('zero_byte_images', 'N/A')}")

    if not passed:
        print("\nDATA VALIDATION FAILED:")
        for f in summary["failures"]:
            print(f"  - {f}")

        mlflow.set_experiment("ImmichTagger")
        with mlflow.start_run(run_name="data-validation-failed"):
            mlflow.log_params({
                "data_validation_passed": False,
                "image_count": summary.get("image_count", 0),
                "annotation_count": summary.get("annotation_count", 0),
            })
            for i, f in enumerate(summary["failures"]):
                mlflow.log_param(f"validation_failure_{i}", f[:250])

        _abort_no_model_version("COCO data validation failed")
        return

    print("\nData validation PASSED.")

    # Write config with correct paths
    config_path = "/tmp/train_config.yaml"
    write_config(CONFIG_TEMPLATE, data_dir, config_path)

    run_training_and_register(config_path)


def run_training_and_register(config_path):
    """Shared tail of the pipeline: invoke train.py, apply the quality gate,
    register on success, and write /tmp/model_version for the Argo step."""
    print("\n" + "=" * 60)
    print("Starting training ...")
    print("=" * 60)

    sys.argv = ["train.py", "--config", config_path]
    from train import main as train_main
    result = train_main()

    if result is None:
        print("Training returned no result", file=sys.stderr)
        _abort_no_model_version("training returned no result")
        return

    run_id, best_f1 = result
    print(f"\nTraining complete. Run ID: {run_id}, Best F1: {best_f1:.4f}")

    print("\n" + "=" * 60)
    print("Quality Gate Check")
    print("=" * 60)

    gate_passed, gate_reason = quality_gate(run_id, best_f1)
    if not gate_passed:
        _abort_no_model_version(f"quality gate failed: {gate_reason}")
        return

    print(f"Quality gate PASSED (F1={best_f1:.4f} >= {MIN_F1_THRESHOLD})")

    version = register_model(run_id)
    if version:
        with open("/tmp/model_version", "w") as f:
            f.write(str(version))
        print(f"\nModel version {version} written to /tmp/model_version")
    else:
        print("WARNING: Model registration failed", file=sys.stderr)
        _abort_no_model_version("model registration returned no version")


if __name__ == "__main__":
    main()
