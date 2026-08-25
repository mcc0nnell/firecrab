#!/usr/bin/env python3
"""Assemble FireCrab release-assurance evidence into one fail-closed verdict."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"
VALID_VERDICTS = {PASS, FAIL, BLOCKED}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"missing evidence: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"evidence is not a JSON object: {path}")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def git_head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def component(
    *,
    stage: str,
    key: str,
    path: Path,
    expected_sha: str,
    expected_subject: dict[str, str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "stage": stage,
        "key": key,
        "path": str(path),
        "verdict": BLOCKED,
        "reason": "required evidence is missing",
    }
    try:
        doc = read_json(path)
    except ValueError as exc:
        record["reason"] = str(exc)
        return record

    verdict = doc.get("verdict")
    if verdict not in VALID_VERDICTS:
        record.update(verdict=FAIL, reason=f"invalid component verdict: {verdict!r}")
        return record

    subject = doc.get("subject")
    if isinstance(subject, dict):
        subject_sha = subject.get("sha")
    else:
        subject_sha = doc.get("sha")
    if subject_sha != expected_sha:
        record.update(
            verdict=FAIL,
            reason=f"subject SHA mismatch: expected {expected_sha}, got {subject_sha!r}",
        )
        return record

    if expected_subject:
        if not isinstance(subject, dict):
            record.update(verdict=FAIL, reason="component subject is missing")
            return record
        for field, expected in expected_subject.items():
            if subject.get(field) != expected:
                record.update(
                    verdict=FAIL,
                    reason=(
                        f"subject {field} mismatch: expected {expected!r}, "
                        f"got {subject.get(field)!r}"
                    ),
                )
                return record

    if verdict == PASS:
        if stage == "m2image-source-assurance":
            for field in ("binaryArtifact", "sourceArtifact"):
                artifact = doc.get(field)
                if (
                    not isinstance(artifact, dict)
                    or not isinstance(artifact.get("bytes"), int)
                    or artifact["bytes"] <= 0
                    or not isinstance(artifact.get("sha256"), str)
                    or len(artifact["sha256"]) != 64
                ):
                    record.update(
                        verdict=FAIL,
                        reason=f"PASS component lacks valid {field} evidence",
                    )
                    return record
        if stage == "host-release-assurance":
            artifact = doc.get("artifact")
            if (
                not isinstance(artifact, dict)
                or not isinstance(artifact.get("bytes"), int)
                or artifact["bytes"] <= 0
                or not isinstance(artifact.get("sha256"), str)
                or len(artifact["sha256"]) != 64
            ):
                record.update(
                    verdict=FAIL,
                    reason="PASS host component lacks valid artifact evidence",
                )
                return record

    record.update(
        verdict=verdict,
        reason=str(doc.get("reason") or "component supplied no reason"),
        evidenceSha256=sha256(path),
    )
    return record


def overall_verdict(components: list[dict[str, Any]]) -> str:
    verdicts = {item["verdict"] for item in components}
    if FAIL in verdicts:
        return FAIL
    if BLOCKED in verdicts:
        return BLOCKED
    return PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("dist/assurance"))
    parser.add_argument(
        "--profile", type=Path, default=Path("packaging/assurance-profile.json")
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("packaging/m2images.json")
    )
    parser.add_argument(
        "--preflight", type=Path, default=Path("dist/gitflare-receipts/verdict.json")
    )
    parser.add_argument("--sha", default=None)
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parent.parent
    profile_path = (repo_root / args.profile).resolve() if not args.profile.is_absolute() else args.profile
    manifest_path = (repo_root / args.manifest).resolve() if not args.manifest.is_absolute() else args.manifest
    evidence_root = (repo_root / args.root).resolve() if not args.root.is_absolute() else args.root
    preflight_path = (repo_root / args.preflight).resolve() if not args.preflight.is_absolute() else args.preflight
    expected_sha = args.sha or git_head(repo_root)

    profile = read_json(profile_path)
    manifest = read_json(manifest_path)
    if profile.get("schemaVersion") != 1 or profile.get("profile") != "firecrab-release-assurance-v1":
        raise SystemExit("unsupported assurance profile")
    if manifest.get("schemaVersion") != 1:
        raise SystemExit("unsupported M2Image manifest")

    aliases = [image.get("alias") for image in manifest.get("images", []) if isinstance(image, dict)]
    architectures = manifest.get("architectures")
    if not aliases or any(not isinstance(alias, str) or not alias for alias in aliases):
        raise SystemExit("M2Image manifest has invalid aliases")
    if not isinstance(architectures, list) or not architectures:
        raise SystemExit("M2Image manifest has no architectures")
    if any(arch not in {"x86_64", "aarch64"} for arch in architectures):
        raise SystemExit("M2Image manifest contains unsupported assurance architecture")

    host_stage = next(
        (
            stage
            for stage in profile.get("stages", [])
            if isinstance(stage, dict) and stage.get("id") == "host-release-assurance"
        ),
        None,
    )
    targets = ((host_stage or {}).get("matrix") or {}).get("targets")
    if not isinstance(targets, list) or not targets or any(not isinstance(item, str) for item in targets):
        raise SystemExit("assurance profile has no host target matrix")

    components: list[dict[str, Any]] = []
    components.append(
        component(
            stage="release-compliance-preflight",
            key="preflight",
            path=preflight_path,
            expected_sha=expected_sha,
        )
    )

    for alias in aliases:
        for arch in architectures:
            path = evidence_root / "m2images" / alias / arch / "result.json"
            components.append(
                component(
                    stage="m2image-source-assurance",
                    key=f"{alias}/{arch}",
                    path=path,
                    expected_sha=expected_sha,
                    expected_subject={"alias": alias, "architecture": arch},
                )
            )

    for target in targets:
        path = evidence_root / "host" / target / "result.json"
        components.append(
            component(
                stage="host-release-assurance",
                key=target,
                path=path,
                expected_sha=expected_sha,
                expected_subject={"target": target},
            )
        )

    verdict = overall_verdict(components)
    counts = {name: sum(1 for item in components if item["verdict"] == name) for name in (PASS, FAIL, BLOCKED)}

    evidence_root.mkdir(parents=True, exist_ok=True)
    subject = {
        "schemaVersion": 1,
        "profile": profile["profile"],
        "sha": expected_sha,
        "profileSha256": sha256(profile_path),
        "m2imageManifestSha256": sha256(manifest_path),
        "expected": {
            "preflight": 1,
            "m2imageCells": len(aliases) * len(architectures),
            "hostCells": len(targets),
            "totalComponents": 1 + len(aliases) * len(architectures) + len(targets),
        },
    }
    (evidence_root / "subject.json").write_text(
        json.dumps(subject, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verdict_doc = {
        "schemaVersion": 1,
        "profile": profile["profile"],
        "sha": expected_sha,
        "verdict": verdict,
        "counts": counts,
        "components": components,
    }
    (evidence_root / "verdict.json").write_text(
        json.dumps(verdict_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    checksum_lines: list[str] = []
    for path in sorted(evidence_root.rglob("*")):
        if not path.is_file() or path.name == "SHA256SUMS":
            continue
        checksum_lines.append(f"{sha256(path)}  {path.relative_to(evidence_root).as_posix()}")
    (evidence_root / "SHA256SUMS").write_text(
        "\n".join(checksum_lines) + "\n", encoding="utf-8"
    )

    print(
        f"assurance: {verdict} sha={expected_sha} "
        f"pass={counts[PASS]} fail={counts[FAIL]} blocked={counts[BLOCKED]}"
    )
    if verdict == PASS:
        return 0
    if verdict == BLOCKED:
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
