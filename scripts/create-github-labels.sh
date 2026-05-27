#!/usr/bin/env bash
# create-github-labels.sh
# Creates GitHub labels for the incident ticketing system.
#
# Usage:
#   export GITHUB_TOKEN=ghp_xxx
#   export GITHUB_REPO=mi/az-arena   # or set here
#   ./scripts/create-github-labels.sh
#
# Requires: gh CLI (https://cli.github.com/) or curl + jq

set -euo pipefail

REPO="${GITHUB_REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo '')}"

if [[ -z "$REPO" ]]; then
  echo "❌ Set GITHUB_REPO env var or run inside a GitHub repo with gh CLI"
  exit 1
fi

echo "📋 Creating labels in: $REPO"

create_label() {
  local name="$1"
  local color="$2"   # hex without #
  local description="$3"

  if gh label list --repo "$REPO" --json name -q '.[].name' | grep -qx "$name"; then
    echo "  ↳ already exists: $name"
  else
    gh label create "$name" \
      --repo "$REPO" \
      --color "$color" \
      --description "$description"
    echo "  ✅ created: $name"
  fi
}

# Core incident label
create_label "incident"  "e11d48" "Active incident — fault is live"

# Fault type labels
create_label "fault-001" "f97316" "FAULT_001: OOM Kill (memory limit 100Mi)"
create_label "fault-002" "eab308" "FAULT_002: DB Connection Exhaustion (no PgBouncer)"
create_label "fault-003" "ef4444" "FAULT_003: Network Blackout (deny-all NetworkPolicy)"
create_label "fault-004" "a855f7" "FAULT_004: Autoscaler Deadlock (KEDA + same DB)"
create_label "fault-005" "3b82f6" "FAULT_005: BlobFuse2 Dangling Lease"
create_label "fault-006" "06b6d4" "FAULT_006: No PodDisruptionBudget"

# Scenario labels
create_label "scenario-oom"              "fca5a5" "SCENARIO_OOM: POST /leak → OOMKilled"
create_label "scenario-db-exhaust"       "fde68a" "SCENARIO_DB_EXHAUST: POST /lock-db"
create_label "scenario-network"          "f87171" "SCENARIO_NETWORK: NetworkPolicy blackout"
create_label "scenario-blob-lease"       "93c5fd" "SCENARIO_BLOB_LEASE: BlobFuse2 node delete"
create_label "scenario-scale-zero"       "d1d5db" "SCENARIO_SCALE_ZERO: kubectl scale --replicas=0"
create_label "scenario-queue-flood"      "c4b5fd" "SCENARIO_QUEUE_FLOOD: POST /flood-queue"
create_label "scenario-autoscaler-deadlock" "e9d5ff" "SCENARIO_AUTOSCALER_DEADLOCK: lock-db + flood-queue"

echo ""
echo "✅ All labels ready in $REPO"
echo "   View: https://github.com/$REPO/labels"
