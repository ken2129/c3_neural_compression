"""Audit private pilot artifacts and export only explicitly selected research values."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re

ORDER = ("smoke_image", "smoke_combined", "A", "C", "B", "D")
ARMS = ("A", "C", "B", "D")


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite(value):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value), "Nonfinite or invalid numerical result")
    return value


def png_psnr(reference, reconstruction):
    import numpy as np
    difference = reference.astype(np.float64) - reconstruction.astype(np.float64)
    mse = float(np.mean(difference * difference)) / (255.0 * 255.0)
    require(mse > 0, "Exact reconstruction needs explicit infinite-PSNR handling")
    return -10.0 * math.log10(mse)


def audit(root):
    root = Path(root)
    plan = read(root / "pilot/execution_plan.json")
    manifest = read(root / "assets/pilot_manifest.json")
    original = read(root / "assets/original_calibration_manifest.json")
    cpu = read(root / "cpu_preflight.json")
    require(manifest["image_ids"] == original["image_ids"][:1], "Not the first frozen calibration image")
    require(len(manifest["images"]) == 1, "Expected one image")
    require(cpu["status"] == "passed", "CPU preflight did not pass")
    for path, expected in plan["asset_sha256"].items():
        require(digest(path) == expected, "Private asset changed")
    for path, expected in plan["execution_source_sha256"].items():
        require(digest(path) == expected, "Execution wrapper changed")

    # This alternate CPU mapper must match the archived official D2 byte hash.
    import numpy as np
    from PIL import Image
    height, width, channels = cpu["mapped_shape"]
    require(channels == 3, "Expected RGB")
    with Image.open(manifest["images"][0]["path"]) as image:
        reference = np.array(image.convert("RGB").resize((width, height), Image.Resampling.BILINEAR))
    require(hashlib.sha256(reference.tobytes()).hexdigest() == cpu["mapped_uint8_sha256"],
            "Independent mapper does not match archived input")
    pixels = height * width
    resolved = read(root / "pilot/resolved_configs.json")
    registry = read(root / "pilot/job_registry.json")
    require(registry["scope_plan_sha256"] == digest(root / "pilot/execution_plan.json"), "Registry mismatch")
    seeds, rows, trace = [], [], []
    for name in ORDER:
        job_path = root / "pilot/jobs" / (name + ".json")
        job = read(job_path)
        require(digest(job_path) == registry["job_sha256"][name], "Job changed")
        receipt_path = root / "control" / (name + "_receipt.json")
        receipt = read(receipt_path) if receipt_path.exists() else {"status": "not_started"}
        if receipt_path.exists():
            require(receipt["job_sha256"] == digest(job_path), "Attempt receipt job mismatch")
            require(receipt["plan_sha256"] in (digest(root / "pilot/execution_plan.json"),
                                               digest(root / "initial_execution_plan.json")), "Attempt plan mismatch")
        row = dict(condition=name, objective=job["objective"], noise_steps=job["noise_steps"],
                   max_STE_steps=job["max_STE_steps"], rd_weight=resolved[name]["experiment_kwargs"]["config"]["loss"]["rd_weight"],
                   status=receipt["status"], worker_wall_seconds=receipt.get("gpu_process_seconds"),
                   sampled_peak_GPU_MiB=receipt.get("sampled_peak_GPU_memory_MiB"),
                   sampled_peak_group_RSS_GiB=(receipt["peak_group_RSS_bytes"] / 1024**3
                                              if "peak_group_RSS_bytes" in receipt else None))
        if receipt["status"] == "stopped_or_failed":
            row["failure_category"] = "stopped_or_failed"
        input_path = Path(job["evidence_dir"]) / "datum_00000/input_identity.json"
        if input_path.exists():
            identity = read(input_path)
            require(identity["image_id"] == manifest["image_ids"][0], "Input correspondence mismatch")
            seeds.append(identity["initial_datum_rng"])
        log_path = Path(job["job_dir"]) / "process.log"
        if log_path.exists():
            log = log_path.read_text()
            if receipt["status"] == "stopped_or_failed" and "Unknown backend cpu" in log:
                row["failure_category"] = "launcher_cpu_backend_disabled"
            pattern = r"(\d+)/(\d+)\+(\d+),loss=([\deE+.\-]+), psnr=([\deE+.\-]+), bpp=([\deE+.\-]+)"
            for match in re.finditer(pattern, log):
                step, noise, ste, loss, psnr, bpp = match.groups()
                point = dict(condition=name, logged_step=int(step), noise_budget=int(noise),
                             max_STE_steps=int(ste), loss=float(loss), psnr_float_db=float(psnr),
                             latent_bpp=float(bpp), image_mse_from_logged_PSNR=10 ** (-float(psnr) / 10))
                block = re.split(r"\n[IWEF]\d{4}", log[match.end():match.end() + 512], maxsplit=1)[0]
                for key, exported in (("machine_distortion", "feature_L1"), ("rd_weight", "rd_weight"),
                                      ("soft_round_temp", "soft_round_temp"), ("time", "logged_interval_seconds")):
                    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
                    value = re.search(r"\b" + key + r"=(" + number + r")", block)
                    if value:
                        point[exported] = float(value.group(1))
                trace.append(point)
        if receipt["status"] != "complete":
            rows.append(row)
            continue
        require(receipt["returncode"] == 0, "Invalid completed return code")
        require(receipt["job_sha256"] == digest(job_path), "Receipt job mismatch")
        require(receipt["plan_sha256"] == digest(root / "pilot/execution_plan.json"), "Completed receipt plan mismatch")
        output = Path(job["output_dir"])
        metric_paths = list(output.glob("datum_*/metrics.json"))
        require(len(metric_paths) == 1, "Missing or duplicate metrics")
        metrics = read(metric_paths[0])
        fields = metrics["experiment_signature"]["fields"]
        expected = resolved[name]["experiment_kwargs"]["config"]
        require(fields["code_commit"] == plan["runtime_commit"], "Codec source mismatch")
        for actual_key, config_key in (("optimization", "opt"), ("quantization", "quant"), ("model", "model")):
            require(fields[actual_key] == expected[config_key], "Scientific config mismatch")
        require(fields["random_seed"] == 20261004, "Seed rule mismatch")
        require(fields["rd_weight"] == row["rd_weight"] and fields["image_weight"] == 1.0,
                "Objective coefficient mismatch")
        require(fields["machine_weight"] == (0.0 if job["objective"] == "image" else 0.1),
                "Feature coefficient mismatch")
        require(metrics["input_shape"] == cpu["mapped_shape"], "Codec shape mismatch")
        require(metrics["datum_metadata"]["image_id"] == manifest["image_ids"][0], "Image mismatch")
        require(metrics["datum_metadata"]["mapped_rgb_uint8_sha256"] == cpu["mapped_uint8_sha256"],
                "Mapped bytes mismatch")
        require(metrics["actual_bitstream_generated"] is False, "Unexpected bitstream claim")
        bits = metrics["estimated_bits"]
        require(all(finite(bits[key]) >= 0 for key in ("latents", "synthesis_network", "entropy_network", "total")),
                "Negative estimated bits")
        total = sum(finite(bits[key]) for key in ("latents", "synthesis_network", "entropy_network"))
        require(math.isclose(total, finite(bits["total"]), rel_tol=1e-6, abs_tol=1e-3), "Bits do not sum")
        require(math.isclose(total / pixels, finite(metrics["estimated_bpp"]["total"]), rel_tol=1e-6),
                "Rate denominator mismatch")
        with Image.open(output / "datum_00000/reconstruction.png") as image:
            require(image.mode == "RGB" and image.size == (width, height), "PNG shape mismatch")
            reconstruction = np.array(image)
        artifact = Path(job["evidence_dir"]) / "datum_00000"
        verification = read(artifact / "final_artifacts_verified.json")
        for key, path in (("sha256", artifact / "quantized_params.pkl"),
                          ("trained_params_sha256", artifact / "trained_params.pkl"),
                          ("PNG_sha256", output / "datum_00000/reconstruction.png"),
                          ("metrics_sha256", metric_paths[0])):
            require(digest(path) == verification[key], "Retained artifact mismatch")
        row.update(total_estimated_bits=total, latent_estimated_bits=bits["latents"],
                   synthesis_estimated_bits=bits["synthesis_network"], entropy_estimated_bits=bits["entropy_network"],
                   codec_pixels=pixels, estimated_bpp=total / pixels,
                   psnr_reported_quantized_db=finite(metrics["psnr_quantized"]),
                   psnr_saved_png_db=png_psnr(reference, reconstruction),
                   optimization_seconds=finite(metrics["timing_seconds"]["optimization"]),
                   network_quantization_search_seconds=finite(metrics["timing_seconds"]["network_quantization_search"]),
                   repository_pre_save_total_seconds=finite(metrics["timing_seconds"]["total"]))
        require(math.isclose(row["psnr_reported_quantized_db"], row["psnr_saved_png_db"], abs_tol=1e-4),
                "Independent PNG PSNR disagrees with rounded codec evaluation")
        machine = metrics.get("machine_loss")
        if job["objective"] == "combined":
            require(machine is not None and machine["detector_checksum_before"] == machine["detector_checksum_after"],
                    "Teacher changed")
            require(machine["detector_gradients_none"] is True, "Teacher received gradients")
            require(machine["feature_layers"] == ["0", "1", "2", "3"]
                    and machine["feature_layer_weights"] == [0.25] * 4, "FPN configuration mismatch")
            row.update(feature_L1_decoder_float=finite(machine["distortion_total_decoder_float"]),
                       feature_L1_saved_uint8=finite(machine["distortion_total_uint8"]), teacher_unchanged=True)
        rows.append(row)

    require(not seeds or all(seed == seeds[0] for seed in seeds), "Independent initial RNG mismatch")
    elapsed = sum(finite(row["worker_wall_seconds"]) for row in rows if row["worker_wall_seconds"] is not None)
    initial = read(root / "sequence_initial_status.json")
    final = read(root / "sequence_status.json")
    wall = ((datetime.fromisoformat(final["finished_at"]) - datetime.fromisoformat(initial["started_at"])).total_seconds()
            if "finished_at" in final else None)
    completed = [row["condition"] for row in rows if row["status"] == "complete"]
    result = dict(issue=24, phase="one_image_cost_pilot", source_commit=plan["runtime_commit"],
                  image_count=1, input_protocol="detector_input_v1", rate_definition="entropy estimated total bits / resized nonpadding pixels",
                  actual_bitstream_generated=False, subset_AP_measured=False, dataset_BD_rate_measured=False,
                  attempts=rows, full_conditions_complete=sum(name in completed for name in ARMS),
                  attempted_jobs=sum(row["status"] != "not_started" for row in rows),
                  cumulative_GPU_stage_worker_seconds=elapsed, overall_wall_seconds=wall,
                  within_time_scope=elapsed <= 7200 and (wall is None or wall <= 7200),
                  initial_RNGs_match=True, initial_RNGs_checked=len(seeds),
                  limits=dict(GPU_count=1, GPU_stage_seconds=7200, max_attempts=6, CPU_cores=2,
                              sampled_RSS_stop_GiB=8, additional_artifacts_GiB=6, free_disk_reserve_GiB=10),
                  limitations=["One previously used calibration image; no dataset AP or BD-rate.",
                               "Fixed lambda within a method is not matched rate across budgets or methods.",
                               "Fresh processes share a persistent JAX compilation cache; timing has order effects.",
                               "Optimization timing combines teacher setup, compilation, noise and STE; phase-specific timing is unavailable.",
                               "Repository total precedes final saving; worker wall includes startup, saving and process exit.",
                               "Training trace uses existing ten-step logging for full runs; it is not an every-step trace.",
                               "Memory peaks are sampled at one-second intervals; RSS monitoring is not a cgroup hard limit.",
                               "Initial image-only connection attempt failed before its first optimizer step and was not retried."])
    return result, trace


def write_csv(path, rows):
    keys = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("private_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result, trace = audit(args.private_root)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "pilot.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    write_csv(args.output / "pilot.csv", result["attempts"])
    write_csv(args.output / "optimization_trace.csv", trace)
    print(json.dumps({"full_conditions_complete": result["full_conditions_complete"],
                      "attempted_jobs": result["attempted_jobs"], "within_time_scope": result["within_time_scope"]}))


if __name__ == "__main__":
    main()
