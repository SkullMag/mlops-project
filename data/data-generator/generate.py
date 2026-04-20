import os
import json
import random
import time
import uuid
import boto3
from datetime import datetime

# MinIO connection settings - all from environment variables
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

SIMULATED_USERS = [f"user_{i:03d}" for i in range(1, 51)]

POSSIBLE_TAGS = [
    "beach", "sunset", "people", "dog", "cat", "food", "car",
    "tree", "building", "mountain", "indoor", "outdoor", "night",
    "sports", "nature", "city", "family", "party", "travel", "art"
]

def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY
    )

def get_random_image_id(s3):
    response = s3.list_objects_v2(
        Bucket=BUCKET_NAME,
        Prefix="coco/val2017/",
        MaxKeys=1000
    )
    objects = response.get("Contents", [])
    images = [o["Key"] for o in objects if o["Key"].endswith((".jpg", ".jpeg", ".png"))]
    if not images:
        return None
    chosen = random.choice(images)
    return chosen.split("/")[-1].rsplit(".", 1)[0]

def simulate_upload(s3, image_id, user_id):
    request_id = str(uuid.uuid4())
    predicted_tags = random.sample(POSSIBLE_TAGS, random.randint(2, 5))

    upload_event = {
        "request_id": request_id,
        "image_id": image_id,
        "user_id": user_id,
        "timestamp": datetime.utcnow().isoformat(),
        "image_uri": f"s3://immich/uploads/{image_id}.jpg",
        "predicted_tags": predicted_tags,
        "confidence_scores": {
            tag: round(random.uniform(0.5, 0.99), 2)
            for tag in predicted_tags
        }
    }

    key = f"feedback/uploads/{request_id}.json"
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=json.dumps(upload_event).encode()
    )
    print(f"Upload simulated: user={user_id}, image={image_id}, tags={predicted_tags}")
    return request_id, predicted_tags

def simulate_feedback(s3, request_id, image_id, user_id, predicted_tags):
    feedback_events = []

    for tag in predicted_tags:
        if random.random() < 0.2:
            feedback_events.append({
                "feedback_id": str(uuid.uuid4()),
                "request_id": request_id,
                "image_id": image_id,
                "user_id": user_id,
                "tag": tag,
                "action": "deleted",
                "timestamp": datetime.utcnow().isoformat()
            })

    if random.random() < 0.3:
        new_tag = random.choice([t for t in POSSIBLE_TAGS if t not in predicted_tags])
        feedback_events.append({
            "feedback_id": str(uuid.uuid4()),
            "request_id": request_id,
            "image_id": image_id,
            "user_id": user_id,
            "tag": new_tag,
            "action": "added",
            "timestamp": datetime.utcnow().isoformat()
        })

    for event in feedback_events:
        key = f"feedback/events/{event['feedback_id']}.json"
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(event).encode()
        )
        print(f"Feedback: user={user_id}, tag={event['tag']}, action={event['action']}")

    return feedback_events

def main():
    s3 = get_minio_client()
    print("Starting data generator...")
    print(f"Simulating {len(SIMULATED_USERS)} users...")

    iteration = 0
    while True:
        iteration += 1
        print(f"\n--- Iteration {iteration} ---")

        num_uploads = random.randint(3, 10)
        for _ in range(num_uploads):
            user_id = random.choice(SIMULATED_USERS)
            image_id = get_random_image_id(s3)
            if not image_id:
                print("No images found in bucket!")
                continue

            request_id, predicted_tags = simulate_upload(s3, image_id, user_id)
            time.sleep(0.5)
            simulate_feedback(s3, request_id, image_id, user_id, predicted_tags)
            time.sleep(0.5)

        print(f"Iteration {iteration} complete. Sleeping 10 seconds...")
        time.sleep(10)

if __name__ == "__main__":
    main()
