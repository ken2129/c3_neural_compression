"""Frozen Faster R-CNN evaluation for original or reconstructed COCO images."""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
from pathlib import Path
import platform
import subprocess
import sys
from typing import Any, Iterable, Sequence

from PIL import Image, ImageDraw

LOGGER = logging.getLogger(__name__)
DETECTOR = "fasterrcnn_resnet50_fpn"
WEIGHTS = "FasterRCNN_ResNet50_FPN_Weights.COCO_V1"
METRICS = ("box_ap", "box_ap50", "box_ap75", "box_ap_small",
           "box_ap_medium", "box_ap_large")

class InputValidationError(ValueError):
  """The input image set is incompatible with the COCO annotation."""

@dataclasses.dataclass(frozen=True)
class ImageRecord:
  image_id: int
  file_name: str
  path: Path
  width: int
  height: int

  def as_dict(self):
    return {"image_id": self.image_id, "file_name": self.file_name,
            "input_path": str(self.path), "width": self.width,
            "height": self.height}

def _read_json(path: Path):
  try:
    with path.open(encoding="utf-8") as stream:
      return json.load(stream)
  except (FileNotFoundError, json.JSONDecodeError) as error:
    raise InputValidationError(f"Cannot read JSON {path}: {error}") from error

def _duplicates(values: Iterable[Any]):
  seen, duplicates = set(), set()
  for value in values:
    if value in seen:
      duplicates.add(value)
    seen.add(value)
  return sorted(duplicates)

def load_subset_ids(path: Path | None):
  """Loads a JSON list or newline-separated COCO image ID list."""
  if path is None:
    return None
  if path.suffix.lower() == ".json":
    values = _read_json(path)
    if not isinstance(values, list):
      raise InputValidationError("Subset JSON must contain a list")
  else:
    try:
      values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    except FileNotFoundError as error:
      raise InputValidationError(f"Missing subset file: {path}") from error
  try:
    image_ids = [int(value) for value in values]
  except (TypeError, ValueError) as error:
    raise InputValidationError("Every subset image ID must be an integer") from error
  if duplicates := _duplicates(image_ids):
    raise InputValidationError(f"Duplicate image IDs in subset: {duplicates}")
  return image_ids

def load_image_path_map(path: Path | None):
  """Loads an explicit COCO image-ID to image-path mapping."""
  if path is None:
    return None
  value = _read_json(path)
  rows = value.get('images') if isinstance(value, dict) else None
  if not isinstance(rows, list):
    raise InputValidationError('Image path map must contain an images list')
  mapping = {}
  for row in rows:
    if not isinstance(row, dict) or not isinstance(row.get('image_id'), int):
      raise InputValidationError('Every image path row needs an integer image_id')
    image_id = row['image_id']
    if image_id in mapping:
      raise InputValidationError(f'Duplicate image ID in path map: {image_id}')
    raw_path = row.get('path')
    if not isinstance(raw_path, str) or not raw_path:
      raise InputValidationError(f'Image ID {image_id} has invalid mapped path')
    mapping[image_id] = Path(raw_path)
  return mapping


