#!/usr/bin/env bash
# Native clean-lab proof for one M2Image + corresponding-source cell.
set -euo pipefail

unset CDPATH
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"

alias_name=''
architecture=''
manifest=${M2IMAGE_MANIFEST:-$ROOT/packaging/m2images.json}
expected_sha=${GITFLARE_EXPECTED_SHA:-}

usage() {
  cat <<'EOF'
Usage: scripts/gitflare-m2image-assurance.sh --alias <alias> --arch <x86_64|aarch64>

Builds one manifest-pinned M2Image on a host-native architecture, materializes
its exact corresponding sources, packages binary/source siblings, recomputes
hashes independently, and emits dist/assurance/m2images/<alias>/<arch>/result.json.

Exit codes: 0 PASS, 1 FAIL, 3 BLOCKED (runner lacks required native/root capability).
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --alias) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; alias_name=$2; shift 2 ;;
    --alias=*) alias_name=${1#--alias=}; shift ;;
    --arch) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; architecture=$2; shift 2 ;;
    --arch=*) architecture=${1#--arch=}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -n "$alias_name" ] || { usage >&2; exit 2; }
case "$architecture" in x86_64|aarch64) ;; *) usage >&2; exit 2 ;; esac

result_dir="$ROOT/dist/assurance/m2images/$alias_name/$architecture"
result_json="$result_dir/result.json"
mkdir -p "$result_dir"

actual_sha=$(git rev-parse HEAD)
status=FAIL
reason='execution failed'

write_result() {
  rc=$1
  STATUS="$status" REASON="$reason" RESULT_JSON="$result_json" ROOT="$ROOT" \
  ALIAS_NAME="$alias_name" ARCHITECTURE="$architecture" ACTUAL_SHA="$actual_sha" \
  python3 - <<'PY'
import hashlib
import json
import os
import platform
from pathlib import Path

root = Path(os.environ["ROOT"])
alias = os.environ["ALIAS_NAME"]
arch = os.environ["ARCHITECTURE"]
result = Path(os.environ["RESULT_JSON"])
dist = root / "dist" / "m2images" / arch
binary = dist / f"{alias}.tar.zst"
source = dist / f"{alias}.sources.tar.zst"
compliance = root / "images" / "compliance" / f"{alias}-{arch}"

def sha256(path: Path):
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return {"path": str(path.relative_to(root)), "bytes": size, "sha256": digest.hexdigest()}

def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

sbom = read_json(compliance / "sbom.spdx.json") or {}
index = None
if source.is_file():
    # source-index.json is inside the archive, so package/source counts are taken
    # from the frozen compliance plan that was independently checked before pack.
    index = read_json(compliance / "source-publication-plan.json") or {}

payload = {
    "schemaVersion": 1,
    "profile": "firecrab-release-assurance-v1",
    "stage": "m2image-source-assurance",
    "subject": {"sha": os.environ["ACTUAL_SHA"], "alias": alias, "architecture": arch},
    "runner": {"platform": platform.platform(), "architecture": platform.machine()},
    "verdict": os.environ["STATUS"],
    "reason": os.environ["REASON"],
    "binaryArtifact": sha256(binary),
    "sourceArtifact": sha256(source),
    "packageCount": len(sbom.get("packages") or []) - (1 if sbom.get("packages") else 0),
    "sourceUnitCount": index.get("sourceCount") if isinstance(index, dict) else None,
    "sourceBackedPackageCount": index.get("sourceBackedPackageCount") if isinstance(index, dict) else None,
    "nonSourcePackageCount": index.get("nonSourcePackageCount") if isinstance(index, dict) else None,
}
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  return "$rc"
}

finish() {
  rc=$?
  trap - EXIT
  write_result "$rc" || true
  exit "$rc"
}
trap finish EXIT

block() {
  status=BLOCKED
  reason=$1
  printf 'm2image assurance: BLOCKED: %s\n' "$reason" >&2
  exit 3
}

fail() {
  status=FAIL
  reason=$1
  printf 'm2image assurance: FAIL: %s\n' "$reason" >&2
  exit 1
}

normalize_arch() {
  case "$1" in
    x86_64|amd64) printf '%s\n' x86_64 ;;
    aarch64|arm64) printf '%s\n' aarch64 ;;
    *) printf '%s\n' unknown ;;
  esac
}

