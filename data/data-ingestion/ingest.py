import os
import shutil
import zipfile
import urllib.request

import boto3

# MinIO connection settings - all from environment variables
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

# Same URLs as training/download_coco.sh
ANNOTATIONS_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
VAL_IMAGES_URL = "http://images.cocodataset.org/zips/val2017.zip"
TRAIN_IMAGES_URL = "http://images.cocodataset.org/zips/train2017.zip"


def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
    )


def download_and_extract(url, dest):
    zip_path = os.path.join(dest, os.path.basename(url))
    print(f"Downloading {url} ...")
    urllib.request.urlretrieve(url, zip_path)
    print(f"Extracting ...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest)
    os.remove(zip_path)
    print("Done.")


def upload_dir(s3, local_dir, minio_prefix):
    """Upload all files under local_dir to MinIO at minio_prefix/."""
    count = 0
    for root, _dirs, files in os.walk(local_dir):
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, local_dir)
            key = f"{minio_prefix}/{rel}"
            with open(fpath, "rb") as f:
                s3.put_object(Bucket=BUCKET_NAME, Key=key, Body=f)
            count += 1
            if count % 500 == 0:
                print(f"  uploaded {count} files ...")
    return count


def main():
    s3 = get_minio_client()
    work_dir = "/data"
    os.makedirs(work_dir, exist_ok=True)

    # --- Annotations (required for training) ---
    print("=== Downloading COCO 2017 annotations ===")
    download_and_extract(ANNOTATIONS_URL, work_dir)
    ann_dir = os.path.join(work_dir, "annotations")
    n = upload_dir(s3, ann_dir, "coco/annotations")
    print(f"Uploaded {n} annotation files")
    shutil.rmtree(ann_dir)

    # --- val2017 images (~5K images, ~1 GB) ---
    print("\n=== Downloading COCO val2017 images ===")
    download_and_extract(VAL_IMAGES_URL, work_dir)
    val_dir = os.path.join(work_dir, "val2017")
    n = upload_dir(s3, val_dir, "coco/val2017")
    print(f"Uploaded {n} val2017 images")
    shutil.rmtree(val_dir)

    # --- train2017 images (~118K images, ~18 GB) ---
    print("\n=== Downloading COCO train2017 images ===")
    download_and_extract(TRAIN_IMAGES_URL, work_dir)
    train_dir = os.path.join(work_dir, "train2017")
    n = upload_dir(s3, train_dir, "coco/train2017")
    print(f"Uploaded {n} train2017 images")
    shutil.rmtree(train_dir)

    print("\nCOCO data ingestion complete!")


if __name__ == "__main__":
    main()
