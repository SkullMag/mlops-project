import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import torch
import torch.nn as nn
from torchvision import models, transforms
from PIL import Image
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from prometheus_client import Counter, Histogram
from prometheus_fastapi_instrumentator import Instrumentator

logger = logging.getLogger("immich-tagger")
logging.basicConfig(level=logging.INFO)

# COCO 80-class labels (matches training dataset.py order)
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane",
    "bus", "train", "truck", "boat", "traffic light",
    "fire hydrant", "stop sign", "parking meter", "bench", "bird",
    "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack",
    "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat",
    "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut",
    "cake", "chair", "couch", "potted plant", "bed",
    "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven",
    "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]

NUM_CLASSES = len(COCO_CLASSES)

MODEL_DIR = Path("/app/model")
VERSIONS_FILE = Path("/app/versions.txt")
ENV_NAME = os.environ.get("ENV_NAME", "unknown")
DEFAULT_MIN_SCORE = float(os.environ.get("DEFAULT_MIN_SCORE", "0.0"))

# ImageNet normalization (used by torchvision pretrained models)
TRANSFORM = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

# Test image: 224x224 solid color (produces a deterministic prediction for /test)
TEST_IMAGE_BYTES = Image.new("RGB", (224, 224), color=(200, 100, 50))

# ---------------------------------------------------------------------------
# Custom Prometheus metrics
# ---------------------------------------------------------------------------
PREDICTION_COUNTER = Counter(
    "tagger_predictions_total",
    "Total predictions by class label",
    ["label", "environment"],
)
PREDICTION_CONFIDENCE = Histogram(
    "tagger_prediction_confidence",
    "Distribution of prediction confidence scores",
    ["environment"],
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 1.0],
)
INFERENCE_LATENCY = Histogram(
    "tagger_inference_latency_seconds",
    "Model inference latency (excludes HTTP overhead)",
    ["environment"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)
FILTERED_PREDICTIONS_COUNTER = Counter(
    "tagger_filtered_predictions_total",
    "Predictions filtered out by confidence threshold",
    ["label", "environment"],
)
FEEDBACK_COUNTER = Counter(
    "tagger_feedback_total",
    "Total feedback events by action",
    ["action", "environment"],
)

# ---------------------------------------------------------------------------
# MinIO client (optional — prediction logging disabled if not configured)
# ---------------------------------------------------------------------------
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT")
MINIO_ACCESS_KEY = os.environ.get("MINIO_ACCESS_KEY")
MINIO_SECRET_KEY = os.environ.get("MINIO_SECRET_KEY")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "proj12-data")

_s3_client = None


def _get_minio_client():
    global _s3_client
    if _s3_client is None and MINIO_ENDPOINT:
        import boto3

        _s3_client = boto3.client(
            "s3",
            endpoint_url=MINIO_ENDPOINT,
            aws_access_key_id=MINIO_ACCESS_KEY,
            aws_secret_access_key=MINIO_SECRET_KEY,
        )
    return _s3_client


def _log_prediction_to_minio(request_id: str, image_uri: str, tags: List["Tag"]):
    """Log prediction to MinIO for the feedback/batch pipeline."""
    client = _get_minio_client()
    if client is None:
        return
    try:
        event = {
            "request_id": request_id,
            "image_id": request_id,
            "image_uri": image_uri,
            "model_version": VERSION,
            "environment": ENV_NAME,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "predicted_tags": [t.label for t in tags],
            "confidence_scores": {t.label: t.confidence for t in tags},
        }
        key = f"feedback/uploads/{request_id}.json"
        client.put_object(
            Bucket=BUCKET_NAME,
            Key=key,
            Body=json.dumps(event).encode(),
        )
    except Exception as e:
        logger.warning("Failed to log prediction to MinIO: %s", e)


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
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

    # Try ResNet50 first (most common), fall back to MobileNetV2
    model = models.resnet50(weights=None)
    model.fc = nn.Linear(model.fc.in_features, NUM_CLASSES)
    try:
        state_dict = torch.load(str(model_path), map_location="cpu", weights_only=True)
        model.load_state_dict(state_dict)
        logger.info("Loaded ResNet50 model")
    except RuntimeError:
        model = models.mobilenet_v2(weights=None)
        model.classifier[1] = nn.Linear(model.classifier[1].in_features, NUM_CLASSES)
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


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
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


class FeedbackRequest(BaseModel):
    request_id: str
    image_id: str
    user_id: str
    tag: str
    action: str  # "added" or "deleted"


class FeedbackResponse(BaseModel):
    feedback_id: str
    status: str