for command in git python3 curl tar zstd sha256sum chroot mount umount mkfs.ext4 debugfs; do
  command -v "$command" >/dev/null 2>&1 || block "required native-lab command not found: $command"
done

host_arch=$(normalize_arch "$(uname -m)")
[ "$host_arch" = "$architecture" ] || block "requires native $architecture runner; current host is $host_arch"

if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || block 'native M2Image build requires root or passwordless sudo'
  sudo -n true >/dev/null 2>&1 || block 'native M2Image build requires passwordless sudo on the disposable lab runner'
fi

if [ -n "$expected_sha" ] && [ "${actual_sha,,}" != "${expected_sha,,}" ]; then
  fail "checkout SHA mismatch: expected $expected_sha, got $actual_sha"
fi

python3 "$ROOT/scripts/m2image-manifest.py" --manifest "$manifest" validate >/dev/null
python3 "$ROOT/scripts/m2image-manifest.py" --manifest "$manifest" registry-key "$alias_name" "$architecture" >/dev/null \
  || fail "alias/architecture is not manifest-backed: $alias_name/$architecture"

# This stage is intentionally destructive only inside a disposable checkout.
# Refuse to inherit generated state; cached source/build output is evidence poison.
if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  fail 'source checkout is not clean before native assurance build'
fi
rm -rf -- "$ROOT/build" "$ROOT/images/rootfs" "$ROOT/images/kernel" \
  "$ROOT/images/compliance" "$ROOT/dist/m2images"
mkdir -p "$ROOT/images/rootfs" "$ROOT/images/kernel" "$ROOT/images/compliance"

printf '== native M2Image build: %s/%s ==\n' "$alias_name" "$architecture"
M2IMAGE_MANIFEST="$manifest" \
  bash "$ROOT/scripts/build-m2images.sh" --alias "$alias_name" --arch "$architecture" --no-package

printf '== binary package ==\n'
M2IMAGE_MANIFEST="$manifest" \
  bash "$ROOT/scripts/package-m2images.sh" --alias "$alias_name" --arch "$architecture"

printf '== exact corresponding-source materialization ==\n'
M2IMAGE_MANIFEST="$manifest" \
  bash "$ROOT/scripts/package-m2image-sources.sh" --alias "$alias_name" --arch "$architecture"

out_dir="$ROOT/dist/m2images/$architecture"
binary="$out_dir/$alias_name.tar.zst"
source="$out_dir/$alias_name.sources.tar.zst"
[ -s "$binary" ] || fail "binary archive missing: $binary"
[ -s "$source" ] || fail "source archive missing: $source"

(
  cd "$out_dir"
  sha256sum -c SHA256SUMS
)
zstd -t "$binary" >/dev/null
zstd -t "$source" >/dev/null

binary_members=$(mktemp)
source_members=$(mktemp)
trap 'rm -f -- "$binary_members" "$source_members"; finish' EXIT
zstd -dc "$binary" | tar -tf - >"$binary_members"
zstd -dc "$source" | tar -tf - >"$source_members"
for required in compliance/sbom.spdx.json compliance/source-map.json compliance/source-publication-plan.json; do
  grep -qx "$required" "$binary_members" || fail "binary archive missing $required"
done
for required in source-publication-plan.json source-index.json; do
  grep -qx "$required" "$source_members" || fail "source archive missing $required"
done
grep -q '^sources/' "$source_members" || fail 'source archive contains no sources/'

# Independent plan coverage check: every installed package must be represented by
# a source unit or an explicit narrowly-scoped non-source disposition.
python3 - "$ROOT/images/compliance/$alias_name-$architecture/source-publication-plan.json" <<'PY'
import json
import sys
from pathlib import Path
plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
packages = plan.get("packageCount")
covered = (plan.get("sourceBackedPackageCount") or 0) + (plan.get("nonSourcePackageCount") or 0)
if not isinstance(packages, int) or packages <= 0:
    raise SystemExit("invalid packageCount in source publication plan")
if covered != packages:
    raise SystemExit(f"source coverage mismatch: covered={covered} packages={packages}")
if not isinstance(plan.get("sourceCount"), int) or plan["sourceCount"] <= 0:
    raise SystemExit("source publication plan has no source units")
PY

status=PASS
reason='native M2Image, compliance bundle, corresponding-source archive, and independent hashes verified'
printf 'm2image assurance: PASS %s/%s\n' "$alias_name" "$architecture"
