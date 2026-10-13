#!/usr/bin/env bash
# Versioned WindAnvil adapter for the real FireCrab GNU host assurance build.
# The archived bytes are copied unchanged; the original evidence is retained.
set -euo pipefail
unset CDPATH
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [ "$#" -ne 2 ] || [ "$1" != "--target" ] || [ "$2" != "x86_64-unknown-linux-gnu" ]; then
  echo "BLOCKED: subject v1 host adapter accepts only x86_64-unknown-linux-gnu" >&2
  exit 3
fi
cd "$root"
bash scripts/gitflare-host-assurance.sh --target "$2"
dir="dist/assurance/host/$2"
source="$dir/firecrab-host-x86_64-gnu.tar.gz"
dest="$dir/subject-host-x86_64-gnu.tar.gz"
test -s "$source"
cp -- "$source" "$dest"
cmp -s "$source" "$dest"
sha256sum "$dest" > "$dir/SHA256SUMS"
# The original result.json remains authoritative for the subject's claims.
echo "PASS: byte-identical original FireCrab host archive bound to WindAnvil v1 adapter"
