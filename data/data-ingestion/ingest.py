import os
import urllib.request
import zipfile
import swiftclient
from PIL import Image, ImageEnhance
import random

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"

COCO_ANNOTATIONS_URL = "http://images.cocodataset.org/annotations/annotations_trainval2017.zip"
COCO_TRAIN_URL = "http://images.cocodataset.org/zips/train2017.zip"

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

def download_file(url, dest):
    print(f"Downloading {url}...")
    urllib.request.urlretrieve(url, dest)
    print(f"Downloaded to {dest}")

def upload_to_swift(conn, local_path, remote_key):
    print(f"Uploading {local_path} to {remote_key}...")
    with open(local_path, "rb") as f:
        conn.put_object(CONTAINER_NAME, remote_key, f)
    print(f"Uploaded {remote_key}")

def augment_image(img):
    augmented = []

    # Original resized
    base = img.resize((224, 224))
    augmented.append(("original", base))

    # Horizontal flip
    flipped = base.transpose(Image.FLIP_LEFT_RIGHT)
    augmented.append(("flip", flipped))

    # Random rotation
    angle = random.uniform(-15, 15)
    rotated = base.rotate(angle)
    augmented.append(("rotate", rotated))

    # Color jitter - brightness
    enhancer = ImageEnhance.Brightness(base)
    bright = enhancer.enhance(random.uniform(0.7, 1.3))
    augmented.append(("bright", bright))

    # Color jitter - contrast
    enhancer = ImageEnhance.Contrast(base)
    contrast = enhancer.enhance(random.uniform(0.7, 1.3))
    augmented.append(("contrast", contrast))

    return augmented

def process_and_upload_images(conn, image_dir, max_images=5000):
    count = 0
    aug_count = 0
    for fname in os.listdir(image_dir):
        if not fname.endswith(".jpg"):
            continue
        if count >= max_images:
            break
        fpath = os.path.join(image_dir, fname)
        try:
            img = Image.open(fpath).convert("RGB")
            augmented_versions = augment_image(img)

            for aug_name, aug_img in augmented_versions:
                base_name = fname.replace(".jpg", "")
                out_fname = f"{base_name}_{aug_name}.jpg"
                out_path = f"/tmp/{out_fname}"
                aug_img.save(out_path)
                upload_to_swift(conn, out_path, f"coco/images/{out_fname}")
                os.remove(out_path)
                aug_count += 1

            count += 1
        except Exception as e:
            print(f"Error processing {fname}: {e}")

    print(f"Processed {count} original images into {aug_count} augmented images")

def main():
    conn = get_swift_conn()
    os.makedirs("/data", exist_ok=True)

    # Annotations
    ann_zip = "/data/annotations.zip"
    download_file(COCO_ANNOTATIONS_URL, ann_zip)
    with zipfile.ZipFile(ann_zip, "r") as z:
        z.extractall("/data")
    ann_file = "/data/annotations/instances_train2017.json"
    upload_to_swift(conn, ann_file, "coco/annotations/instances_train2017.json")
    print("Annotations uploaded!")

    # Images with augmentation
    img_zip = "/data/train2017.zip"
    download_file(COCO_TRAIN_URL, img_zip)
    with zipfile.ZipFile(img_zip, "r") as z:
        z.extractall("/data")
    process_and_upload_images(conn, "/data/train2017", max_images=5000)
    print("Pipeline complete!")

if __name__ == "__main__":
    main()
