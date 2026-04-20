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


def per_class_precision_recall(logits, targets, threshold=0.5):
    """Compute per-class precision and recall using sigmoid threshold."""
    probs = torch.sigmoid(logits)
    preds = (probs >= threshold).float()
    num_classes = targets.size(1)
    precision = {}
    recall = {}
    for c in range(num_classes):
        tp = ((preds[:, c] == 1) & (targets[:, c] == 1)).sum().item()
        fp = ((preds[:, c] == 1) & (targets[:, c] == 0)).sum().item()
        fn = ((preds[:, c] == 0) & (targets[:, c] == 1)).sum().item()
        precision[c] = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall[c] = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    return precision, recall


def compute_all_metrics(logits, targets, k):
    return {
        f"precision_at_{k}": precision_at_k(logits, targets, k),
        f"recall_at_{k}": recall_at_k(logits, targets, k),
        f"f1_at_{k}": f1_at_k(logits, targets, k),
    }
