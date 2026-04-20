import os
import io
import uuid
import json
import boto3
from datetime import datetime
from fastapi import FastAPI, UploadFile, File
from PIL import Image
import uvicorn

# MinIO connection settings - all from environment variables
MINIO_ENDPOINT = os.environ["MINIO_ENDPOINT"]
MINIO_ACCESS_KEY = os.environ["MINIO_ACCESS_KEY"]
MINIO_SECRET_KEY = os.environ["MINIO_SECRET_KEY"]
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

app = FastAPI()

def get_minio_client():
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY
    )

def preprocess_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((224, 224))
    return img

@app.post("/process")
async def process_image(file: UploadFile = File(...)):
    image_bytes = await file.read()
    request_id = str(uuid.uuid4())
    image_id = str(uuid.uuid4())

    img = preprocess_image(image_bytes)

    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    s3 = get_minio_client()
    key = f"uploads/{image_id}.jpg"
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=key,
        Body=buf.getvalue()
    )

    result = {
        "request_id": request_id,
        "image_uri": f"s3://immich/uploads/{image_id}.jpg",
        "timestamp": datetime.utcnow().isoformat(),
        "preprocessing": {
            "resized_to": "224x224",
            "normalized": True,
            "format": "JPEG"
        }
    }

    print(f"Processed image: request_id={request_id}, stored at {key}")
    return result

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
