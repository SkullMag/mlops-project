import torch


def precision_at_k(logits, targets, k):
    topk_idx = logits.topk(k, dim=1).indices
    hits = targets.gather(1, topk_idx)
    return hits.sum().item() / (logits.size(0) * k)


def recall_at_k(logits, targets, k):
    topk_idx = logits.topk(k, dim=1).indices
    hits = targets.gather(1, topk_idx)
    n_pos = targets.sum(dim=1).clamp(min=1)
    per_sample = hits.sum(dim=1) / n_pos
    return per_sample.mean().item()


def f1_at_k(logits, targets, k):
    p = precision_at_k(logits, targets, k)
    r = recall_at_k(logits, targets, k)
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def compute_all_metrics(logits, targets, k):
    return {
        f"precision_at_{k}": precision_at_k(logits, targets, k),
        f"recall_at_{k}": recall_at_k(logits, targets, k),
        f"f1_at_{k}": f1_at_k(logits, targets, k),
    }
