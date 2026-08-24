#!/usr/bin/env python3
"""Materialize exact source artifacts described by an M2Image source plan.

This deliberately consumes the normalized plan rather than rediscovering package
metadata.  Each source unit lands under sources/<sourceId>/ and is then hashed
by m2image_source_publication.py so a release can bind installed binaries to the
source bytes it actually publishes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "m2image_source_publication", ROOT / "m2image_source_publication.py"
)
assert _spec and _spec.loader
sourcepub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sourcepub)

UBUNTU_CODENAMES = {
    "26.04": "resolute",
}
ROCKY_SOURCE_REPOS = ("BaseOS", "AppStream", "CRB")
SHA512_RE = re.compile(r"^[0-9a-fA-F]{128}$")


def run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=cwd, env=env, check=True)


def capture(args: list[str], *, cwd: Path | None = None) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise ValueError(f"required source-publication tool is missing: {name}")


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


def fetch_rocky(unit: dict, image: dict, destination: Path) -> None:
    require_tool("curl")
    source = unit["source"]
    artifact = str(source["sourceArtifact"])
    version = str(image.get("version") or "")
    if not version:
        raise ValueError("Rocky source plan image is missing version")
    first = artifact[0].lower()
    target = destination / artifact
    attempted = []
    for repo in ROCKY_SOURCE_REPOS:
        url = (
            f"https://download.rockylinux.org/pub/rocky/{version}/{repo}/source/tree/"
            f"Packages/{first}/{artifact}"
        )
        attempted.append(url)
        if fetch_url(url, target):
            (destination / "FETCHED_FROM.txt").write_text(url + "\n", encoding="utf-8")
            return
    raise ValueError(
        f"Rocky source artifact was not found in BaseOS/AppStream/CRB: {artifact}; "
        f"tried {len(attempted)} canonical mirror paths"
    )


def _apt_options(root: Path, sources: Path) -> list[str]:
    lists = root / "lists"
    cache = root / "cache"
    (lists / "partial").mkdir(parents=True, exist_ok=True)
    (cache / "archives" / "partial").mkdir(parents=True, exist_ok=True)
    return [
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        "APT::Get::List-Cleanup=0",
        "-o",
        f"Dir::State::lists={lists}",
        "-o",
        f"Dir::Cache={cache}",
    ]


def fetch_ubuntu(unit: dict, image: dict, destination: Path) -> None:
    require_tool("apt-get")
    source = unit["source"]
    package = str(source["sourcePackage"])
    version = str(source["sourceVersion"])
    image_version = str(image.get("version") or "")
    codename = UBUNTU_CODENAMES.get(image_version)
    if not codename:
        raise ValueError(f"no Ubuntu source codename mapping for image version {image_version!r}")

    with tempfile.TemporaryDirectory(prefix="firecrab-apt-source-") as tmpdir:
        apt_root = Path(tmpdir)
        sources = apt_root / "sources.list"
        components = "main universe restricted multiverse"
        sources.write_text(
            f"deb-src http://archive.ubuntu.com/ubuntu {codename} {components}\n"
            f"deb-src http://archive.ubuntu.com/ubuntu {codename}-updates {components}\n"
            f"deb-src http://archive.ubuntu.com/ubuntu {codename}-backports {components}\n"
            f"deb-src http://security.ubuntu.com/ubuntu {codename}-security {components}\n",
            encoding="utf-8",
        )
        options = _apt_options(apt_root, sources)
        run(["apt-get", *options, "update"])
        run(
            [
                "apt-get",
                *options,
                "source",
                "--download-only",
                f"{package}={version}",
            ],
            cwd=destination,
        )
    files = [path for path in destination.iterdir() if path.is_file()]
    if not files:
        raise ValueError(f"apt-get source published no files for {package}={version}")


def _ensure_aports(cache: Path, commit: str) -> Path:
    require_tool("git")
    cache.parent.mkdir(parents=True, exist_ok=True)
    if not cache.exists():
        run(["git", "init", "--bare", str(cache)])
        run(
            [
                "git",
                "--git-dir",
                str(cache),
                "remote",
                "add",
                "origin",
                "https://gitlab.alpinelinux.org/alpine/aports.git",
            ]
        )
    exists = subprocess.run(
        ["git", "--git-dir", str(cache), "cat-file", "-e", f"{commit}^{{commit}}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0
    if not exists:
        run(
            [
                "git",
                "--git-dir",
                str(cache),
                "fetch",
                "--depth=1",
                "origin",
                commit,
            ]
        )
    return cache


def _aports_recipe_path(repo: Path, commit: str, package: str) -> str:
    names = capture(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", commit]
    ).splitlines()
    suffix = f"/{package}/APKBUILD"
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Alpine source package {package!r} has {len(matches)} APKBUILD matches at {commit}"
        )
    return matches[0].rsplit("/", 1)[0]


def _literal_sha512sums(apkbuild: Path) -> dict[str, str]:
    """Read the literal sha512sums block without executing an APKBUILD.

    Alpine's archived distfiles are accepted only when the exact aports recipe
    carries a literal SHA-512 for that filename.  Dynamic/unsupported checksum
    syntax deliberately yields no fallback rather than weakening verification.
    """

    text = apkbuild.read_text(encoding="utf-8")
    assignment = re.search(r"(?m)^[ \t]*sha512sums=([\"'])", text)
    if assignment is None:
        return {}
    delimiter = assignment.group(1)
    start = assignment.end()
    end = text.find(delimiter, start)
    if end < 0:
        return {}

    checksums: dict[str, str] = {}
    for raw_line in text[start:end].splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split(None, 1)
        if len(fields) != 2 or not SHA512_RE.fullmatch(fields[0]):
            return {}
        filename = fields[1].strip()
        path = Path(filename)
        if not filename or path.is_absolute() or ".." in path.parts:
            return {}
        checksums[filename] = fields[0].lower()
    return checksums


def _alpine_distfiles_branch(image: dict) -> str:
    version = str(image.get("version") or "")
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?", version)
    if match is None:
        raise ValueError(f"cannot derive Alpine distfiles branch from image version {version!r}")
    return f"v{match.group(1)}.{match.group(2)}"


def _recover_alpine_distfiles(recipe_root: Path, distfiles: Path, image: dict) -> list[str]:
    """Recover missing, checksum-bound source files from Alpine's archive."""

    checksums = _literal_sha512sums(recipe_root / "APKBUILD")
    if not checksums:
        return []

    branch = _alpine_distfiles_branch(image)
    recovered: list[str] = []
    for filename, expected in sorted(checksums.items()):
        # Local patches/configs exported with the exact aports recipe are not
        # remote distfiles. Existing downloads also need no recovery.
        if (recipe_root / filename).is_file() or (distfiles / filename).is_file():
            continue
        target = distfiles / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = quote(filename, safe="/._+-")
        url = f"https://distfiles.alpinelinux.org/distfiles/{branch}/{encoded}"
        if not fetch_url(url, target):
            continue
        digest = hashlib.sha512(target.read_bytes()).hexdigest()
        if digest != expected:
            target.unlink(missing_ok=True)
            raise ValueError(
                f"Alpine distfile checksum mismatch for {filename}: "
                f"expected {expected}, got {digest}"
            )
        recovered.append(url)
    return recovered


