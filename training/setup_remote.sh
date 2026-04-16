#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/training"

sudo apt-get update -qq
sudo apt-get install -y -qq docker.io docker-compose-v2 unzip wget curl > /dev/null
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

if [ "${SYNTHETIC:-0}" = "1" ]; then
    echo "generating synthetic data..."
    uv run --with numpy --with pillow python create_test_data.py
else
    if [ ! -d "data/train2017" ]; then
        bash download_coco.sh data
    else
        echo "coco data present, skipping"
    fi
fi

sudo docker compose up -d mlflow
for i in $(seq 1 30); do
    curl -sf http://localhost:5000/ > /dev/null 2>&1 && break
    sleep 2
done

sudo bash run_experiments.sh

echo "done. mlflow: http://$(curl -sf ifconfig.me):5000"
