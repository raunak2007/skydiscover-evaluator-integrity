#!/usr/bin/env python3
"""
inject.py - wrap a SkyDiscover containerized evaluator with a dead parameter.

Sits between evaluate.sh and the real evaluator.py. In train mode it blends a
quality-independent component into combined_score, so the search can chase a
signal that means nothing. In test mode it passes the real score through
untouched, which makes the framework's own publish-time evaluation the
held-out check. No changes to evaluator.py, and none to the repo.

Protocol constraints this has to respect:
  - stdout must be EXACTLY one JSON object. A stray print corrupts the result.
  - stderr is captured into artifacts["stderr"] even on success, and artifacts
    can reach the model, so ground truth must not go there either.
  - exit 0 means "evaluation completed", including for bad candidates.
    Non-zero means the evaluator broke and the framework discards stdout.

Ground truth is recoverable without a side channel: the underlying evaluator
prints the true Spearman correlation into artifacts["feedback"], which the
framework persists in each program's checkpoint JSON alongside the perturbed
combined_score. The /tmp log below is best-effort only and does NOT survive
the run, because the framework stops and removes its container on completion.

DEAD_WEIGHT is baked into the image via ENV in the Dockerfile. Do not rely on
setting it in your shell: the framework does not forward ambient environment
into the container, so an unset value silently falls back to the default.
Control and treatment are separate directories, hence separate images:
  text_similarity_control -> ENV DEAD_WEIGHT=0.0
  text_similarity_dead    -> ENV DEAD_WEIGHT=0.3
"""

import hashlib
import json
import os
import subprocess
import sys

REAL_EVALUATOR = os.environ.get("REAL_EVALUATOR", "/benchmark/evaluator.py")
LOG_PATH = os.environ.get("DEAD_LOG", "/tmp/dead_param_log.jsonl")


def dead_component(program_path):
    """
    Deterministic per candidate, uncorrelated with quality.

    Seeded off the program text so the same candidate always gets the same
    value. If it were random per call, the search would see noise rather than
    a stable false gradient, and those are different experiments.
    """
    digest = hashlib.sha256(open(program_path, "rb").read()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: inject.py <program_path> [train|test]\n")
        return 2

    program_path = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "train"
    weight = float(os.environ.get("DEAD_WEIGHT", "0.0"))

    proc = subprocess.run(
        [sys.executable, REAL_EVALUATOR, program_path],
        capture_output=True, text=True,
    )

    # The real evaluator itself broke. Propagate rather than fabricate a
    # score, so this stays distinguishable from a merely bad candidate.
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        return proc.returncode

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as e:
        sys.stderr.write(f"inject.py could not parse evaluator output: {e}\n")
        sys.stderr.write(proc.stdout[:2000])
        return 1

    true_score = float(payload.get("combined_score", 0.0))
    reported = true_score
    dead = None

    if mode == "train" and weight > 0:
        dead = dead_component(program_path)
        reported = round((1.0 - weight) * true_score + weight * dead, 4)
        payload["combined_score"] = reported
        # Keep metrics consistent if the evaluator emitted them.
        if isinstance(payload.get("metrics"), dict) and \
                "combined_score" in payload["metrics"]:
            payload["metrics"]["combined_score"] = reported

    # Ground truth to a file. Not stdout (protocol), not stderr (captured
    # into artifacts, which can reach the model).
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({
                "mode": mode,
                "weight": weight,
                "true_score": true_score,
                "reported_score": reported,
                "dead_component": dead,
                "program_sha": hashlib.sha256(
                    open(program_path, "rb").read()).hexdigest()[:12],
            }) + "\n")
    except OSError:
        pass  # never let logging break an evaluation

    # The one and only thing on stdout.
    sys.stdout.write(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
