import os
import json
import boto3
import requests
from datetime import datetime, timedelta, timezone
from botocore.exceptions import ClientError

# MinIO connection settings - all from environment variables
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

TEST_USERS = {"user_001", "user_002", "user_003", "user_004", "user_005"}

IMMICH_API_KEY = "3gPv2G1Tt2wRzm7uMNKeISlURlxCqN3NF8gqsMUzy6E"
IMMICH_BASE_URL = "http://129.114.24.200:2283"

def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY
    )

def check_data_quality_passed(s3):
    """Read latest soda quality report and check if data is safe to use"""
    print("Checking data quality reports...")

    # Find all quality reports
    paginator = s3.get_paginator("list_objects_v2")
    reports = []
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="quality-reports/"):
        for obj in page.get("Contents", []):
            reports.append(obj["Key"])

    if not reports:
        print("No quality reports found — proceeding with caution!")
        return True

    # Get the latest report (sorted by name = sorted by timestamp)
    latest_report_key = sorted(reports)[-1]
    print(f"Reading quality report: {latest_report_key}")

    response = s3.get_object(Bucket=BUCKET_NAME, Key=latest_report_key)
    report = json.loads(response["Body"].read())

    # Check if any FAIL status
    failed_checks = [
        check for check in report["checks"]
        if check["status"] == "FAIL"
    ]

    if failed_checks:
        print("DATA QUALITY FAILED! The following checks failed:")
        for check in failed_checks:
            print(f"  FAIL: {check['check']}")
        return False

    # Check if volume is sufficient
    total_events = report.get("total_feedback_events", 0)
    if total_events == 0:
        print("No feedback events found in quality report!")
        return False

    print(f"Data quality PASSED! ({total_events} feedback events)")
    return True

def drop_ready_marker(s3, version, manifest):
    marker = {
        "version": version,
        "dataset_path": f"datasets/v{version}/",
        "train_size": manifest["train_size"],
        "val_size": manifest["val_size"],
        "test_size": manifest["test_size"],
        "status": "READY",
        "created_at": manifest["created_at"]
    }
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=f"datasets/v{version}/READY",
        Body=json.dumps(marker).encode()
    )
    print(f"READY marker dropped for version {version}")

def load_feedback_events(s3):
    print("Loading feedback events from bucket...")
    events = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="feedback/events/"):
        for obj in page.get("Contents", []):
            response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            event = json.loads(response["Body"].read())
            events.append(event)
    print(f"Loaded {len(events)} feedback events")
    return events

def load_upload_events(s3):
    print("Loading upload events from bucket...")
    uploads = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="feedback/uploads/"):
        for obj in page.get("Contents", []):
            response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            upload = json.loads(response["Body"].read())
            uploads[upload["request_id"]] = upload
    print(f"Loaded {len(uploads)} upload events")
    return uploads

def fetch_and_store_image(s3, image_id, version):
    """Fetch image from Immich and store in MinIO"""
    url = f"{IMMICH_BASE_URL}/api/assets/{image_id}/original"
    headers = {"x-api-key": IMMICH_API_KEY}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 200:
            key = f"datasets/v{version}/images/{image_id}.jpg"
            s3.put_object(
                Bucket=BUCKET_NAME,
                Key=key,
                Body=response.content
            )
            print(f"Fetched and stored image {image_id}")
            return key
        else:
            print(f"Failed to fetch image {image_id}: {response.status_code}")
            return None
    except Exception as e:
        print(f"Error fetching image {image_id}: {e}")
        return None

