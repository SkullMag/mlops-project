"""Feedback dataset loader: pulls retraining data from MinIO and adapts it
to the same multi-label tensor format the COCO loader produces.

Layout in MinIO (written by data/batch-pipeline/batch.py):
  datasets/v{version}/READY                  (presence marker)
  datasets/v{version}/{train,val,test}.json  (list of feedback records)
  datasets/v{version}/images/{image_id}.jpg

Each record is (image_id, tag, label, ...). Records are aggregated per image_id
into a multi-label vector over COCO_CATEGORY_NAMES. Records whose tag is not in
the COCO vocab are dropped.
"""

import json
import os
from collections import defaultdict

import boto3
import torch
from botocore.exceptions import ClientError
from PIL import Image
from torch.utils.data import DataLoader, Dataset

from dataset import COCO_CATEGORY_NAMES, get_transforms


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get(
            "MINIO_ENDPOINT", "http://minio.immich-platform.svc.cluster.local:9000"),
        aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY", ""),
        aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY", ""),
    )


def find_latest_ready_version(s3, bucket):
    """Return the newest dataset version that has a READY marker, or None."""
    paginator = s3.get_paginator("list_objects_v2")
    versions = []
    for page in paginator.paginate(Bucket=bucket, Prefix="datasets/", Delimiter="/"):
        for prefix in page.get("CommonPrefixes", []):
            parts = prefix["Prefix"].rstrip("/").split("/")
            if len(parts) != 2 or not parts[1].startswith("v"):
                continue
            version = parts[1][1:]
            try:
                s3.head_object(Bucket=bucket, Key=f"datasets/v{version}/READY")
            except ClientError:
                continue
            versions.append(version)
    if not versions:
        return None
    return sorted(versions)[-1]