def _alpine_abuild_fetch(work_root: Path, distfiles: Path) -> int:
    command = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{work_root.resolve()}:/src",
        "-v",
        f"{distfiles.resolve()}:/dist",
        "alpine:3.24",
        "sh",
        "-lc",
        (
            "set -eu; "
            "trap 'chmod -R a+rwX /src /dist 2>/dev/null || true' EXIT; "
            "apk add --no-cache alpine-sdk >/dev/null; "
            "adduser -D builder; "
            "chmod -R a+rwX /src /dist; "
            "su builder -c 'cd /src && SRCDEST=/dist abuild fetch'"
        ),
    ]
    return subprocess.run(command, check=False).returncode


def fetch_alpine(unit: dict, image: dict, destination: Path, cache_dir: Path) -> None:
    require_tool("git")
    require_tool("tar")
    require_tool("docker")
    require_tool("curl")
    source = unit["source"]
    package = str(source["sourcePackage"])
    commit = str(source["repositoryCommit"])
    aports = _ensure_aports(cache_dir / "aports.git", commit)
    recipe_path = _aports_recipe_path(aports, commit, package)
    recipe_root = destination / "recipe"
    distfiles = destination / "distfiles"
    recipe_root.mkdir()
    distfiles.mkdir()

    with tempfile.TemporaryDirectory(prefix="firecrab-aports-") as tmpdir:
        extracted = Path(tmpdir)
        archive = subprocess.Popen(
            ["git", "--git-dir", str(aports), "archive", commit, recipe_path],
            stdout=subprocess.PIPE,
        )
        assert archive.stdout is not None
        untar = subprocess.run(["tar", "-x", "-C", str(extracted)], stdin=archive.stdout)
        archive.stdout.close()
        archive_rc = archive.wait()
        if archive_rc or untar.returncode:
            raise subprocess.CalledProcessError(archive_rc or untar.returncode, "git archive | tar")
        exported = extracted / recipe_path
        if not (exported / "APKBUILD").is_file():
            raise ValueError(f"Alpine APKBUILD export missing for {package}@{commit}")
        shutil.copytree(exported, recipe_root, dirs_exist_ok=True)

    # abuild creates a local src/ work directory while fetching. Preserve the
    # exact git-archive recipe as immutable evidence and give abuild a disposable
    # writable copy so generated work files never contaminate the source bundle.
    with tempfile.TemporaryDirectory(prefix="firecrab-abuild-fetch-") as tmpdir:
        work_root = Path(tmpdir) / "recipe"
        shutil.copytree(recipe_root, work_root)
        first_rc = _alpine_abuild_fetch(work_root, distfiles)
        recovered: list[str] = []
        if first_rc:
            # Historical upstream URLs can disappear after Alpine has built the
            # binary. Alpine's distfiles archive is the fail-closed recovery
            # source: only exact files whose SHA-512 is frozen in this exact
            # APKBUILD are admitted, then abuild verifies the completed set too.
            recovered = _recover_alpine_distfiles(recipe_root, distfiles, image)
            if not recovered:
                raise subprocess.CalledProcessError(first_rc, "abuild fetch")
            retry_rc = _alpine_abuild_fetch(work_root, distfiles)
            if retry_rc:
                raise subprocess.CalledProcessError(retry_rc, "abuild fetch after distfiles recovery")

    (destination / "APORTS_COMMIT.txt").write_text(commit + "\n", encoding="utf-8")
    if recovered:
        (destination / "FETCHED_FROM.txt").write_text(
            "\n".join(recovered) + "\n", encoding="utf-8"
        )


