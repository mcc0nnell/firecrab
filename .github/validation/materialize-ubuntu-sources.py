#!/usr/bin/env python3
"""Validation-only full Ubuntu source materializer with one shared apt index."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts" / "m2image_source_publication.py"
spec = importlib.util.spec_from_file_location("m2image_source_publication", MODULE)
assert spec and spec.loader
sourcepub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sourcepub)

CODENAMES = {"26.04": "resolute"}
COMPONENTS = "main universe restricted multiverse"


def run(args: list[str], *, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def apt_options(root: Path, sources: Path) -> list[str]:
    lists = root / "lists"
    cache = root / "cache"
    (lists / "partial").mkdir(parents=True, exist_ok=True)
    (cache / "archives" / "partial").mkdir(parents=True, exist_ok=True)
    return [
        "-o", f"Dir::Etc::sourcelist={sources}",
        "-o", "Dir::Etc::sourceparts=-",
        "-o", "APT::Get::List-Cleanup=0",
        "-o", f"Dir::State::lists={lists}",
        "-o", f"Dir::Cache={cache}",
    ]


def materialize(plan_path: Path, output: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schemaVersion") != 1 or plan.get("coveragePolicy") != "all-installed-packages":
        raise ValueError("unsupported source publication plan")
    image = plan.get("image") or {}
    if image.get("distribution") != "ubuntu":
        raise ValueError("validation helper accepts Ubuntu plans only")
    version = str(image.get("version") or "")
    codename = CODENAMES.get(version)
    if not codename:
        raise ValueError(f"no Ubuntu codename mapping for {version!r}")
    units = plan.get("sources")
    if not isinstance(units, list) or not units:
        raise ValueError("source plan has no source units")

    if output.exists():
        shutil.rmtree(output)
    source_root = output / "sources"
    source_root.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="firecrab-full-ubuntu-apt-") as tmpdir:
        apt_root = Path(tmpdir)
        sources = apt_root / "sources.list"
        sources.write_text(
            f"deb-src http://archive.ubuntu.com/ubuntu {codename} {COMPONENTS}\n"
            f"deb-src http://archive.ubuntu.com/ubuntu {codename}-updates {COMPONENTS}\n"
            f"deb-src http://archive.ubuntu.com/ubuntu {codename}-backports {COMPONENTS}\n"
            f"deb-src http://security.ubuntu.com/ubuntu {codename}-security {COMPONENTS}\n",
            encoding="utf-8",
        )
        options = apt_options(apt_root, sources)
        print("ubuntu full source: refreshing source indexes once", flush=True)
        run(["apt-get", *options, "update"])

        for number, unit in enumerate(units, start=1):
            source = unit.get("source") or {}
            if source.get("type") != "ubuntu-source-package":
                raise ValueError(f"unexpected resolver for {unit.get('sourceId')}: {source!r}")
            package = str(source.get("sourcePackage") or "")
            source_version = str(source.get("sourceVersion") or "")
            source_id = str(unit.get("sourceId") or "")
            if not package or not source_version or not source_id:
                raise ValueError(f"incomplete Ubuntu source unit: {unit!r}")
            destination = source_root / source_id
            destination.mkdir()
            print(
                f"ubuntu full source: {number}/{len(units)} "
                f"{package}={source_version} ({source_id})",
                flush=True,
            )
            run(
                [
                    "apt-get", *options, "source", "--download-only",
                    f"{package}={source_version}",
                ],
                cwd=destination,
            )
            files = [path for path in destination.iterdir() if path.is_file()]
            if not files:
                raise ValueError(f"no source files fetched for {package}={source_version}")

    index = sourcepub.source_index(plan, source_root)
    (output / "source-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "source-publication-plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return index


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: materialize-ubuntu-sources.py <plan.json> <output-dir>", file=sys.stderr)
        return 2
    try:
        index = materialize(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ubuntu full source: {exc}", file=sys.stderr)
        return 2
    print(
        "ubuntu full source complete: "
        f"packages={index['packageCount']} sources={index['sourceCount']} "
        f"files={index['fileCount']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
