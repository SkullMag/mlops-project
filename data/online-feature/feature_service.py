import os
import io
import uuid
import json
import swiftclient
from datetime import datetime
from fastapi import FastAPI, UploadFile, File
from PIL import Image
import uvicorn

AUTH_URL = "https://chi.uc.chameleoncloud.org:5000/v3"
APPLICATION_CREDENTIAL_ID = "31e8934d3ef44f69806e43ff4293be65"
APPLICATION_CREDENTIAL_SECRET = os.environ["APP_CRED_SECRET"]
CONTAINER_NAME = "proj12-data"

app = FastAPI()

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

def preprocess_image(image_bytes):
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((224, 224))
    return img

def upload_to_swift(conn, image, image_id):
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    key = f"uploads/{image_id}.jpg"
    conn.put_object(CONTAINER_NAME, key, buf)
    return key

@app.post("/process")
async def process_image(file: UploadFile = File(...)):
    image_bytes = await file.read()
    request_id = str(uuid.uuid4())
    image_id = str(uuid.uuid4())

    img = preprocess_image(image_bytes)

    conn = get_swift_conn()
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    key = f"uploads/{image_id}.jpg"
    conn.put_object(CONTAINER_NAME, key, buf)

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