def materialize(plan: dict, output_dir: Path, cache_dir: Path) -> dict:
    if plan.get("schemaVersion") != 1 or plan.get("coveragePolicy") != "all-installed-packages":
        raise ValueError("unsupported source publication plan")
    image = plan.get("image")
    sources = plan.get("sources")
    if not isinstance(image, dict) or not isinstance(sources, list):
        raise ValueError("source publication plan is malformed")
    distribution = str(image.get("distribution") or "").lower()

    source_root = output_dir / "sources"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    source_root.mkdir(parents=True)

    for index, unit in enumerate(sources, start=1):
        source_id = str(unit.get("sourceId") or "")
        destination = source_root / source_id
        destination.mkdir()
        print(
            f"m2image source fetch: {distribution} {index}/{len(sources)} "
            f"{source_id}",
            flush=True,
        )
        if distribution == "rocky":
            fetch_rocky(unit, image, destination)
        elif distribution == "ubuntu":
            fetch_ubuntu(unit, image, destination)
        elif distribution == "alpine":
            fetch_alpine(unit, image, destination, cache_dir)
        else:
            raise ValueError(f"unsupported source materializer distribution: {distribution!r}")

    index = sourcepub.source_index(plan, source_root)
    (output_dir / "source-index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "source-publication-plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return index


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("M2IMAGE_SOURCE_CACHE", ".cache/m2image-sources")),
    )
    args = parser.parse_args(argv)
    try:
        with args.plan.open(encoding="utf-8") as stream:
            plan = json.load(stream)
        index = materialize(plan, args.output_dir, args.cache_dir)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"m2image source fetch: {exc}", file=sys.stderr)
        return 2
    print(
        "m2image source fetch complete: "
        f"packages={index['packageCount']} sources={index['sourceCount']} "
        f"files={index['fileCount']} -> {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
