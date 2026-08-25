#!/usr/bin/env bash
# Native clean-lab proof for one FireCrab host release target.
set -euo pipefail

unset CDPATH
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$ROOT"

target=''
expected_sha=${GITFLARE_EXPECTED_SHA:-}

usage() {
  cat <<'EOF'
Usage: scripts/gitflare-host-assurance.sh --target <rust-target>

Supported targets:
  x86_64-unknown-linux-gnu
  x86_64-unknown-linux-musl
  aarch64-unknown-linux-gnu
  aarch64-unknown-linux-musl

Runs the clean release-compliance preflight, builds binaries and dashboard from
fresh dependency state on a native architecture, packages the host archive, and
independently checks architecture, required members, attribution and SHA-256.

Exit codes: 0 PASS, 1 FAIL, 3 BLOCKED.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; target=$2; shift 2 ;;
    --target=*) target=${1#--target=}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$target" in
  x86_64-unknown-linux-gnu) arch=x86_64; libc=gnu ;;
  x86_64-unknown-linux-musl) arch=x86_64; libc=musl ;;
  aarch64-unknown-linux-gnu) arch=aarch64; libc=gnu ;;
  aarch64-unknown-linux-musl) arch=aarch64; libc=musl ;;
  *) usage >&2; exit 2 ;;
esac

result_dir="$ROOT/dist/assurance/host/$target"
result_json="$result_dir/result.json"
artifact="$result_dir/firecrab-host-$arch-$libc.tar.gz"
actual_sha=$(git rev-parse HEAD)
status=FAIL
reason='execution failed'
work=''
unpacked=''

write_result() {
  rc=$1
  STATUS="$status" REASON="$reason" RESULT_JSON="$result_json" ROOT="$ROOT" \
  TARGET="$target" ARCH="$arch" LIBC="$libc" ACTUAL_SHA="$actual_sha" ARTIFACT="$artifact" \
  python3 - <<'PY'
import hashlib
import json
import os
import platform
from pathlib import Path

root = Path(os.environ["ROOT"])
artifact = Path(os.environ["ARTIFACT"])
result = Path(os.environ["RESULT_JSON"])

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

payload = {
    "schemaVersion": 1,
    "profile": "firecrab-release-assurance-v1",
    "stage": "host-release-assurance",
    "subject": {
        "sha": os.environ["ACTUAL_SHA"],
        "target": os.environ["TARGET"],
        "architecture": os.environ["ARCH"],
        "libc": os.environ["LIBC"],
    },
    "runner": {"platform": platform.platform(), "architecture": platform.machine()},
    "verdict": os.environ["STATUS"],
    "reason": os.environ["REASON"],
    "artifact": sha256(artifact),
}
result.parent.mkdir(parents=True, exist_ok=True)
result.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
  return "$rc"
}

finish() {
  rc=$?
  trap - EXIT
  [ -z "$unpacked" ] || rm -rf -- "$unpacked"
  [ -z "$work" ] || rm -rf -- "$work"
  rm -rf -- "$ROOT/firecrab-frontend/node_modules"
  write_result "$rc" || true
  exit "$rc"
}
trap finish EXIT

block() {
  status=BLOCKED
  reason=$1
  printf 'host assurance: BLOCKED: %s\n' "$reason" >&2
  exit 3
}

fail() {
  status=FAIL
  reason=$1
  printf 'host assurance: FAIL: %s\n' "$reason" >&2
  exit 1
}

normalize_arch() {
  case "$1" in
    x86_64|amd64) printf '%s\n' x86_64 ;;
    aarch64|arm64) printf '%s\n' aarch64 ;;
    *) printf '%s\n' unknown ;;
  esac
}

for command in git python3 cargo rustc rustup node npm shellcheck tar gzip sha256sum; do
  command -v "$command" >/dev/null 2>&1 || block "required native-lab command not found: $command"
done
if [ "$libc" = musl ]; then
  command -v musl-gcc >/dev/null 2>&1 || block "musl target requires musl-gcc on the native runner"
