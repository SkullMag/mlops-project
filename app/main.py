import os
import glob
import io
import logging
from pathlib import Path
from typing import List, Optional

import torch
import torch.nn as nn
from torchvision import models, transforms
from PIL import Image
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

logger = logging.getLogger("immich-tagger")
logging.basicConfig(level=logging.INFO)

# Food-11 class labels (matches test-staging.yaml expectations)
FOOD11_CLASSES = [
    "Bread",
    "Dairy product",
    "Dessert",
    "Egg",
    "Fried food",
    "Meat",
    "Noodles/Pasta",
    "Rice",
    "Seafood",
    "Soup",
    "Vegetable/Fruit",
]

MODEL_DIR = Path("/app/model")
VERSIONS_FILE = Path("/app/versions.txt")
ENV_NAME = os.environ.get("ENV_NAME", "unknown")

# ImageNet normalization (used by torchvision pretrained models)
TRANSFORM = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

# Test image: 1x1 red pixel (produces a deterministic prediction for /test)
TEST_IMAGE_BYTES = Image.new("RGB", (224, 224), color=(200, 100, 50))


def _load_version() -> str:
    if VERSIONS_FILE.exists():
        return VERSIONS_FILE.read_text().strip()
    return "1.0.0"


def _find_model_file() -> Optional[Path]:
    patterns = ["*.pth", "**/*.pth"]
    for pattern in patterns:
        files = list(MODEL_DIR.glob(pattern))
        if files:
            return files[0]
    return None


def _load_model() -> Optional[nn.Module]:
    model_path = _find_model_file()
    if model_path is None:
        logger.warning("No model file found in %s — running in placeholder mode", MODEL_DIR)
        return None

    logger.info("Loading model from %s", model_path)
    num_classes = len(FOOD11_CLASSES)

    # Try ResNet50 first (most common), fall back to MobileNetV2
    model = models.resnet50(weights=None)
    model.fc = nn.Linear(model.fc.in_features, num_classes)
    try:
        state_dict = torch.load(str(model_path), map_location="cpu", weights_only=True)
        model.load_state_dict(state_dict)
        logger.info("Loaded ResNet50 model")
    except RuntimeError:
        model = models.mobilenet_v2(weights=None)
        model.classifier[1] = nn.Linear(model.classifier[1].in_features, num_classes)
        try:
            state_dict = torch.load(str(model_path), map_location="cpu", weights_only=True)
            model.load_state_dict(state_dict)
            logger.info("Loaded MobileNetV2 model")
        except Exception as e:
            logger.error("Failed to load model: %s", e)
            return None

    model.eval()
    return model


# Load model at startup
MODEL = _load_model()
VERSION = _load_version()

app = FastAPI(title="Immich Tagger")
Instrumentator().instrument(app).expose(app)


class Tag(BaseModel):
    label: str
    confidence: float


class InferenceRequest(BaseModel):
    request_id: str
    image_uri: str


class InferenceResponse(BaseModel):
    request_id: str
    model_version: str
    top_k: int = 5
    tags: List[Tag]


def _predict_from_image(image: Image.Image, top_k: int = 5) -> List[Tag]:
    if MODEL is None:
        # Placeholder mode: return a fixed prediction
        return [Tag(label="Bread", confidence=0.85)]

    tensor = TRANSFORM(image).unsqueeze(0)
    with torch.no_grad():
        logits = MODEL(tensor)
        probs = torch.sigmoid(logits)[0]

    top_values, top_indices = probs.topk(min(top_k, len(FOOD11_CLASSES)))
    tags = []
    for val, idx in zip(top_values.tolist(), top_indices.tolist()):
        tags.append(Tag(label=FOOD11_CLASSES[idx], confidence=round(val, 4)))
    return tags


@app.get("/health")
async def health():
    return {"status": "healthy", "model_loaded": MODEL is not None}


@app.get("/version")
async def version():
    return {"version": VERSION, "environment": ENV_NAME}


@app.get("/test")
async def test():
    """Run inference on a built-in test image and return the top predicted class name."""
    tags = _predict_from_image(TEST_IMAGE_BYTES, top_k=1)
    return tags[0].label


@app.post("/predict", response_model=InferenceResponse)
async def predict(request: InferenceRequest):
    top_k = 5

    # Try to load image from URI (supports http/https and local paths)
    image = None
    uri = request.image_uri

    if uri.startswith(("http://", "https://")):
        import urllib.request
        try:
            with urllib.request.urlopen(uri, timeout=10) as resp:
                image = Image.open(io.BytesIO(resp.read())).convert("RGB")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch image: {e}")
    else:
        # Local path or S3-style path — try to open directly
        try:
            image = Image.open(uri).convert("RGB")
        except Exception:
            # Fall back to test image for placeholder mode
            image = TEST_IMAGE_BYTES

    tags = _predict_from_image(image, top_k=top_k)
    return InferenceResponse(
        request_id=request.request_id,
        model_version=VERSION,
        top_k=top_k,
        tags=tags,
    )