def apply_candidate_selection(events, uploads):
    print("Applying candidate selection filters...")

    # Use timezone-aware datetime to match feedback timestamps
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=30)

    seen = set()
    filtered = []

    for event in events:
        # Filter 1: time range - last 30 days
        event_time = datetime.fromisoformat(event["timestamp"])
        if event_time < cutoff_date:
            continue

        # Filter 2: exclude test users
        if event["user_id"] in TEST_USERS:
            continue

        # Filter 3: deduplication per image-tag pair
        dedup_key = f"{event['image_id']}_{event['tag']}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Filter 4: confidence check directly from feedback event
        # confidence=0.0 means user added a tag model missed (always keep)
        # confidence=1.0 is default when no confidence info available
        # We filter out low confidence deletions (model was already unsure)
        confidence = event.get("confidence", 1.0)
        if event.get("action") == "deleted" and confidence < 0.3:
            continue

        filtered.append(event)

    print(f"After filtering: {len(filtered)} candidates")
    return filtered

def split_data(events, uploads):
    print("Splitting data into train/val/test...")

    # Use image_id based split since all real users share "immich-user"
    all_images = list(set(e["image_id"] for e in events))
    all_images.sort()

    # Time based split - sort by timestamp
    events_sorted = sorted(events, key=lambda x: x["timestamp"])
    n = len(events_sorted)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)

    train = events_sorted[:train_end]
    val = events_sorted[train_end:val_end]
    test = events_sorted[val_end:]

    print(f"Train: {len(train)}, Val: {len(val)}, Test: {len(test)}")
    return train, val, test

def build_dataset(s3, events, uploads, version):
    dataset = []
    for event in events:
        upload = uploads.get(event["request_id"], {})

        # Fetch actual image from Immich and store in MinIO
        image_key = fetch_and_store_image(s3, event["image_id"], version)

        record = {
            "image_id": event["image_id"],
            "image_uri": image_key if image_key else upload.get("image_uri", ""),
            "tag": event["tag"],
            "label": 1 if event["action"] == "added" else 0,
            "confidence": event.get("confidence", 1.0),
            "timestamp": event["timestamp"],
            "user_id": event["user_id"]
        }
        dataset.append(record)
    return dataset

def upload_dataset(s3, dataset, split_name, version):
    key = f"datasets/v{version}/{split_name}.json"
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(dataset).encode()
    )
    print(f"Uploaded {split_name} dataset ({len(dataset)} records) to {key}")

def main():
    s3 = get_minio_client()
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    print(f"Starting batch pipeline - version {version}")

    # Step 1: Check data quality before proceeding
    if not check_data_quality_passed(s3):
        print("Aborting batch pipeline — data quality checks failed!")
        return

    # Step 2: Load feedback and upload events
    events = load_feedback_events(s3)
    uploads = load_upload_events(s3)

    if not events:
        print("No feedback events found!")
        return

    # Step 3: Apply candidate selection filters
    candidates = apply_candidate_selection(events, uploads)

    if not candidates:
        print("No candidates after filtering!")
        return

    # Step 4: Split into train/val/test
    train_events, val_events, test_events = split_data(candidates, uploads)

    # Step 5: Build datasets with real images from Immich
    train_data = build_dataset(s3, train_events, uploads, version)
    val_data = build_dataset(s3, val_events, uploads, version)
    test_data = build_dataset(s3, test_events, uploads, version)

    # Step 6: Upload datasets to MinIO
    upload_dataset(s3, train_data, "train", version)
    upload_dataset(s3, val_data, "val", version)
    upload_dataset(s3, test_data, "test", version)

    # Step 7: Save manifest and drop READY marker
    manifest = {
        "version": version,
        "created_at": datetime.utcnow().isoformat(),
        "train_size": len(train_data),
        "val_size": len(val_data),
        "test_size": len(test_data),
        "candidate_selection": {
            "time_range_days": 30,
            "excluded_test_users": list(TEST_USERS),
            "deduplication": "per image-tag pair",
            "min_confidence": 0.3
        },
        "split_strategy": "time_based_70_15_15"
    }

    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=f"datasets/v{version}/manifest.json",
        Body=json.dumps(manifest).encode()
    )
    drop_ready_marker(s3, version, manifest)
    print(f"Pipeline complete! Dataset version: {version}")
    print(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()
