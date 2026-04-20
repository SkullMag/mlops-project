# Training Pipeline

Multi-label image classification on COCO 2017 (80 categories). ResNet-50 as the main model, MobileNetV2 as a lighter baseline. Everything runs in Docker on Chameleon Cloud, tracked with MLflow.

## Setup

Needs `uv` and `docker` installed.

```
uv sync
```

## Quick start (local, synthetic data)

```
uv run python create_test_data.py
docker compose up -d mlflow
docker compose build training
docker compose run --rm training train.py --config configs/resnet50_adam.yaml
```

## Run all experiments

```
bash run_experiments.sh
```

Runs 3 configs (mobilenet baseline, resnet50+adam, resnet50+sgd), then prints a comparison table.

## Deploy to Chameleon

```
SYNTHETIC=1 bash deploy_chameleon.sh   # synthetic data, fast test
bash deploy_chameleon.sh               # real COCO data
```

This provisions a bare-metal instance, pushes code, downloads data, starts MLflow, and runs everything.

## Project structure

- `train.py` - training loop
- `models.py` - ResNet-50 and MobileNetV2
- `dataset.py` - COCO dataset loader
- `metrics.py` - precision@k, recall@k, f1@k
- `compare_runs.py` - pulls runs from MLflow and generates comparison table
- `configs/` - experiment configs (yaml)
- `Dockerfile` + `docker-compose.yaml` - containerized training + MLflow
- `deploy_chameleon.sh` - one-command Chameleon deployment
- `setup_remote.sh` - server setup (docker, data, experiments)
- `run_experiments.sh` - runs all 3 experiments sequentially

## MLflow

After experiments run, MLflow is at `http://<server-ip>:5000`. All params, metrics (per epoch), cost metrics, and model checkpoints are logged there.

## Configs

Each experiment is a yaml file in `configs/`. To add a new experiment, copy one and change the values. No code changes needed.
