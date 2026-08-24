#!/usr/bin/env python3
"""Validation-only full Rocky source materializer from the frozen source plan."""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts" / "m2image_source_publication.py"
spec = importlib.util.spec_from_file_location("m2image_source_publication", MODULE)
assert spec and spec.loader
sourcepub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sourcepub)

REPOSITORIES = ("BaseOS", "AppStream", "CRB")
WORKERS = 8


def fetch_url(url: str, output: Path) -> bool:
    completed = subprocess.run(
        [
            "curl",
            "--fail",
            "--location",
            "--silent",
            "--show-error",
            "--retry",
            "2",
            "--retry-all-errors",
            "--connect-timeout",
            "10",
            "--max-time",
            "180",
            "--output",
            str(output),
            url,
        ],
        check=False,
    )
    if completed.returncode == 0 and output.is_file() and output.stat().st_size:
        return True
    output.unlink(missing_ok=True)
    return False


def fetch_unit(unit: dict, version: str, source_root: Path) -> tuple[str, str, int]:
    source_id = str(unit.get("sourceId") or "")
    source = unit.get("source") or {}
    if source.get("type") != "rocky-source-rpm":
        raise ValueError(f"unexpected Rocky source resolver for {source_id}: {source!r}")
    artifact = str(source.get("sourceArtifact") or "")
    source_version = str(source.get("sourceVersion") or "")
    if not source_id or not artifact or not source_version:
        raise ValueError(f"incomplete Rocky source unit: {unit!r}")
    if not artifact.endswith((".src.rpm", ".nosrc.rpm")):
        raise ValueError(f"Rocky source artifact is not an SRPM: {artifact!r}")

    destination = source_root / source_id
    destination.mkdir()
    target = destination / artifact
    first = artifact[0].lower()
    attempted: list[str] = []
    for repository in REPOSITORIES:
        url = (
            f"https://download.rockylinux.org/pub/rocky/{version}/{repository}/"
            f"source/tree/Packages/{first}/{artifact}"
        )
        attempted.append(url)
        if fetch_url(url, target):
            (destination / "FETCHED_FROM.txt").write_text(url + "\n", encoding="utf-8")
            return source_id, artifact, target.stat().st_size

    raise ValueError(
        f"{artifact}: exact Rocky source not found in BaseOS/AppStream/CRB; "
        f"tried {len(attempted)} canonical paths"
    )


def materialize(plan_path: Path, output: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schemaVersion") != 1 or plan.get("coveragePolicy") != "all-installed-packages":
        raise ValueError("unsupported source publication plan")
    image = plan.get("image") or {}
    if image.get("distribution") != "rocky":
        raise ValueError("validation helper accepts Rocky plans only")
    version = str(image.get("version") or "")
    if not version:
        raise ValueError("Rocky source plan image is missing version")
    units = plan.get("sources")
    if not isinstance(units, list) or not units:
        raise ValueError("Rocky source plan has no source units")

    if output.exists():
        shutil.rmtree(output)
    source_root = output / "sources"
    source_root.mkdir(parents=True)

    print(
        f"rocky full source: packages={plan['packageCount']} "
        f"sources={len(units)} workers={WORKERS}",
        flush=True,
    )
    completed = 0
    total_bytes = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        future_to_unit = {
            pool.submit(fetch_unit, unit, version, source_root): unit for unit in units
        }
        for future in concurrent.futures.as_completed(future_to_unit):
            source_id, artifact, size = future.result()
            completed += 1
            total_bytes += size
            print(
                f"rocky full source: {completed}/{len(units)} {artifact} "
                f"bytes={size} source={source_id}",
                flush=True,
            )

    index = sourcepub.source_index(plan, source_root)
    (output / "source-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "source-publication-plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        "rocky full source complete: "
        f"packages={index['packageCount']} sources={index['sourceCount']} "
        f"non_source={index['nonSourcePackageCount']} files={index['fileCount']} "
        f"srpm_bytes={total_bytes}",
        flush=True,
    )
    return index


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: materialize-rocky-sources.py <plan.json> <output-dir>", file=sys.stderr)
        return 2
    try:
        materialize(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"rocky full source: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
