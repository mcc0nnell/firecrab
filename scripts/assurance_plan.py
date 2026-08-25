#!/usr/bin/env python3
"""Expand FireCrab's versioned assurance profile into executable job descriptors."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

SAFE_VALUE = re.compile(r"^[A-Za-z0-9._+-]+$")


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def safe(value: str, field: str) -> str:
    if not SAFE_VALUE.fullmatch(value):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


def host_subject(target: str) -> tuple[str, str]:
    mapping = {
        "x86_64-unknown-linux-gnu": ("x86_64", "gnu"),
        "x86_64-unknown-linux-musl": ("x86_64", "musl"),
        "aarch64-unknown-linux-gnu": ("aarch64", "gnu"),
        "aarch64-unknown-linux-musl": ("aarch64", "musl"),
    }
    try:
        return mapping[target]
    except KeyError as exc:
        raise ValueError(f"unsupported host assurance target: {target}") from exc


def build_plan(profile: dict[str, Any], manifest: dict[str, Any], sha: str) -> dict[str, Any]:
    if profile.get("schemaVersion") != 1 or profile.get("profile") != "firecrab-release-assurance-v1":
        raise ValueError("unsupported assurance profile")
    if manifest.get("schemaVersion") != 1:
        raise ValueError("unsupported M2Image manifest")
    if not re.fullmatch(r"[0-9a-fA-F]{40}([0-9a-fA-F]{24})?", sha):
        raise ValueError("sha must be a 40- or 64-character hexadecimal object id")

    images = manifest.get("images")
    architectures = manifest.get("architectures")
    if not isinstance(images, list) or not images:
        raise ValueError("M2Image manifest has no images")
    if not isinstance(architectures, list) or not architectures:
        raise ValueError("M2Image manifest has no architectures")

    host_stage = next(
        (
            item
            for item in profile.get("stages", [])
            if isinstance(item, dict) and item.get("id") == "host-release-assurance"
        ),
        None,
    )
    targets = ((host_stage or {}).get("matrix") or {}).get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("assurance profile has no host targets")

    jobs: list[dict[str, Any]] = [
        {
            "id": "release-compliance-preflight",
            "stage": "release-compliance-preflight",
            "runnerClass": "sandbox",
            "command": "bash scripts/gitflare-release-compliance.sh",
            "env": {"GITFLARE_EXPECTED_SHA": sha},
            "evidence": "dist/gitflare-receipts/verdict.json",
            "dependsOn": [],
        }
    ]
    native_ids: list[str] = []

    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("alias"), str):
            raise ValueError("M2Image manifest contains an invalid image entry")
        alias = safe(image["alias"], "image alias")
        for raw_arch in architectures:
            if raw_arch not in {"x86_64", "aarch64"}:
                raise ValueError(f"unsupported M2Image architecture: {raw_arch!r}")
            arch = raw_arch
            job_id = f"m2image-source:{alias}:{arch}"
            native_ids.append(job_id)
            jobs.append(
                {
                    "id": job_id,
                    "stage": "m2image-source-assurance",
                    "runnerClass": "native-root",
                    "constraints": {
                        "architecture": arch,
                        "root": True,
                        "network": True,
                        "disposableWorkspace": True,
                    },
                    "command": (
                        f"bash scripts/gitflare-m2image-assurance.sh "
                        f"--alias {alias} --arch {arch}"
                    ),
                    "env": {"GITFLARE_EXPECTED_SHA": sha},
                    "evidence": f"dist/assurance/m2images/{alias}/{arch}/result.json",
                    "dependsOn": ["release-compliance-preflight"],
                }
            )

    for raw_target in targets:
        if not isinstance(raw_target, str):
            raise ValueError("host target must be a string")
        target = safe(raw_target, "host target")
        arch, libc = host_subject(target)
        job_id = f"host-release:{target}"
        native_ids.append(job_id)
        jobs.append(
            {
                "id": job_id,
                "stage": "host-release-assurance",
                "runnerClass": "native",
                "constraints": {
                    "architecture": arch,
                    "libc": libc,
                    "muslTools": libc == "musl",
                    "network": True,
                    "disposableWorkspace": True,
                },
                "command": f"bash scripts/gitflare-host-assurance.sh --target {target}",
                "env": {"GITFLARE_EXPECTED_SHA": sha},
                "evidence": f"dist/assurance/host/{target}/result.json",
                "dependsOn": ["release-compliance-preflight"],
            }
        )

    jobs.append(
        {
            "id": "aggregate",
            "stage": "aggregate",
            "runnerClass": "evidence",
            "command": (
                "python3 scripts/assemble_assurance.py --root dist/assurance "
                "--preflight dist/gitflare-receipts/verdict.json"
            ),
            "env": {"GITFLARE_EXPECTED_SHA": sha},
            "evidence": "dist/assurance/verdict.json",
            "dependsOn": native_ids,
        }
    )

    return {
        "schemaVersion": 1,
        "profile": profile["profile"],
        "sha": sha,
        "jobCount": len(jobs),
        "nativeJobCount": len(native_ids),
        "jobs": jobs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=Path("packaging/assurance-profile.json"))
    parser.add_argument("--manifest", type=Path, default=Path("packaging/m2images.json"))
    parser.add_argument("--sha", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    profile_path = args.profile if args.profile.is_absolute() else repo_root / args.profile
    manifest_path = args.manifest if args.manifest.is_absolute() else repo_root / args.manifest
    sha = args.sha or git_head(repo_root)
    plan = build_plan(read_json(profile_path), read_json(manifest_path), sha)
    rendered = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        output = args.output if args.output.is_absolute() else repo_root / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
