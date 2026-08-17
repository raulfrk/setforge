#!/usr/bin/env python3
"""Benchmark repeated SetForge CLI launches in one warm E2E container.

Run from the repository root, for example::

    uv run python scripts/benchmark_docker_cli.py --target smoke --repeats 20

The content-addressed image is built or reused through the same helper as the
Docker test suite. Timings include ``docker exec`` because that is the real E2E
test boundary; image preparation and container startup are excluded.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import subprocess
import time
import uuid
from collections.abc import Sequence

from tests.docker.container_runtime import container_run_argv
from tests.docker.image import DEFAULT_IMAGE_TARGET, IMAGE_TARGETS, ensure_docker_image


class DockerBenchmarkCleanupError(RuntimeError):
    """Raised when a successful benchmark cannot reclaim its container."""


def _cleanup(name: str, *, strict: bool) -> None:
    """Remove an owned container, preserving an active primary error if asked."""
    try:
        result = subprocess.run(
            ["docker", "rm", "-f", name],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        if strict:
            raise
        return
    if result.returncode != 0 and strict:
        raise DockerBenchmarkCleanupError(
            f"failed to remove benchmark container {name}: {result.stderr}"
        )


def _summary(samples: Sequence[float]) -> dict[str, float]:
    """Return stable descriptive statistics for one timing sample."""
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    return {
        "median_s": statistics.median(samples),
        "mean_s": statistics.mean(samples),
        "stdev_s": stdev,
        "mean_95ci_half_width_s": 1.96 * stdev / math.sqrt(len(samples)),
        "min_s": min(samples),
        "max_s": max(samples),
    }


def _measure(image: str, *, repeats: int, warmups: int) -> list[float]:
    """Measure warm CLI launches and always reclaim the owned container."""
    name = f"setforge-e2e-benchmark-{uuid.uuid4().hex[:10]}"
    samples: list[float] = []
    try:
        launched = subprocess.run(
            container_run_argv(
                name=name,
                image=image,
                env={"SETFORGE_NO_WELCOME": "1"},
                cmd=None,
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        cid = launched.stdout.strip()
        for iteration in range(warmups + repeats):
            started = time.perf_counter()
            subprocess.run(
                ["docker", "exec", cid, "uv", "run", "setforge", "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            elapsed = time.perf_counter() - started
            if iteration >= warmups:
                samples.append(elapsed)
    except BaseException:
        # The name is known before launch. If the client times out after the
        # daemon creates the container but before returning its CID, cleanup
        # still owns the right target and cannot mask the original failure.
        _cleanup(name, strict=False)
        raise
    _cleanup(name, strict=True)
    return samples


def main() -> None:
    """Build/reuse the selected image and print warm-launch timings as JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", choices=sorted(IMAGE_TARGETS), default=DEFAULT_IMAGE_TARGET
    )
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")
    if args.warmups < 0:
        parser.error("--warmups must be non-negative")

    image = ensure_docker_image(args.target)
    if image is None:
        raise SystemExit("docker binary not found")
    samples = _measure(image, repeats=args.repeats, warmups=args.warmups)
    print(
        json.dumps(
            {
                "image": image,
                "repeats": args.repeats,
                "warmups": args.warmups,
                "samples_s": samples,
                **_summary(samples),
            },
            indent=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover - exercised as a script boundary
    main()
