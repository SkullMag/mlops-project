#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

rm -rf outputs

docker compose build training

echo ">> MobileNetV2 baseline"
docker compose run --rm training train.py --config configs/baseline_mobilenet.yaml

echo ">> ResNet-50 + Adam"
docker compose run --rm training train.py --config configs/resnet50_adam.yaml

echo ">> ResNet-50 + SGD"
docker compose run --rm training train.py --config configs/resnet50_sgd.yaml

echo ">> Comparing runs"
docker compose run --rm --entrypoint sh training \
    -c "uv run python compare_runs.py && cp training_runs_comparison.csv /app/outputs/ 2>/dev/null; true"
