#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
for mode in direct vpn; do
 sed 's|context: ../..|context: .|' "deploy/oracle/compose.$mode.yaml" > "compose.$mode.yaml"
done
echo 'Root Compose files rendered from versioned Oracle templates.'