#!/usr/bin/env bash
# Vercel's project root is the nested reservation-hub/ folder, so keep its public/ identical to the top-level one.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf reservation-hub/public && cp -R public reservation-hub/public
cp vercel.json reservation-hub/vercel.json
rm -f index.html reservation-hub/index.html
echo "synced public/ -> reservation-hub/public/"