def build_image_records(image_root: Path | None, annotation_file: Path,
                        subset_ids: Sequence[int] | None = None,
                        image_paths: dict[int, Path] | None = None):
  """Checks IDs, paths, file uniqueness, readability, and exact dimensions."""
  annotation = _read_json(annotation_file)
  images = annotation.get("images") if isinstance(annotation, dict) else None
  if not isinstance(images, list):
    raise InputValidationError("COCO annotation must contain an images list")
  ids = [item.get("id") for item in images if isinstance(item, dict)]
  if len(ids) != len(images) or any(not isinstance(value, int) for value in ids):
    raise InputValidationError("Every COCO image must have an integer id")
  if duplicates := _duplicates(ids):
    raise InputValidationError(f"Duplicate image IDs in annotations: {duplicates}")
  by_id = {item["id"]: item for item in images}
  selected = list(subset_ids) if subset_ids is not None else ids
  if unknown := sorted(set(selected) - set(by_id)):
    raise InputValidationError(f"Subset IDs absent from annotations: {unknown}")
  if image_paths is not None:
    if missing := sorted(set(selected) - set(image_paths)):
      raise InputValidationError(f'Image IDs absent from path map: {missing}')
    if extra := sorted(set(image_paths) - set(selected)):
      raise InputValidationError(f'Unselected image IDs in path map: {extra}')
  elif image_root is None:
    raise InputValidationError('Either image_root or image path map is required')
  records, paths = [], {}
  for image_id in selected:
    item = by_id[image_id]
    file_name, width, height = (item.get("file_name"), item.get("width"),
                                item.get("height"))
    if not isinstance(file_name, str) or not file_name:
      raise InputValidationError(f"Image {image_id} has invalid file_name")
    if not isinstance(width, int) or not isinstance(height, int):
      raise InputValidationError(f"Image {image_id} has invalid dimensions")
    path = (
        image_paths[image_id]
        if image_paths is not None else image_root / file_name
    )
    resolved = path.resolve()
    if resolved in paths:
      raise InputValidationError(
          f"Image IDs {paths[resolved]} and {image_id} map to one file: {path}")
    paths[resolved] = image_id
    if not path.is_file():
      raise InputValidationError(f"Missing image for image ID {image_id}: {path}")
    try:
      with Image.open(path) as image:
        actual_size = image.size
        image.verify()
    except Exception as error:
      raise InputValidationError(
          f"Unreadable image for image ID {image_id}: {path}: {error}") from error
    if actual_size != (width, height):
      raise InputValidationError(
          f"Size mismatch for image ID {image_id}: annotation={(width, height)}, "
          f"actual={actual_size}, path={path}")
    records.append(ImageRecord(image_id, file_name, path, width, height))
  return records, annotation

def create_frozen_detector(device: str):
  """Creates the fixed official detector in eval mode with frozen parameters."""
  import torchvision
  from torchvision.models.detection import (FasterRCNN_ResNet50_FPN_Weights,
                                             fasterrcnn_resnet50_fpn)
  weights = FasterRCNN_ResNet50_FPN_Weights.COCO_V1
  model = fasterrcnn_resnet50_fpn(weights=weights)
  model.eval()
  model.requires_grad_(False)
  if model.training or any(p.requires_grad for p in model.parameters()):
    raise RuntimeError("Detector is not frozen in eval mode")
  return model.to(device), weights, torchvision.__version__

def detector_config(model, weights):
  """Returns exact weight and GeneralizedRCNN preprocessing metadata."""
  transform = model.transform
  return {
      "name": DETECTOR,
      "weights": WEIGHTS,
      "weights_url": weights.url,
      "weights_meta": weights.meta,
      "weights_transform": repr(weights.transforms()),
      "model_transform": {
          "min_size": list(transform.min_size),
          "max_size": transform.max_size,
          "image_mean": list(transform.image_mean),
          "image_std": list(transform.image_std),
          "size_divisible": transform.size_divisible,
      },
      "training": model.training,
      "all_parameters_frozen": all(
          not parameter.requires_grad for parameter in model.parameters()
      ),
  }

def prediction_to_coco(image_id: int, prediction: dict[str, Any]):
  """Converts torchvision xyxy output to unthresholded COCO xywh rows."""
  boxes = prediction["boxes"].detach().cpu().tolist()
  labels = prediction["labels"].detach().cpu().tolist()
  scores = prediction["scores"].detach().cpu().tolist()
  if not len(boxes) == len(labels) == len(scores):
    raise RuntimeError(f"Malformed detector output for image ID {image_id}")
  return [{"image_id": image_id, "category_id": int(label),
           "bbox": [float(box[0]), float(box[1]), float(box[2] - box[0]),
                    float(box[3] - box[1])], "score": float(score)}
          for box, label, score in zip(boxes, labels, scores)]