# ---------------------------------------------------------------------------
# Inference helpers
# ---------------------------------------------------------------------------
def _predict_from_image(image: Image.Image, top_k: int = 5) -> List[Tag]:
    if MODEL is None:
        # Placeholder mode: return mock predictions so the pipeline works without a trained model
        mock_tags = [
            Tag(label="person", confidence=0.92),
            Tag(label="dog", confidence=0.85),
            Tag(label="car", confidence=0.78),
            Tag(label="chair", confidence=0.71),
            Tag(label="bottle", confidence=0.65),
        ]
        return mock_tags[:top_k]

    tensor = TRANSFORM(image).unsqueeze(0)

    start = time.perf_counter()
    with torch.no_grad():
        logits = MODEL(tensor)
        probs = torch.sigmoid(logits)[0]
    elapsed = time.perf_counter() - start
    INFERENCE_LATENCY.labels(environment=ENV_NAME).observe(elapsed)

    top_values, top_indices = probs.topk(min(top_k, NUM_CLASSES))
    tags = []
    for val, idx in zip(top_values.tolist(), top_indices.tolist()):
        tags.append(Tag(label=COCO_CLASSES[idx], confidence=round(val, 4)))
    return tags


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/ping")
async def ping():
    """Immich ML health check — must return plain text 'pong'."""
    return PlainTextResponse("pong")


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


@app.post("/predict")
async def predict_immich(
    entries: str = Form(...),
    image: UploadFile = File(...),
):
    """Immich ML protocol: accepts multipart FormData with 'entries' JSON and 'image' file."""
    parsed = json.loads(entries)

    image_bytes = await image.read()
    pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    response: dict = {
        "imageHeight": pil_image.height,
        "imageWidth": pil_image.width,
    }

    if "classification" in parsed:
        options = parsed["classification"].get("visual", {}).get("options", {})
        top_k = options.get("topK", 5)
        min_score = options.get("minScore", 0.0)

        tags = _predict_from_image(pil_image, top_k=top_k)
        response["classification"] = [
            {"label": t.label, "confidence": t.confidence}
            for t in tags
            if t.confidence >= min_score
        ]

        # Record custom metrics
        for t in tags:
            PREDICTION_COUNTER.labels(label=t.label, environment=ENV_NAME).inc()
            PREDICTION_CONFIDENCE.labels(environment=ENV_NAME).observe(t.confidence)

    return response


@app.post("/predict/legacy", response_model=InferenceResponse)
async def predict_legacy(request: InferenceRequest):
    """Legacy JSON endpoint for load tests and staging checks."""
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

    all_tags = _predict_from_image(image, top_k=top_k)

    # Record custom metrics for all predictions
    for tag in all_tags:
        PREDICTION_CONFIDENCE.labels(environment=ENV_NAME).observe(tag.confidence)

    # Apply confidence threshold
    tags = [t for t in all_tags if t.confidence >= DEFAULT_MIN_SCORE]
    for tag in tags:
        PREDICTION_COUNTER.labels(label=tag.label, environment=ENV_NAME).inc()
    for tag in all_tags:
        if tag.confidence < DEFAULT_MIN_SCORE:
            FILTERED_PREDICTIONS_COUNTER.labels(label=tag.label, environment=ENV_NAME).inc()

    # Log prediction to MinIO (best-effort, non-blocking)
    _log_prediction_to_minio(request.request_id, uri, tags)

    return InferenceResponse(
        request_id=request.request_id,
        model_version=VERSION,
        top_k=top_k,
        tags=tags,
    )


@app.post("/feedback", response_model=FeedbackResponse)
async def feedback(req: FeedbackRequest):
    """Accept user feedback (tag added or deleted) and log to MinIO."""
    if req.action not in ("added", "deleted"):
        raise HTTPException(status_code=400, detail="action must be 'added' or 'deleted'")

    feedback_id = str(uuid.uuid4())
    FEEDBACK_COUNTER.labels(action=req.action, environment=ENV_NAME).inc()

    client = _get_minio_client()
    if client is not None:
        try:
            event = {
                "feedback_id": feedback_id,
                "request_id": req.request_id,
                "image_id": req.image_id,
                "user_id": req.user_id,
                "tag": req.tag,
                "action": req.action,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            key = f"feedback/events/{feedback_id}.json"
            client.put_object(
                Bucket=BUCKET_NAME,
                Key=key,
                Body=json.dumps(event).encode(),
            )
        except Exception as e:
            logger.warning("Failed to log feedback to MinIO: %s", e)

    return FeedbackResponse(feedback_id=feedback_id, status="recorded")
