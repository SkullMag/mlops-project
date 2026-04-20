#!/usr/bin/env bash
set -euo pipefail

DIR="${1:-data}"
mkdir -p "$DIR" && cd "$DIR"

wget -c http://images.cocodataset.org/zips/train2017.zip
wget -c http://images.cocodataset.org/zips/val2017.zip
wget -c http://images.cocodataset.org/annotations/annotations_trainval2017.zip

unzip -qo train2017.zip
unzip -qo val2017.zip
unzip -qo annotations_trainval2017.zip

rm -f train2017.zip val2017.zip annotations_trainval2017.zip

echo "COCO 2017 downloaded to $DIR/"
