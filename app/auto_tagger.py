"""
Auto-tagger sidecar: polls Immich for new assets, runs classification
via the tagger service, and creates ml/* tags on assets.
"""

import io
import logging
import os
import time

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("auto-tagger")

IMMICH_URL = os.environ.get("IMMICH_URL", "http://immich-server.immich.svc.cluster.local:2283")
IMMICH_API_KEY = os.environ["IMMICH_API_KEY"]
TAGGER_URL = os.environ.get("TAGGER_URL", "http://immich-tagger.immich-production.svc.cluster.local:8080")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "5"))
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.5"))
TAG_PREFIX = os.environ.get("TAG_PREFIX", "ml")

FEEDBACK_URL = os.environ.get("FEEDBACK_URL", f"{TAGGER_URL}/feedback")

HEADERS = {"x-api-key": IMMICH_API_KEY}

# Track which assets we've already tagged (in-memory; resets on restart)
_tagged_assets: set[str] = set()
# Track current tags per asset to detect user additions/removals
_asset_tags: dict[str, set[str]] = {}


def get_all_assets() -> list[dict]:
    """Fetch all assets from Immich."""
    resp = requests.post(
        f"{IMMICH_URL}/api/search/metadata",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"size": 1000},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("assets", {}).get("items", [])


def get_asset_tags(asset_id: str) -> list[dict]:
    """Get existing tags for an asset."""
    resp = requests.get(
        f"{IMMICH_URL}/api/assets/{asset_id}",
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("tags", [])


def download_asset_thumbnail(asset_id: str) -> bytes:
    """Download asset thumbnail (preview) for classification."""
    resp = requests.get(
        f"{IMMICH_URL}/api/assets/{asset_id}/thumbnail?size=preview",
        headers=HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def classify_image(image_bytes: bytes) -> list[dict]:
    """Send image to tagger service using Immich ML FormData protocol."""
    import json

    entries = json.dumps({
        "classification": {
            "visual": {
                "modelName": "coco-80-resnet50",
                "options": {"minScore": MIN_SCORE, "topK": 5},
            }
        }
    })

    resp = requests.post(
        f"{TAGGER_URL}/predict",
        files={"image": ("image.jpg", io.BytesIO(image_bytes), "image/jpeg")},
        data={"entries": entries},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    return result.get("classification", [])


def upsert_tag(tag_value: str) -> str:
    """Create tag if it doesn't exist, return tag ID."""
    # Check if tag exists
    resp = requests.get(f"{IMMICH_URL}/api/tags", headers=HEADERS, timeout=10)
    resp.raise_for_status()
    for tag in resp.json():
        if tag["value"] == tag_value:
            return tag["id"]

    # Create tag
    resp = requests.post(
        f"{IMMICH_URL}/api/tags",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"name": tag_value},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def tag_asset(tag_id: str, asset_id: str) -> None:
    """Associate a tag with an asset."""
    resp = requests.put(
        f"{IMMICH_URL}/api/tags/{tag_id}/assets",
        headers={**HEADERS, "Content-Type": "application/json"},
        json={"ids": [asset_id]},
        timeout=10,
    )
    resp.raise_for_status()


def has_ml_tags(asset: dict) -> bool:
    """Check if asset already has ml/* tags."""
    for tag in asset.get("tags", []):
        if tag.get("value", "").startswith(f"{TAG_PREFIX}/"):
            return True
    return False


def get_ml_tag_values(asset: dict) -> set[str]:
    """Get set of ml/* tag values for an asset."""
    return {
        tag["value"]
        for tag in asset.get("tags", [])
        if tag.get("value", "").startswith(f"{TAG_PREFIX}/")
    }


def send_feedback(asset_id: str, tag_value: str, action: str) -> None:
    """Send feedback event to the tagger service."""
    try:
        requests.post(
            FEEDBACK_URL,
            json={
                "request_id": asset_id,
                "image_id": asset_id,
                "user_id": "immich-user",
                "tag": tag_value,
                "action": action,
            },
            timeout=10,
        )
        logger.info("  Feedback sent: %s %s on %s", action, tag_value, asset_id)
    except Exception as e:
        logger.warning("Failed to send feedback for %s: %s", asset_id, e)


def detect_tag_changes(asset: dict) -> None:
    """Compare current tags with tracked state and send feedback for changes."""
    asset_id = asset["id"]
    # Search API doesn't include tags — fetch them from the asset detail endpoint
    tags_list = get_asset_tags(asset_id)
    current_tags = {
        t["value"] for t in tags_list if t.get("value", "").startswith(f"{TAG_PREFIX}/")
    }
    previous_tags = _asset_tags.get(asset_id)

    if previous_tags is None:
        # First time seeing this asset — just record, don't send feedback
        logger.info("Baseline tags for %s: %s", asset_id[:8], current_tags)
        _asset_tags[asset_id] = current_tags
        return

    if current_tags != previous_tags:
        added = current_tags - previous_tags
        removed = previous_tags - current_tags
        logger.info("Tag change on %s: prev=%s curr=%s added=%s removed=%s",
                     asset_id[:8], previous_tags, current_tags, added, removed)

        for tag_value in added:
            send_feedback(asset_id, tag_value, "added")
        for tag_value in removed:
            send_feedback(asset_id, tag_value, "deleted")

    _asset_tags[asset_id] = current_tags


def process_asset(asset: dict) -> None:
    """Classify a single asset and tag it."""
    asset_id = asset["id"]

    if asset_id in _tagged_assets:
        return

    # Skip if already has ML tags
    if has_ml_tags(asset):
        _tagged_assets.add(asset_id)
        return

    logger.info("Processing asset %s (%s)", asset_id, asset.get("originalFileName", "?"))

    try:
        image_bytes = download_asset_thumbnail(asset_id)
        tags = classify_image(image_bytes)

        applied = 0
        for tag_info in tags:
            label = tag_info["label"]
            confidence = tag_info["confidence"]
            if confidence < MIN_SCORE:
                continue

            tag_value = f"{TAG_PREFIX}/{label}"
            tag_id = upsert_tag(tag_value)
            tag_asset(tag_id, asset_id)
            applied += 1
            logger.info("  Tagged: %s (%.2f)", tag_value, confidence)

        _tagged_assets.add(asset_id)
        # Update tracked tags so the next poll doesn't report these as user-added
        _asset_tags[asset_id] = get_ml_tag_values(asset) | {
            f"{TAG_PREFIX}/{t['label']}" for t in tags if t["confidence"] >= MIN_SCORE
        }
        logger.info("Applied %d tag(s) to asset %s", applied, asset_id)

    except Exception as e:
        logger.error("Failed to process asset %s: %s", asset_id, e)


def main():
    logger.info("Auto-tagger starting (poll every %ds)", POLL_INTERVAL)
    logger.info("Immich: %s | Tagger: %s", IMMICH_URL, TAGGER_URL)

    while True:
        try:
            assets = get_all_assets()

            # Detect tag changes (user additions/removals) on all known assets
            for asset in assets:
                detect_tag_changes(asset)

            untagged = [a for a in assets if a["id"] not in _tagged_assets and not has_ml_tags(a)]

            if untagged:
                logger.info("Found %d untagged asset(s) out of %d total", len(untagged), len(assets))
                for asset in untagged:
                    process_asset(asset)
            else:
                logger.info("No new assets to process (%d total, %d tagged)", len(assets), len(_tagged_assets))

        except Exception as e:
            logger.error("Poll cycle failed: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
