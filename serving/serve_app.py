import asyncio
import random
from typing import List
from fastapi import FastAPI
from pydantic import BaseModel
from ray import serve

app = FastAPI(title="Immich API - Dynamic Batching")

# --- Define the JSON Contract ---
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

# --- Ray Serve Deployment ---
@serve.deployment
@serve.ingress(app)
class ImmichServeModel:
    
    # This decorator tells Ray to wait up to 50ms to collect a batch of 16 requests
    @serve.batch(max_batch_size=16, batch_wait_timeout_s=0.05)
    async def process_batch(self, requests: List[InferenceRequest]) -> List[InferenceResponse]:
        # 1. Simulate fetching images (async so the server doesn't freeze)
        await asyncio.sleep(0.05)
        
        # 2. Simulate ML compute for the ENTIRE batch. 
        # On a GPU, doing matrix math for 16 images takes roughly 
        # the same time as 1 image. This is the magic of batching!
        await asyncio.sleep(0.15)
        
        # 3. Return a response for each request in the batch
        return [
            InferenceResponse(
                request_id=req.request_id,
                model_version="ray_serve_dynamic_batching",
                tags=[
                    Tag(label="person", confidence=round(random.uniform(0.7, 0.99), 2)),
                    Tag(label="dog", confidence=round(random.uniform(0.5, 0.95), 2))
                ]
            ) for req in requests
        ]

    @app.post("/predict")
    async def predict(self, request: InferenceRequest):
        # Ray Serve intercepts individual requests here and routes them to the batcher
        return await self.process_batch(request)

# Bind the deployment so Ray Serve can run it
app_node = ImmichServeModel.bind()

if __name__ == "__main__":
    import ray
    from ray import serve
    
    # 1. Initialize a local Ray cluster
    ray.init()
    
    # 2. Start Ray Serve and explicitly bind to 0.0.0.0 so Docker can expose the port
    serve.start(http_options={"host": "0.0.0.0", "port": 8000})
    
    # 3. Run your deployment (replace 'app_node' with whatever your bound app variable is named)
    serve.run(app_node, blocking=True)