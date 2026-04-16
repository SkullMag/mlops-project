import os
import json
import random
import time
import uuid
import swiftclient
from datetime import datetime

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"

SIMULATED_USERS = [f"user_{i:03d}" for i in range(1, 51)]

POSSIBLE_TAGS = [
    "beach", "sunset", "people", "dog", "cat", "food", "car",
    "tree", "building", "mountain", "indoor", "outdoor", "night",
    "sports", "nature", "city", "family", "party", "travel", "art"
]

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

def get_random_image_id(conn):
    _, objects = conn.get_container(CONTAINER_NAME, prefix="coco/images/", limit=1000)
    original_images = [o["name"] for o in objects if "_original" in o["name"]]
    if not original_images:
        return None
    chosen = random.choice(original_images)
    return chosen.split("/")[-1].replace("_original.jpg", "")

def simulate_upload(conn, image_id, user_id):
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
    conn.put_object(
        CONTAINER_NAME, 
        key,
        json.dumps(upload_event).encode()
    )
    print(f"Upload simulated: user={user_id}, image={image_id}, tags={predicted_tags}")
    return request_id, predicted_tags

def simulate_feedback(conn, request_id, image_id, user_id, predicted_tags):
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
        conn.put_object(
            CONTAINER_NAME,
            key,
            json.dumps(event).encode()
        )
        print(f"Feedback: user={user_id}, tag={event['tag']}, action={event['action']}")

    return feedback_events

def main():
    conn = get_swift_conn()
    print("Starting data generator...")
    print(f"Simulating {len(SIMULATED_USERS)} users...")

    iteration = 0
    while True:
        iteration += 1
        print(f"\n--- Iteration {iteration} ---")
        
        num_uploads = random.randint(3, 10)
        for _ in range(num_uploads):
            user_id = random.choice(SIMULATED_USERS)
            image_id = get_random_image_id(conn)
            if not image_id:
                print("No images found in bucket!")
                continue

            request_id, predicted_tags = simulate_upload(conn, image_id, user_id)
            time.sleep(0.5)
            simulate_feedback(conn, request_id, image_id, user_id, predicted_tags)
            time.sleep(0.5)

        print(f"Iteration {iteration} complete. Sleeping 10 seconds...")
        time.sleep(10)

if __name__ == "__main__":
    main()
