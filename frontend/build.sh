#!/usr/bin/env bash
# Cloudflare Pages build command: `bash frontend/build.sh`, output directory: `frontend`.
# The site is static; the build just places the committed data next to it.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data
cp ../data/companies.json ../data/prices.json data/
