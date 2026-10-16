# Issue26 YOLOv8m transfer evaluation

This evaluates existing uint8 C3/C3′ reconstructions and a mapped COCO-JPEG baseline. It performs no codec optimization. The private source root contains the verified Issue49 jobs and manifests; configure all locations explicitly.

Use an isolated Python3.10 environment with CUDA PyTorch2.7.1, torchvision0.22.1, numpy1.25.2, pycocotools2.0.11 and Detectron2 compatible with the existing detector-input mapper. Pin ultralytics8.3.0, opencv-python-headless4.10.0.84 and the official `yolov8m.pt` release asset from `https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8m.pt`. Ultralytics8.3.0 is AGPL-3.0; preserve upstream notices and applicable terms. Record the wheel, checkpoint, dependency versions and implementation hashes privately before inference. Other dependencies include matplotlib, pandas, seaborn, psutil, py-cpuinfo and ultralytics-thop. Do not update shared C3 environments.

From the parent of the `c3_neural_compression` package, with the intended checkout on PYTHONPATH:

```sh
python c3_neural_compression/experiments/issue26/evaluate.py --stage prepare --root "$PRIVATE_RUN" --source "$ISSUE49_RUN"
CUDA_VISIBLE_DEVICES=0 CUBLAS_WORKSPACE_CONFIG=:4096:8 python c3_neural_compression/experiments/issue26/evaluate.py --stage smoke --root "$PRIVATE_RUN"
CUDA_VISIBLE_DEVICES=0 CUBLAS_WORKSPACE_CONFIG=:4096:8 python c3_neural_compression/experiments/issue26/evaluate.py --stage full --root "$PRIVATE_RUN"
CUDA_VISIBLE_DEVICES='' python c3_neural_compression/experiments/issue26/report.py --root "$PRIVATE_RUN" --output "$PUBLIC_ASSET_DIR"
```

`prepare` audits180 inputs and existing8 rates, saves a private frozen config and never reads YOLO performance. Before `full`, add the approved `full_max_GPU_seconds` to the config and retain the scope receipt. The executed cap was900seconds, new artifact1GiB and free space3GiB; it does not authorize arbitrary new runs. Smoke adds6 forwards on one fixed image across3 conditions, separately from the180 full inputs. Retain logs and failed startup evidence; do not hide retries.

The model uses FP32, no fusion or augmentation, batch1, fixed640square centered letterbox, scaleup, pad114, stride32. NMS follows pinned detection validation (conf.001,IoU.7,multi_label=True,class-aware,max_det300), rather than implicit predict defaults. Predictions first undo letterbox with the official `scale_boxes`, then undo the D2 codec-input resize. `test_coordinates.py` exercises known boxes in landscape and portrait images, empty output and exact COCO category mapping.

COCOeval aggregates all20 IDs including empty detections and uses maxDets1/10/100. Reports recompute every saved metric twice, preserve all rates and compare AP within each detector. The nine D2 evaluations are reused; nine YOLO evaluations are new. Primary bpp remains the Issue49 mean bits per D2-resized nonpadding pixel. Small-sample unmatched-rate AP differences do not establish a causal loss effect, significance or generalization.

Keep frozen config, weights, source/input hashes, complete predictions and environment provenance private. Published artifacts contain research numbers and validation summaries only. This script intentionally requires the private Issue49 retention layout; it is not a data or checkpoint distribution.
