import time
import random
from typing import List
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Immich Image Tagging API")

class Tag(BaseModel):
    label: str
    confidence: float

class InferenceRequest(BaseModel):
    request_id: str
    image_uri: str

class InferenceResponse(BaseModel):
    request_id: str
    model_version: str
    tags: List[Tag]

@app.post("/predict", response_model=InferenceResponse)
def predict(request: InferenceRequest):
    time.sleep(0.05) # Simulate S3 fetch
    time.sleep(0.15) # Simulate inference compute
    
    return InferenceResponse(
        request_id=request.request_id,
        model_version="baseline_untrained_v0",
        tags=[
            Tag(label="person", confidence=round(random.uniform(0.7, 0.99), 2)),
            Tag(label="dog", confidence=round(random.uniform(0.5, 0.95), 2)),
            Tag(label="bench", confidence=round(random.uniform(0.3, 0.85), 2))
        ]
    )

@app.get("/health")
async def health_check():
    return {"status": "healthy"}