def run_inference(model, weights, records, device, batch_size, visualization_limit=0):
  """Runs inference; visualization score thresholds are never applied here."""
  import torch
  rows, raw = [], {}
  transform = weights.transforms()
  with torch.inference_mode():
    for start in range(0, len(records), batch_size):
      batch = records[start:start + batch_size]
      tensors = []
      for record in batch:
        with Image.open(record.path) as image:
          tensors.append(transform(image.convert("RGB")).to(device))
      predictions = model(tensors)
      if len(predictions) != len(batch):
        raise RuntimeError("Detector output batch length mismatch")
      for record, prediction in zip(batch, predictions):
        rows.extend(prediction_to_coco(record.image_id, prediction))
        if len(raw) < visualization_limit:
          raw[record.image_id] = {key: value.detach().cpu()
                                  for key, value in prediction.items()}
      LOGGER.info("Processed %d/%d", start + len(batch), len(records))
  return rows, raw

def evaluate_coco(annotation_file, predictions, image_ids):
  from pycocotools.coco import COCO
  from pycocotools.cocoeval import COCOeval
  ground_truth = COCO(str(annotation_file))
  known_ids = set(ground_truth.getImgIds())
  unknown_ids = sorted(set(image_ids) - known_ids)
  if unknown_ids:
    raise InputValidationError(
        f'Image IDs absent from COCO annotations: {unknown_ids}'
    )
  ground_truth_count = len(ground_truth.getAnnIds(imgIds=list(image_ids)))
  if not predictions:
    LOGGER.warning(
        "Detector produced no predictions for %d images; reporting zero AP.",
        len(image_ids),
    )
    return {
        **{name: 0.0 for name in METRICS},
        'empty_predictions': True,
        'official_cocoeval_executed': False,
        'ground_truth_annotation_count': ground_truth_count,
    }
  evaluator = COCOeval(ground_truth, ground_truth.loadRes(predictions), "bbox")
  evaluator.params.imgIds = list(image_ids)
  evaluator.evaluate()
  evaluator.accumulate()
  evaluator.summarize()
  return {
      **{name: float(evaluator.stats[index])
         for index, name in enumerate(METRICS)},
      'empty_predictions': False,
      'official_cocoeval_executed': True,
      'ground_truth_annotation_count': ground_truth_count,
  }

def save_visualizations(records, raw, annotation, output_dir, threshold, limit):
  """Draws green ground truth and thresholded red predictions."""
  if limit <= 0:
    return
  categories = {item["id"]: item["name"] for item in annotation.get("categories", [])}
  ground_truth = {}
  for item in annotation.get("annotations", []):
    ground_truth.setdefault(item["image_id"], []).append(item)
  visual_dir = output_dir / "visualizations"
  visual_dir.mkdir(parents=True, exist_ok=True)
  for record in records[:limit]:
    with Image.open(record.path) as source:
      image = source.convert("RGB")
    draw = ImageDraw.Draw(image)
    for item in ground_truth.get(record.image_id, []):
      x, y, w, h = item["bbox"]
      draw.rectangle((x, y, x + w, y + h), outline="lime", width=2)
    prediction = raw[record.image_id]
    values = zip(prediction["boxes"].detach().cpu().tolist(),
                 prediction["labels"].detach().cpu().tolist(),
                 prediction["scores"].detach().cpu().tolist())
    for box, label, score in values:
      if score >= threshold:
        draw.rectangle(tuple(box), outline="red", width=2)
        draw.text((box[0], box[1]),
                  f"{categories.get(int(label), label)} {score:.2f}", fill="red")
    image.save(visual_dir / f"{record.image_id:012d}.png")

def environment_info(torch, torchvision_version):
  try:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        check=True, capture_output=True, text=True, timeout=10)
    nvidia_smi = result.stdout.strip().splitlines()
  except (FileNotFoundError, subprocess.SubprocessError):
    nvidia_smi = None
  available = torch.cuda.is_available()
  return {"python": platform.python_version(), "platform": platform.platform(),
          "torch": torch.__version__, "torchvision": torchvision_version,
          "torch_cuda": torch.version.cuda, "cuda_available": available,
          "nvidia_smi_gpu_driver": nvidia_smi,
          "gpu": torch.cuda.get_device_name(torch.cuda.current_device())
                 if available else None}