fi

host_arch=$(normalize_arch "$(uname -m)")
[ "$host_arch" = "$arch" ] || block "requires native $arch runner; current host is $host_arch"

if [ -n "$expected_sha" ] && [ "${actual_sha,,}" != "${expected_sha,,}" ]; then
  fail "checkout SHA mismatch: expected $expected_sha, got $actual_sha"
fi
if [ -n "$(git status --porcelain=v1 --untracked-files=all)" ]; then
  fail 'source checkout is not clean before host assurance build'
fi

printf '== release compliance preflight ==\n'
GITFLARE_EXPECTED_SHA="$actual_sha" bash "$ROOT/scripts/gitflare-release-compliance.sh"
preflight_verdict="$ROOT/dist/gitflare-receipts/verdict.json"
python3 - "$preflight_verdict" "$actual_sha" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit("preflight verdict is missing")
doc = json.loads(path.read_text(encoding="utf-8"))
if doc.get("verdict") != "PASS":
    raise SystemExit(f"preflight did not pass: {doc.get('verdict')}")
if doc.get("sha") != sys.argv[2]:
    raise SystemExit("preflight subject SHA does not match host assurance SHA")
PY

work="$ROOT/.gitflare/host-assurance/$target"
rm -rf -- "$work" "$ROOT/firecrab-frontend/node_modules"
mkdir -p "$work/cargo" "$work/target" "$work/npm-cache" "$result_dir"
export CARGO_HOME="$work/cargo"
export CARGO_TARGET_DIR="$work/target"
export npm_config_cache="$work/npm-cache"
export PYTHONDONTWRITEBYTECODE=1

printf '== native host build: %s ==\n' "$target"
rustup target add "$target"
cargo build --release --locked --target "$target" \
  -p firecrab-api -p firecrab-net-helper -p firecrab-cli

for binary in firecrab-api firecrab-net-helper firecrab; do
  "$ROOT/scripts/verify-release-binary.sh" \
    "$work/target/$target/release/$binary" "$arch" "$libc"
done

printf '== fresh dashboard build ==\n'
npm ci --prefix "$ROOT/firecrab-frontend"
npm run build --prefix "$ROOT/firecrab-frontend"

printf '== host package ==\n'
"$ROOT/scripts/package-host-release.sh" \
  "$arch" "$work/target/$target/release" "$ROOT/firecrab-frontend/dist" \
  "$artifact" "$ROOT/dist/compliance" >/dev/null
[ -s "$artifact" ] || fail "host archive missing: $artifact"

gzip -t "$artifact"
unpacked=$(mktemp -d)
tar -xzf "$artifact" -C "$unpacked"
for required in \
  firecrab-api firecrab-net-helper firecrab \
  LICENSE THIRD_PARTY_NOTICES.txt release-license-inventory.json \
  licenses/GPL-2.0-only.txt extract-vmlinux extract-arm64-image; do
  [ -e "$unpacked/$required" ] || fail "host archive missing $required"
done

for binary in firecrab-api firecrab-net-helper firecrab; do
  "$ROOT/scripts/verify-release-binary.sh" "$unpacked/$binary" "$arch" "$libc"
done
cmp "$ROOT/LICENSE" "$unpacked/LICENSE"
cmp "$ROOT/licenses/GPL-2.0-only.txt" "$unpacked/licenses/GPL-2.0-only.txt"
cmp "$ROOT/dist/compliance/THIRD_PARTY_NOTICES.txt" "$unpacked/THIRD_PARTY_NOTICES.txt"
cmp "$ROOT/dist/compliance/release-license-inventory.json" "$unpacked/release-license-inventory.json"

sha256sum "$artifact" >"$result_dir/SHA256SUMS"
status=PASS
reason='native binaries, dashboard, host archive, compliance members, architecture/libc, and independent hash verified'
printf 'host assurance: PASS %s\n' "$target"