def download_feedback_dataset(s3, bucket, version, dest_dir):
    """Pull split JSONs and images for a given version into dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    images_dir = os.path.join(dest_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    for split in ("train", "val", "test"):
        key = f"datasets/v{version}/{split}.json"
        local = os.path.join(dest_dir, f"{split}.json")
        s3.download_file(bucket, key, local)
        print(f"  downloaded {key}", flush=True)

    paginator = s3.get_paginator("list_objects_v2")
    count = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=f"datasets/v{version}/images/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = os.path.basename(key)
            if not filename:
                continue
            local = os.path.join(images_dir, filename)
            if os.path.exists(local) and os.path.getsize(local) > 0:
                count += 1
                continue
            s3.download_file(bucket, key, local)
            count += 1
    print(f"  downloaded {count} images to {images_dir}", flush=True)
    return images_dir


def aggregate_records(records, name_to_idx):
    """Group feedback records by image_id, return {image_id: {class_idx: label}}.

    Records whose tag is not in the COCO vocab are skipped silently. When the same
    (image, tag) pair appears multiple times, the last occurrence wins — callers
    should pass records sorted by timestamp if they want deterministic behavior.
    """
    per_image = defaultdict(dict)
    for r in records:
        tag = (r.get("tag") or "").strip().lower()
        if tag not in name_to_idx:
            continue
        per_image[r["image_id"]][name_to_idx[tag]] = int(r.get("label", 0))
    return per_image


class FeedbackMultiLabelDataset(Dataset):
    def __init__(self, records, image_dir, transform=None):
        self.image_dir = image_dir
        self.transform = transform
        self.num_classes = len(COCO_CATEGORY_NAMES)
        name_to_idx = {n: i for i, n in enumerate(COCO_CATEGORY_NAMES)}

        per_image = aggregate_records(records, name_to_idx)

        local_files = set(os.listdir(image_dir)) if os.path.isdir(image_dir) else set()
        self.items = []
        for image_id, label_map in per_image.items():
            filename = f"{image_id}.jpg"
            if filename not in local_files:
                continue
            self.items.append((filename, label_map))

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        filename, label_map = self.items[idx]
        image = Image.open(os.path.join(self.image_dir, filename)).convert("RGB")
        if self.transform:
            image = self.transform(image)
        labels = torch.zeros(self.num_classes, dtype=torch.float32)
        for class_idx, lbl in label_map.items():
            labels[class_idx] = float(lbl)
        return image, labels


def _load_split(json_path):
    with open(json_path) as f:
        return json.load(f)


def create_feedback_dataloaders(cfg):
    """Build train/val DataLoaders from cfg["data"]["feedback_dir"]."""
    data = cfg["data"]
    bs = cfg["training"]["batch_size"]
    feedback_dir = data["feedback_dir"]
    image_dir = os.path.join(feedback_dir, "images")

    train_records = _load_split(os.path.join(feedback_dir, "train.json"))
    val_records = _load_split(os.path.join(feedback_dir, "val.json"))

    train_ds = FeedbackMultiLabelDataset(
        train_records, image_dir,
        transform=get_transforms(data["image_size"], train=True),
    )
    val_ds = FeedbackMultiLabelDataset(
        val_records, image_dir,
        transform=get_transforms(data["image_size"], train=False),
    )

    if len(train_ds) == 0:
        raise RuntimeError(
            f"Feedback train set is empty after filtering "
            f"(raw records={len(train_records)}, image_dir={image_dir}). "
            f"Check that feedback tags overlap with COCO_CATEGORY_NAMES "
            f"and that images downloaded successfully.")
    if len(val_ds) == 0:
        raise RuntimeError(
            f"Feedback val set is empty after filtering (raw records={len(val_records)})")

    print(f"  feedback train: {len(train_ds)} images, val: {len(val_ds)} images", flush=True)

    train_loader = DataLoader(
        train_ds, batch_size=bs, shuffle=True,
        num_workers=data.get("num_workers", 4), pin_memory=True,
    )
    val_loader = DataLoader(
        val_ds, batch_size=bs, shuffle=False,
        num_workers=data.get("num_workers", 4), pin_memory=True,
    )
    return train_loader, val_loader


def validate_feedback_data(feedback_dir, min_images=50, min_positive_records=20):
    """Lightweight pre-flight check on a downloaded feedback dataset."""
    summary = {"failures": []}

    images_dir = os.path.join(feedback_dir, "images")
    if not os.path.isdir(images_dir):
        summary["failures"].append(f"Missing images dir: {images_dir}")
        return False, summary

    image_files = [f for f in os.listdir(images_dir)
                   if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    summary["image_count"] = len(image_files)
    if len(image_files) < min_images:
        summary["failures"].append(
            f"Too few feedback images: {len(image_files)} (minimum {min_images})")

    zero_byte = [f for f in image_files
                 if os.path.getsize(os.path.join(images_dir, f)) == 0]
    summary["zero_byte_images"] = len(zero_byte)
    if zero_byte:
        summary["failures"].append(f"{len(zero_byte)} zero-byte image files")

    name_to_idx = {n: i for i, n in enumerate(COCO_CATEGORY_NAMES)}
    counts = {"train": 0, "val": 0, "test": 0}
    positive_in_vocab = 0
    for split in counts:
        path = os.path.join(feedback_dir, f"{split}.json")
        if not os.path.isfile(path):
            summary["failures"].append(f"Missing split file: {path}")
            continue
        records = _load_split(path)
        counts[split] = len(records)
        for r in records:
            tag = (r.get("tag") or "").strip().lower()
            if tag in name_to_idx and int(r.get("label", 0)) == 1:
                positive_in_vocab += 1

    summary.update({f"{k}_records": v for k, v in counts.items()})
    summary["positive_records_in_vocab"] = positive_in_vocab
    if positive_in_vocab < min_positive_records:
        summary["failures"].append(
            f"Too few positive records with tags in COCO vocab: "
            f"{positive_in_vocab} (minimum {min_positive_records})")

    return len(summary["failures"]) == 0, summary