def write_json(path, value):
  temporary = path.with_suffix(path.suffix + ".tmp")
  with temporary.open("w", encoding="utf-8") as stream:
    json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
    stream.write("\n")
  os.replace(temporary, path)

def _parser():
  parser = argparse.ArgumentParser(description=__doc__)
  source = parser.add_mutually_exclusive_group(required=True)
  source.add_argument("--image-root", type=Path)
  source.add_argument("--image-path-map", type=Path)
  parser.add_argument("--annotation-file", type=Path, required=True)
  parser.add_argument("--output-dir", type=Path, required=True)
  parser.add_argument("--subset-ids", type=Path)
  parser.add_argument("--device", default="cuda")
  parser.add_argument("--batch-size", type=int, default=1)
  parser.add_argument("--visualization-score-threshold", type=float, default=.5)
  parser.add_argument("--visualization-limit", type=int, default=0)
  parser.add_argument("--validate-only", action="store_true",
                      help="Validate inputs without loading weights or inference")
  parser.add_argument("--overwrite", action="store_true")
  return parser

def main(argv: Sequence[str] | None = None):
  args = _parser().parse_args(argv)
  logging.basicConfig(level=logging.INFO,
                      format="%(asctime)s %(levelname)s %(message)s")
  if args.batch_size < 1:
    raise InputValidationError("--batch-size must be positive")
  if not 0 <= args.visualization_score_threshold <= 1:
    raise InputValidationError("Visualization threshold must be in [0, 1]")
  if args.visualization_limit < 0:
    raise InputValidationError("--visualization-limit must be non-negative")
  records, annotation = build_image_records(
      args.image_root, args.annotation_file, load_subset_ids(args.subset_ids),
      load_image_path_map(args.image_path_map))
  LOGGER.info("Validated %d images", len(records))
  if not records:
    raise InputValidationError("No images selected for evaluation")
  if args.validate_only:
    return 0
  import torch
  if args.device.startswith("cuda") and not torch.cuda.is_available():
    raise RuntimeError(f"CUDA requested ({args.device}) but unavailable")
  artifacts = [args.output_dir / name for name in
               ("predictions.json", "metrics.json", "config.json", "evaluation.log")]
  if existing := [path for path in artifacts if path.exists() and not args.overwrite]:
    raise FileExistsError(f"Refusing to overwrite without --overwrite: {existing}")
  args.output_dir.mkdir(parents=True, exist_ok=True)
  file_handler = logging.FileHandler(args.output_dir / "evaluation.log")
  file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
  logging.getLogger().addHandler(file_handler)
  model, weights, torchvision_version = create_frozen_detector(args.device)
  predictions, raw = run_inference(model, weights, records, args.device,
                                   args.batch_size, args.visualization_limit)
  env = environment_info(torch, torchvision_version)
  metrics = evaluate_coco(args.annotation_file, predictions,
                          [record.image_id for record in records])
  metrics.update({"dataset": "COCO2017 val", "image_count": len(records),
                  "detector": DETECTOR, "weights": WEIGHTS,
                  "environment": env})
  config = {
      "created_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
      "detector": detector_config(model, weights),
      "image_root": str(args.image_root.resolve()) if args.image_root else None,
      "image_path_map": (
          str(args.image_path_map.resolve()) if args.image_path_map else None
      ),
      "annotation_file": str(args.annotation_file.resolve()),
      "subset_ids_file": str(args.subset_ids.resolve()) if args.subset_ids else None,
      "device": args.device, "batch_size": args.batch_size,
      "visualization_score_threshold": args.visualization_score_threshold,
      "evaluation_predictions_score_filtered": False, "versions": env,
      "images": [record.as_dict() for record in records],
      "command": [sys.executable, "-m", __spec__.name if __spec__ else __name__]
                 + sys.argv[1:]}
  write_json(args.output_dir / "predictions.json", predictions)
  write_json(args.output_dir / "metrics.json", metrics)
  write_json(args.output_dir / "config.json", config)
  save_visualizations(records, raw, annotation, args.output_dir,
                      args.visualization_score_threshold,
                      args.visualization_limit)
  return 0

if __name__ == "__main__":
  raise SystemExit(main())
