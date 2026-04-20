import os
import json
import boto3
from datetime import datetime, timedelta

MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY
    )

def load_feedback_events(s3):
    events = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="feedback/events/"):
        for obj in page.get("Contents", []):
            response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            events.append(json.loads(response["Body"].read()))
    return events

def load_upload_events(s3):
    uploads = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="feedback/uploads/"):
        for obj in page.get("Contents", []):
            response = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            uploads.append(json.loads(response["Body"].read()))
    return uploads

def run_checks(events, uploads):
    print("=" * 60)
    print("SODA-STYLE DATA QUALITY CHECKS — Photnizer Feedback Data")
    print(f"Run time: {datetime.utcnow().isoformat()}")
    print("=" * 60)

    results = []
    now = datetime.utcnow()
    cutoff = now - timedelta(days=30)

    missing_fields = [e for e in events if not all(k in e for k in ["feedback_id", "request_id", "image_id", "user_id", "tag", "action", "timestamp"])]
    check1 = {"check": "No missing required fields in feedback events", "status": "PASS" if len(missing_fields) == 0 else "FAIL", "total": len(events), "failed": len(missing_fields)}
    results.append(check1)
    print(f"\n[{check1['status']}] {check1['check']}")
    print(f"  Total events: {check1['total']}, Failed: {check1['failed']}")

    future_events = [e for e in events if datetime.fromisoformat(e["timestamp"]) > now]
    check2 = {"check": "No future timestamps in feedback events", "status": "PASS" if len(future_events) == 0 else "FAIL", "total": len(events), "failed": len(future_events)}
    results.append(check2)
    print(f"\n[{check2['status']}] {check2['check']}")
    print(f"  Total events: {check2['total']}, Failed: {check2['failed']}")

    invalid_actions = [e for e in events if e.get("action") not in ["added", "deleted"]]
    check3 = {"check": "Action field only contains 'added' or 'deleted'", "status": "PASS" if len(invalid_actions) == 0 else "FAIL", "total": len(events), "failed": len(invalid_actions)}
    results.append(check3)
    print(f"\n[{check3['status']}] {check3['check']}")
    print(f"  Total events: {check3['total']}, Failed: {check3['failed']}")

    seen = set()
    duplicates = []
    for e in events:
        key = f"{e['image_id']}_{e['tag']}_{e['user_id']}"
        if key in seen:
            duplicates.append(e)
        seen.add(key)
    check4 = {"check": "No duplicate corrections per (image, tag, user) triplet", "status": "PASS" if len(duplicates) == 0 else "WARN", "total": len(events), "failed": len(duplicates)}
    results.append(check4)
    print(f"\n[{check4['status']}] {check4['check']}")
    print(f"  Total events: {check4['total']}, Duplicates: {check4['failed']}")

    added = sum(1 for e in events if e.get("action") == "added")
    deleted = sum(1 for e in events if e.get("action") == "deleted")
    total = added + deleted
    ratio = deleted / total if total > 0 else 0
    check5 = {"check": "Feedback ratio not too skewed (deletions < 80%)", "status": "PASS" if ratio < 0.8 else "WARN", "total": total, "added": added, "deleted": deleted, "deletion_ratio": round(ratio, 2)}
    results.append(check5)
    print(f"\n[{check5['status']}] {check5['check']}")
    print(f"  Added: {added}, Deleted: {deleted}, Deletion ratio: {ratio:.2%}")

    missing_uploads = [u for u in uploads if not all(k in u for k in ["request_id", "image_id", "user_id", "timestamp", "predicted_tags"])]
    check6 = {"check": "No missing required fields in upload events", "status": "PASS" if len(missing_uploads) == 0 else "FAIL", "total": len(uploads), "failed": len(missing_uploads)}
    results.append(check6)
    print(f"\n[{check6['status']}] {check6['check']}")
    print(f"  Total uploads: {check6['total']}, Failed: {check6['failed']}")

    recent_events = [e for e in events if datetime.fromisoformat(e["timestamp"]) > cutoff]
    check7 = {"check": "Sufficient feedback volume (>= 50 events in last 30 days)", "status": "PASS" if len(recent_events) >= 50 else "WARN", "recent_events": len(recent_events)}
    results.append(check7)
    print(f"\n[{check7['status']}] {check7['check']}")
    print(f"  Recent events (last 30 days): {check7['recent_events']}")

    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"SUMMARY: {passed} PASS | {warned} WARN | {failed} FAIL")
    print("=" * 60)
    return results

def main():
    print("Connecting to MinIO...")
    s3 = get_minio_client()

    print("Loading feedback events...")
    events = load_feedback_events(s3)
    print(f"Loaded {len(events)} feedback events")

    print("Loading upload events...")
    uploads = load_upload_events(s3)
    print(f"Loaded {len(uploads)} upload events")

    results = run_checks(events, uploads)

    report = {
        "run_time": datetime.utcnow().isoformat(),
        "total_feedback_events": len(events),
        "total_upload_events": len(uploads),
        "checks": results
    }
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=f"quality-reports/report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json",
        Body=json.dumps(report, indent=2).encode()
    )
    print("\nQuality report saved to bucket!")

if __name__ == "__main__":
    main()
