import json
import os
import random

import numpy as np
from PIL import Image

random.seed(42)
np.random.seed(42)

out = "data"
img_dir = os.path.join(out, "train2017")
val_dir = os.path.join(out, "val2017")
ann_dir = os.path.join(out, "annotations")
os.makedirs(img_dir, exist_ok=True)
os.makedirs(val_dir, exist_ok=True)
os.makedirs(ann_dir, exist_ok=True)

categories = [
    {"id": i, "name": f"cat_{i}", "supercategory": "object"}
    for i in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 17,
              18, 19, 20, 21, 22, 23, 24, 25, 27, 28, 31, 32, 33, 34,
              35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 46, 47, 48, 49,
              50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63,
              64, 65, 67, 70, 72, 73, 74, 75, 76, 77, 78, 79, 80, 81,
              82, 84, 85, 86, 87, 88, 89, 90]
]

def make_split(directory, n_images, ann_path):
    images = []
    annotations = []
    ann_id = 1
    for i in range(n_images):
        fname = f"{i:012d}.jpg"
        img = Image.fromarray(np.random.randint(0, 255, (64, 64, 3), dtype=np.uint8))
        img.save(os.path.join(directory, fname))
        images.append({"id": i, "file_name": fname, "height": 64, "width": 64})
        for _ in range(random.randint(1, 3)):
            cat = random.choice(categories)
            annotations.append({
                "id": ann_id, "image_id": i, "category_id": cat["id"],
                "bbox": [10, 10, 30, 30], "area": 900, "iscrowd": 0,
            })
            ann_id += 1

    with open(ann_path, "w") as f:
        json.dump({
            "images": images,
            "annotations": annotations,
            "categories": categories,
        }, f)

make_split(img_dir, 100, os.path.join(ann_dir, "instances_train2017.json"))
make_split(val_dir, 20, os.path.join(ann_dir, "instances_val2017.json"))
print("Test data created in data/")
