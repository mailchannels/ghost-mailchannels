#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
docker build -t mailchannels-ghost-tests -f "$root/test/Dockerfile" "$root"
docker run --rm --network none mailchannels-ghost-tests
