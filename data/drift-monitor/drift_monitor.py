import os
import json
import boto3
import numpy as np
from datetime import datetime, timedelta
from PIL import Image
import io

MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")
PSI_THRESHOLD = 0.2

def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY
    )

def compute_brightness(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize((224, 224))
    return float(np.array(img, dtype=np.float32).mean() / 255.0)

def compute_psi(expected, actual, bins=10):
    expected, actual = np.array(expected), np.array(actual)
    edges = np.linspace(min(expected.min(), actual.min()), max(expected.max(), actual.max()), bins+1)
    exp_pct = (np.histogram(expected, bins=edges)[0] + 1e-6) / (len(expected) + 1e-6*bins)
    act_pct = (np.histogram(actual, bins=edges)[0] + 1e-6) / (len(actual) + 1e-6*bins)
    return float(np.sum((act_pct - exp_pct) * np.log(act_pct / exp_pct)))

def load_reference_stats(s3):
    try:
        response = s3.get_object(Bucket=BUCKET_NAME, Key="drift/reference_stats.json")
        return json.loads(response["Body"].read())
    except Exception:
        return None

def compute_reference_stats(s3):
    print("Computing reference stats from training images...")
    response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="coco/val2017/", MaxKeys=500)
    objects = response.get("Contents", [])
    brightness_values = []
    for obj in objects[:200]:
        try:
            r = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            brightness_values.append(compute_brightness(r["Body"].read()))
        except Exception as e:
            print(f"Skipping {obj['Key']}: {e}")
    if not brightness_values:
        return None
    stats = {"brightness": brightness_values, "mean": float(np.mean(brightness_values)),
             "std": float(np.std(brightness_values)), "created_at": datetime.utcnow().isoformat()}
    s3.put_object(Bucket=BUCKET_NAME, Key="drift/reference_stats.json", Body=json.dumps(stats).encode())
    print(f"Reference stats from {len(brightness_values)} images")
    return stats

def load_recent_production_images(s3, hours=24):
    print(f"Loading production images from last {hours} hours...")
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="uploads/", MaxKeys=1000)
    objects = response.get("Contents", [])
    brightness_values = []
    for obj in objects:
        try:
            last_modified = obj["LastModified"].replace(tzinfo=None)
            if last_modified < cutoff:
                continue
            r = s3.get_object(Bucket=BUCKET_NAME, Key=obj["Key"])
            brightness_values.append(compute_brightness(r["Body"].read()))
        except Exception as e:
            print(f"Skipping {obj['Key']}: {e}")
    print(f"Loaded {len(brightness_values)} production images")
    return brightness_values

def main():
    s3 = get_minio_client()
    version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    print(f"Starting drift monitor - {version}")
    ref_stats = load_reference_stats(s3) or compute_reference_stats(s3)
    if not ref_stats:
        print("Cannot compute reference stats - exiting")
        return
    production_brightness = load_recent_production_images(s3, hours=24)
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
    s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=json.dumps(report).encode())
    print(f"Drift report saved to {key}")
    if is_drifted:
        print("WARNING: Significant drift detected! Consider retraining.")
    else:
        print("OK: No significant drift detected.")
    print(json.dumps(report, indent=2))

if __name__ == "__main__":
    main()
