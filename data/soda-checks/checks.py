import os
import json
import swiftclient
from datetime import datetime, timedelta

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"

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
    _, objects = conn.get_container(CONTAINER_NAME, prefix="feedback/events/", limit=10000)
    events = []
    for obj in objects:
        _, content = conn.get_object(CONTAINER_NAME, obj["name"])
        events.append(json.loads(content))
    return events

def load_upload_events(conn):
    _, objects = conn.get_container(CONTAINER_NAME, prefix="feedback/uploads/", limit=10000)
    uploads = []
    for obj in objects:
        _, content = conn.get_object(CONTAINER_NAME, obj["name"])
        uploads.append(json.loads(content))
    return uploads

def run_checks(events, uploads):
    print("=" * 60)
    print("SODA-STYLE DATA QUALITY CHECKS — Photnizer Feedback Data")
    print(f"Run time: {datetime.utcnow().isoformat()}")
    print("=" * 60)

    results = []
    now = datetime.utcnow()
    cutoff = now - timedelta(days=30)

    # CHECK 1: No missing required fields in feedback events
    missing_fields = [
        e for e in events
        if not all(k in e for k in ["feedback_id", "request_id", "image_id", "user_id", "tag", "action", "timestamp"])
    ]
    check1 = {
        "check": "No missing required fields in feedback events",
        "status": "PASS" if len(missing_fields) == 0 else "FAIL",
        "total": len(events),
        "failed": len(missing_fields)
    }
    results.append(check1)
    print(f"\n[{check1['status']}] {check1['check']}")
    print(f"  Total events: {check1['total']}, Failed: {check1['failed']}")

    # CHECK 2: No future timestamps
    future_events = [
        e for e in events
        if datetime.fromisoformat(e["timestamp"]) > now
    ]
    check2 = {
        "check": "No future timestamps in feedback events",
        "status": "PASS" if len(future_events) == 0 else "FAIL",
        "total": len(events),
        "failed": len(future_events)
    }
    results.append(check2)
    print(f"\n[{check2['status']}] {check2['check']}")
    print(f"  Total events: {check2['total']}, Failed: {check2['failed']}")

    # CHECK 3: Action field only contains valid values
    invalid_actions = [
        e for e in events
        if e.get("action") not in ["added", "deleted"]
    ]
    check3 = {
        "check": "Action field only contains 'added' or 'deleted'",
        "status": "PASS" if len(invalid_actions) == 0 else "FAIL",
        "total": len(events),
        "failed": len(invalid_actions)
    }
    results.append(check3)
    print(f"\n[{check3['status']}] {check3['check']}")
    print(f"  Total events: {check3['total']}, Failed: {check3['failed']}")

    # CHECK 4: No duplicate corrections per image-tag pair
    seen = set()
    duplicates = []
    for e in events:
        key = f"{e['image_id']}_{e['tag']}_{e['user_id']}"
        if key in seen:
            duplicates.append(e)
        seen.add(key)
    check4 = {
        "check": "No duplicate corrections per (image, tag, user) triplet",
        "status": "PASS" if len(duplicates) == 0 else "WARN",
        "total": len(events),
        "failed": len(duplicates)
    }
    results.append(check4)
    print(f"\n[{check4['status']}] {check4['check']}")
    print(f"  Total events: {check4['total']}, Duplicates: {check4['failed']}")

    # CHECK 5: Feedback ratio not too skewed
    added = sum(1 for e in events if e.get("action") == "added")
    deleted = sum(1 for e in events if e.get("action") == "deleted")
    total = added + deleted
    ratio = deleted / total if total > 0 else 0
    check5 = {
        "check": "Feedback ratio not too skewed (deletions < 80%)",
        "status": "PASS" if ratio < 0.8 else "WARN",
        "total": total,
        "added": added,
        "deleted": deleted,
        "deletion_ratio": round(ratio, 2)
    }
    results.append(check5)
    print(f"\n[{check5['status']}] {check5['check']}")
    print(f"  Added: {added}, Deleted: {deleted}, Deletion ratio: {ratio:.2%}")

    # CHECK 6: No missing required fields in upload events
    missing_uploads = [
        u for u in uploads
        if not all(k in u for k in ["request_id", "image_id", "user_id", "timestamp", "predicted_tags"])
    ]
    check6 = {
        "check": "No missing required fields in upload events",
        "status": "PASS" if len(missing_uploads) == 0 else "FAIL",
        "total": len(uploads),
        "failed": len(missing_uploads)
    }
    results.append(check6)
    print(f"\n[{check6['status']}] {check6['check']}")
    print(f"  Total uploads: {check6['total']}, Failed: {check6['failed']}")

    # CHECK 7: Minimum feedback volume
    recent_events = [
        e for e in events
        if datetime.fromisoformat(e["timestamp"]) > cutoff
    ]
    check7 = {
        "check": "Sufficient feedback volume (>= 50 events in last 30 days)",
        "status": "PASS" if len(recent_events) >= 50 else "WARN",
        "recent_events": len(recent_events)
    }
    results.append(check7)
    print(f"\n[{check7['status']}] {check7['check']}")
    print(f"  Recent events (last 30 days): {check7['recent_events']}")

    # SUMMARY
    print("\n" + "=" * 60)
    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    print(f"SUMMARY: {passed} PASS | {warned} WARN | {failed} FAIL")
    print("=" * 60)

    # Save results to bucket
    return results

def main():
    print("Connecting to Chameleon object storage...")
    conn = get_swift_conn()

    print("Loading feedback events...")
    events = load_feedback_events(conn)
    print(f"Loaded {len(events)} feedback events")

    print("Loading upload events...")
    uploads = load_upload_events(conn)
    print(f"Loaded {len(uploads)} upload events")

    results = run_checks(events, uploads)

    # Save results back to bucket
    report = {
        "run_time": datetime.utcnow().isoformat(),
        "total_feedback_events": len(events),
        "total_upload_events": len(uploads),
        "checks": results
    }
    conn.put_object(
        CONTAINER_NAME,
        f"quality-reports/report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json",
        json.dumps(report, indent=2).encode()
    )
    print("\nQuality report saved to bucket!")

if __name__ == "__main__":
    main()
