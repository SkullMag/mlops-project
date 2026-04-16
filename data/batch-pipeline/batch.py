import os
import json
import uuid
import swiftclient
from datetime import datetime, timedelta

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"

TEST_USERS = {"user_001", "user_002", "user_003", "user_004", "user_005"}

def get_swift_conn():
    return swiftclient.Connection(
        authurl=AUTH_URL,
        auth_version="3",
        os_options={
            "auth_type": "v3applicationcredential",
            "application_credential_id": APPLICATION_CREDENTIAL_ID,
            "application_credential_secret": APPLICATION_CREDENTIAL_SECRET,
        }
    )

def load_feedback_events(conn):
    print("Loading feedback events from bucket...")
    _, objects = conn.get_container(CONTAINER_NAME, prefix="feedback/events/", limit=10000)
    events = []
    for obj in objects:
        _, content = conn.get_object(CONTAINER_NAME, obj["name"])
        event = json.loads(content)
        events.append(event)
    print(f"Loaded {len(events)} feedback events")
    return events

def load_upload_events(conn):
    print("Loading upload events from bucket...")
    _, objects = conn.get_container(CONTAINER_NAME, prefix="feedback/uploads/", limit=10000)
    uploads = {}
    for obj in objects:
        _, content = conn.get_object(CONTAINER_NAME, obj["name"])
        upload = json.loads(content)
        uploads[upload["request_id"]] = upload
    print(f"Loaded {len(uploads)} upload events")
    return uploads

def apply_candidate_selection(events, uploads):
    print("Applying candidate selection filters...")
    cutoff_date = datetime.utcnow() - timedelta(days=30)
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

        # Filter 4: only include if upload exists
        if event["request_id"] not in uploads:
            continue

        # Filter 5: tag confidence > 0.3
        upload = uploads[event["request_id"]]
        confidence = upload.get("confidence_scores", {}).get(event["tag"], 1.0)
        if confidence < 0.3:
            continue

        filtered.append(event)

    print(f"After filtering: {len(filtered)} candidates")
    return filtered

def split_data(events, uploads):
    print("Splitting data into train/val/test...")
    
    # User based split - 80% train, 20% test
    all_users = list(set(e["user_id"] for e in events) - TEST_USERS)
    all_users.sort()
    split_idx = int(len(all_users) * 0.8)
    train_users = set(all_users[:split_idx])

    # Time based split
    events_sorted = sorted(events, key=lambda x: x["timestamp"])
    n = len(events_sorted)
    train_end = int(n * 0.70)
    val_end = int(n * 0.85)

    train = events_sorted[:train_end]
    val = events_sorted[train_end:val_end]
    test = events_sorted[val_end:]

    print(f"Train: {len(train)}, Val: {len(val)}, Test: {len(test)}")
    return train, val, test

def build_dataset(events, uploads):
    dataset = []
    for event in events:
        upload = uploads.get(event["request_id"], {})
        record = {
            "image_id": event["image_id"],
            "image_uri": upload.get("image_uri", ""),
            "tag": event["tag"],
            "label": 1 if event["action"] == "added" else 0,
            "timestamp": event["timestamp"],
            "user_id": event["user_id"]
        }
        dataset.append(record)
    return dataset

def upload_dataset(conn, dataset, split_name, version):
    key = f"datasets/v{version}/{split_name}.json"
    content = json.dumps(dataset).encode()
    conn.put_object(CONTAINER_NAME, key, content)
    print(f"Uploaded {split_name} dataset ({len(dataset)} records) to {key}")

def main():
    conn = get_swift_conn()
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    print(f"Starting batch pipeline - version {version}")

    events = load_feedback_events(conn)
    uploads = load_upload_events(conn)

    if not events:
        print("No feedback events found!")
        return

    candidates = apply_candidate_selection(events, uploads)

    if not candidates:
        print("No candidates after filtering!")
        return

    train_events, val_events, test_events = split_data(candidates, uploads)

    train_data = build_dataset(train_events, uploads)
    val_data = build_dataset(val_events, uploads)
    test_data = build_dataset(test_events, uploads)

    upload_dataset(conn, train_data, "train", version)
    upload_dataset(conn, val_data, "val", version)
    upload_dataset(conn, test_data, "test", version)

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

    conn.put_object(
        CONTAINER_NAME,
        f"datasets/v{version}/manifest.json",
        json.dumps(manifest).encode()
    )
    print(f"Pipeline complete! Dataset version: {version}")
    print(json.dumps(manifest, indent=2))

if __name__ == "__main__":
    main()
