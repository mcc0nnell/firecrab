#!/usr/bin/env bash
# Versioned WindAnvil adapter for the actual manifest-pinned Rocky 9.8 image.
set -euo pipefail
unset CDPATH
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [ "$#" -ne 2 ] || [ "$1" != "--alias" ] || [ "$2" != "rocky-9.8" ]; then
  echo "BLOCKED: subject v1 Rocky adapter requires the manifest-backed rocky-9.8 alias" >&2
  exit 3
fi
cd "$root"
exec bash scripts/gitflare-m2image-assurance.sh --alias rocky-9.8 --arch x86_64
