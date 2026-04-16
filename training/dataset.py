import os

import torch
from PIL import Image
from pycocotools.coco import COCO
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms

COCO_CATEGORY_NAMES = [
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


class CocoMultiLabelDataset(Dataset):

    def __init__(self, img_dir, ann_file, transform=None):
        self.img_dir = img_dir
        self.transform = transform
        self.coco = COCO(ann_file)
        self.img_ids = sorted(self.coco.getImgIds())
        self.num_classes = 80

        cats = sorted(self.coco.getCatIds())
        self.cat_to_idx = {c: i for i, c in enumerate(cats)}

    def __len__(self):
        return len(self.img_ids)

    def __getitem__(self, idx):
        img_id = self.img_ids[idx]
        img_info = self.coco.loadImgs(img_id)[0]
        path = os.path.join(self.img_dir, img_info["file_name"])
        image = Image.open(path).convert("RGB")

        if self.transform:
            image = self.transform(image)

        ann_ids = self.coco.getAnnIds(imgIds=img_id)
        anns = self.coco.loadAnns(ann_ids)
        labels = torch.zeros(self.num_classes, dtype=torch.float32)
        for ann in anns:
            labels[self.cat_to_idx[ann["category_id"]]] = 1.0

        return image, labels


def get_transforms(image_size, train=False):
    normalize = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    if train:
        return transforms.Compose([
            transforms.RandomResizedCrop(image_size),
            transforms.RandomHorizontalFlip(),
            transforms.ColorJitter(brightness=0.2, contrast=0.2),
            transforms.ToTensor(),
            normalize,
        ])
    return transforms.Compose([
        transforms.Resize(int(image_size * 1.14)),
        transforms.CenterCrop(image_size),
        transforms.ToTensor(),
        normalize,
    ])


def create_dataloaders(cfg):
    data = cfg["data"]
    bs = cfg["training"]["batch_size"]

    train_ds = CocoMultiLabelDataset(
        data["train_img_dir"], data["train_ann_file"],
        transform=get_transforms(data["image_size"], train=True),
    )
    val_ds = CocoMultiLabelDataset(
        data["val_img_dir"], data["val_ann_file"],
        transform=get_transforms(data["image_size"], train=False),
    )

    train_loader = DataLoader(
        train_ds, batch_size=bs, shuffle=True,
        num_workers=data["num_workers"], pin_memory=True,
    )
    val_loader = DataLoader(
        val_ds, batch_size=bs, shuffle=False,
        num_workers=data["num_workers"], pin_memory=True,
    )
    return train_loader, val_loader
