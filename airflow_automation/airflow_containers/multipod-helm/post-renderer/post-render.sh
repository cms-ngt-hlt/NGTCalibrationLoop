#!/usr/bin/env bash
# What Airflow's chart renders, through kustomization.yaml next to this file, for what the chart's values can't set:
# Helm passes the manifests on stdin and applies what comes out on stdout. Run by Helm as the plugin in plugin.yaml.
set -euo pipefail

here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cat > "$work/chart.yaml"
cp "$here/kustomization.yaml" "$work/"
kubectl kustomize "$work"
