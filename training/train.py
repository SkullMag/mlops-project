import argparse
import os
import time
from pathlib import Path

import mlflow
import torch
import torch.nn as nn
import yaml

from dataset import COCO_CATEGORY_NAMES, create_dataloaders
from metrics import compute_all_metrics, per_class_precision_recall
from models import build_model


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def set_seed(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False


def get_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def make_optimizer(params, cfg):
    opt = cfg["training"]["optimizer"].lower()
    lr = cfg["training"]["learning_rate"]
    wd = cfg["training"]["weight_decay"]
    if opt == "adam":
        return torch.optim.Adam(params, lr=lr, weight_decay=wd)
    if opt == "sgd":
        return torch.optim.SGD(params, lr=lr, momentum=0.9, weight_decay=wd)
    raise ValueError(f"Unknown optimizer: {opt}")


def make_scheduler(optimizer, cfg):
    sched = cfg["training"]["scheduler"]
    if sched == "cosine":
        return torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=cfg["training"]["epochs"])
    if sched == "step":
        return torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=cfg["training"]["step_size"],
            gamma=cfg["training"]["step_gamma"])
    return None


def train_one_epoch(model, loader, criterion, optimizer, device, max_batches=None):
    model.train()
    running_loss = 0.0
    total_samples = 0
    num_batches = len(loader) if max_batches is None else min(len(loader), max_batches)
    t_epoch = time.time()
    for i, (imgs, targets) in enumerate(loader, 1):
        imgs, targets = imgs.to(device), targets.to(device)
        optimizer.zero_grad()
        loss = criterion(model(imgs), targets)
        loss.backward()
        optimizer.step()
        running_loss += loss.item() * imgs.size(0)
        total_samples += imgs.size(0)

        if i % 10 == 0 or i == num_batches:
            elapsed = time.time() - t_epoch
            it_per_sec = i / elapsed
            eta = (num_batches - i) / it_per_sec
            print(f"  batch {i}/{num_batches}  {it_per_sec:.2f} it/s  ETA {eta:.0f}s", flush=True)

        if max_batches is not None and i >= max_batches:
            break

    return running_loss / total_samples


@torch.no_grad()
def evaluate(model, loader, criterion, device, k, max_batches=None):
    model.eval()
    running_loss = 0.0
    total_samples = 0
    all_logits, all_targets = [], []
    for i, (imgs, targets) in enumerate(loader, 1):
        imgs, targets = imgs.to(device), targets.to(device)
        logits = model(imgs)
        running_loss += criterion(logits, targets).item() * imgs.size(0)
        total_samples += imgs.size(0)
        all_logits.append(logits)
        all_targets.append(targets)
        if max_batches is not None and i >= max_batches:
            break

    metrics = compute_all_metrics(torch.cat(all_logits), torch.cat(all_targets), k)
    metrics["validation_loss"] = running_loss / total_samples
    return metrics


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    cfg = load_config(args.config)
    set_seed(cfg["seed"])
    device = get_device()
    k = cfg["evaluation"]["top_k"]
    num_epochs = cfg["training"]["epochs"]
    max_batches = cfg["training"].get("max_batches")

    train_loader, val_loader = create_dataloaders(cfg)
    model = build_model(
        cfg["model"]["type"], cfg["model"]["num_classes"],
        pretrained=cfg["model"]["pretrained"],
    ).to(device)

    init_ckpt = os.environ.get("INIT_CHECKPOINT", "").strip()
    if init_ckpt:
        if not os.path.isfile(init_ckpt):
            raise RuntimeError(f"INIT_CHECKPOINT set but file not found: {init_ckpt}")
        print(f"Loading init weights from {init_ckpt}", flush=True)
        state = torch.load(init_ckpt, map_location=device)
        model.load_state_dict(state)

    criterion = nn.BCEWithLogitsLoss()
    optimizer = make_optimizer(model.parameters(), cfg)
    scheduler = make_scheduler(optimizer, cfg)

    mlflow.set_tracking_uri(cfg["mlflow"]["tracking_uri"])
    mlflow.set_experiment(cfg["mlflow"]["experiment_name"])
    run_name = cfg["mlflow"]["run_name"] or f"{cfg['model']['type']}_lr{cfg['training']['learning_rate']}"

    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params({
            "model_type": cfg["model"]["type"],
            "pretrained": cfg["model"]["pretrained"],
            "num_classes": cfg["model"]["num_classes"],
            "learning_rate": cfg["training"]["learning_rate"],
            "batch_size": cfg["training"]["batch_size"],
            "epochs": num_epochs,
            "optimizer": cfg["training"]["optimizer"],
            "weight_decay": cfg["training"]["weight_decay"],
            "scheduler": cfg["training"]["scheduler"],
            "image_size": cfg["data"]["image_size"],
            "top_k": k, "seed": cfg["seed"],
        })
        gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none"
        mlflow.log_params({
            "device": os.environ.get("MLFLOW_LOG_DEVICE", str(device)),
            "gpu_name": os.environ.get("MLFLOW_LOG_GPU", gpu),
        })

        t_start = time.time()
        best_f1 = 0.0

        for epoch in range(1, num_epochs + 1):
            t0 = time.time()
            train_loss = train_one_epoch(model, train_loader, criterion, optimizer, device, max_batches)
            val = evaluate(model, val_loader, criterion, device, k, max_batches)
            dt = time.time() - t0

            if scheduler:
                scheduler.step()

            pk = val[f"precision_at_{k}"]
            f1 = val[f"f1_at_{k}"]
            mlflow.log_metrics({
                "train_loss": train_loss,
                "validation_loss": val["validation_loss"],
                f"precision_at_{k}": pk,
                f"recall_at_{k}": val[f"recall_at_{k}"],
                f"f1_at_{k}": f1,
                "time_per_epoch": dt,
            }, step=epoch)

            print(
                f"[{epoch}/{num_epochs}] "
                f"train_loss={train_loss:.4f}  val_loss={val['validation_loss']:.4f}  "
                f"P@{k}={pk:.4f}  R@{k}={val[f'recall_at_{k}']:.4f}  "
                f"F1@{k}={f1:.4f}  ({dt:.1f}s)"
            )

            if f1 > best_f1:
                best_f1 = f1
                ckpt = Path("outputs") / run_name / "best_model.pth"
                ckpt.parent.mkdir(parents=True, exist_ok=True)
                torch.save(model.state_dict(), ckpt)
                mlflow.log_artifact(str(ckpt))

        # Log per-class precision/recall for fairness monitoring
        all_logits, all_targets = [], []
        model.eval()
        with torch.no_grad():
            for i, (imgs, targets) in enumerate(val_loader, 1):
                imgs, targets = imgs.to(device), targets.to(device)
                all_logits.append(model(imgs))
                all_targets.append(targets)
                if max_batches is not None and i >= max_batches:
                    break
        cls_prec, cls_rec = per_class_precision_recall(
            torch.cat(all_logits), torch.cat(all_targets),
        )
        for idx in range(min(len(COCO_CATEGORY_NAMES), len(cls_prec))):
            name = COCO_CATEGORY_NAMES[idx].replace(" ", "_")
            mlflow.log_metric(f"class_precision/{name}", cls_prec[idx])
            mlflow.log_metric(f"class_recall/{name}", cls_rec[idx])

        total = time.time() - t_start
        mlflow.log_metric("total_training_time", total)
        mlflow.log_metric("best_f1", best_f1)
        mlflow.log_artifact(args.config)
        print(f"\ndone in {total:.1f}s, best F1@{k}={best_f1:.4f}")

        return run.info.run_id, best_f1


if __name__ == "__main__":
    main()
