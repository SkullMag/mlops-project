import os
import json
import swiftclient
import numpy as np
from datetime import datetime, timedelta
from PIL import Image
import io

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"
PSI_THRESHOLD = 0.2

def get_swift_conn():
    return swiftclient.Connection(
        authurl=AUTH_URL, auth_version="3",
        os_options={"auth_type": "v3applicationcredential",
                    "application_credential_id": APPLICATION_CREDENTIAL_ID,
                    "application_credential_secret": APPLICATION_CREDENTIAL_SECRET})

def compute_brightness(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((224, 224))
    return float(np.array(img, dtype=np.float32).mean() / 255.0)

def compute_psi(expected, actual, bins=10):
    expected, actual = np.array(expected), np.array(actual)
    edges = np.linspace(min(expected.min(), actual.min()), max(expected.max(), actual.max()), bins+1)
    exp_pct = (np.histogram(expected, bins=edges)[0] + 1e-6) / (len(expected) + 1e-6*bins)
    act_pct = (np.histogram(actual, bins=edges)[0] + 1e-6) / (len(actual) + 1e-6*bins)
    return float(np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct)))

def load_reference_stats(conn):
    try:
        _, content = conn.get_object(CONTAINER_NAME, "drift/reference_stats.json")
        return json.loads(content)
    except Exception:
        return None

def compute_reference_stats(conn):
    print("Computing reference stats from training images...")
    _, objects = conn.get_container(CONTAINER_NAME, prefix="coco/images/", limit=500)
    brightness_values = []
    for obj in objects[:200]:
        try:
            _, content = conn.get_object(CONTAINER_NAME, obj["name"])
            brightness_values.append(compute_brightness(content))
        except Exception as e:
            print(f"Skipping {obj['name']}: {e}")
    if not brightness_values:
        return None
    stats = {"brightness": brightness_values, "mean": float(np.mean(brightness_values)),
             "std": float(np.std(brightness_values)), "created_at": datetime.utcnow().isoformat()}
    conn.put_object(CONTAINER_NAME, "drift/reference_stats.json", json.dumps(stats).encode())
    print(f"Reference stats from {len(brightness_values)} images")
    return stats

def load_recent_production_images(conn, hours=24):
    print(f"Loading production images from last {hours} hours...")
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    _, objects = conn.get_container(CONTAINER_NAME, prefix="uploads/", limit=1000)
    brightness_values = []
    for obj in objects:
        try:
            last_modified = datetime.strptime(obj["last_modified"], "%Y-%m-%dT%H:%M:%S.%f")
            if last_modified < cutoff:
                continue
            _, content = conn.get_object(CONTAINER_NAME, obj["name"])
            brightness_values.append(compute_brightness(content))
        except Exception as e:
            print(f"Skipping {obj['name']}: {e}")
    print(f"Loaded {len(brightness_values)} production images")
    return brightness_values

def main():
    conn = get_swift_conn()
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    print(f"Starting drift monitor - {version}")
    ref_stats = load_reference_stats(conn) or compute_reference_stats(conn)
    if not ref_stats:
        print("Cannot compute reference stats - exiting")
        return
    production_brightness = load_recent_production_images(conn, hours=24)
    if len(production_brightness) < 10:
        print(f"Not enough production images ({len(production_brightness)}) - need at least 10")
        return
    psi = compute_psi(ref_stats["brightness"], production_brightness)
    is_drifted = psi > PSI_THRESHOLD
    production_mean = float(np.mean(production_brightness))
    print(f"PSI: {psi:.4f} (threshold: {PSI_THRESHOLD})")
    print(f"Reference mean brightness: {ref_stats['mean']:.4f}")
    print(f"Production mean brightness: {production_mean:.4f}")
    print(f"Drift detected: {is_drifted}")
    report = {"version": version, "timestamp": datetime.utcnow().isoformat(),
              "psi": psi, "reference_mean_brightness": ref_stats["mean"],
              "production_mean_brightness": production_mean,
              "drift_detected": is_drifted, "psi_threshold": PSI_THRESHOLD,
              "status": "DRIFT_DETECTED" if is_drifted else "OK"}
    key = f"drift/reports/report_{version}.json"
    conn.put_object(CONTAINER_NAME, key, json.dumps(report).encode())
    print(f"Drift report saved to {key}")
    if is_drifted:
        print("WARNING: Significant drift detected! Consider retraining.")
    else:
        print("OK: No significant drift detected.")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
