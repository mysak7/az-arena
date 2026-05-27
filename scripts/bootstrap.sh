#!/usr/bin/env bash
# bootstrap.sh — One-time setup for az-arena
# Run this ONCE before any Terraform or GitHub Actions.
# Prerequisites: az CLI logged in, gh CLI authenticated.
set -euo pipefail

RESOURCE_GROUP="rg-dev-euc1-chaos-arena"
STORAGE_ACCOUNT="stdevchaosbattle"
LOCATION="eastus"
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)

echo "╔══════════════════════════════════════════════════════╗"
echo "║   az-arena · Bootstrap                               ║"
echo "╚══════════════════════════════════════════════════════╝"
echo "Subscription: $SUBSCRIPTION_ID"
echo "Tenant:       $TENANT_ID"
echo ""

# ── Step 1: Resource Group ─────────────────────────────────────────────────────
echo "▶ [1/5] Creating resource group..."
az group create \
  --name "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --output none
echo "  ✓ $RESOURCE_GROUP"

# ── Step 2: Storage Account for Terraform state ────────────────────────────────
echo "▶ [2/5] Creating Terraform state storage..."
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --min-tls-version TLS1_2 \
  --output none

az storage container create \
  --name tfstate \
  --account-name "$STORAGE_ACCOUNT" \
  --auth-mode login \
  --output none
echo "  ✓ $STORAGE_ACCOUNT / tfstate"

# ── Step 3: GitHub Actions OIDC federation ─────────────────────────────────────
echo "▶ [3/5] Setting up GitHub Actions OIDC..."
REPO="mysak7/az-arena"
APP_NAME="sp-chaos-arena-github-actions"

# Create app registration (or reuse if exists)
APP_ID=$(az ad app list --display-name "$APP_NAME" --query '[0].appId' -o tsv 2>/dev/null || true)
if [ -z "$APP_ID" ] || [ "$APP_ID" = "None" ]; then
  APP_ID=$(az ad app create --display-name "$APP_NAME" --query appId -o tsv)
  echo "  Created App: $APP_ID"
else
  echo "  Reusing existing App: $APP_ID"
fi

# Create service principal
SP_OBJ_ID=$(az ad sp list --filter "appId eq '$APP_ID'" --query '[0].id' -o tsv 2>/dev/null || true)
if [ -z "$SP_OBJ_ID" ] || [ "$SP_OBJ_ID" = "None" ]; then
  SP_OBJ_ID=$(az ad sp create --id "$APP_ID" --query id -o tsv)
  echo "  Created SP: $SP_OBJ_ID"
fi

# Assign Contributor on resource group
az role assignment create \
  --assignee "$APP_ID" \
  --role "Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP" \
  --output none 2>/dev/null || echo "  (Contributor already assigned)"

# Storage Blob Data Contributor for TF state
az role assignment create \
  --assignee "$APP_ID" \
  --role "Storage Blob Data Contributor" \
  --scope "/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.Storage/storageAccounts/$STORAGE_ACCOUNT" \
  --output none 2>/dev/null || echo "  (Storage role already assigned)"

# Federated credentials: push to main + workflow_dispatch
for SUBJECT in "ref:refs/heads/main" "ref:refs/heads/main:workflow_dispatch"; do
  NAME=$(echo "$SUBJECT" | tr ':/' '--')
  az ad app federated-credential create \
    --id "$APP_ID" \
    --parameters "{
      \"name\": \"github-$NAME\",
      \"issuer\": \"https://token.actions.githubusercontent.com\",
      \"subject\": \"repo:$REPO:$SUBJECT\",
      \"audiences\": [\"api://AzureADTokenExchange\"]
    }" \
    --output none 2>/dev/null || echo "  (Federation $SUBJECT already exists)"
done

echo "  ✓ OIDC federation configured"
echo ""
echo "  ┌─────────────────────────────────────────────────────┐"
echo "  │ Add these secrets to GitHub repo → Settings →       │"
echo "  │ Secrets and variables → Actions:                    │"
echo "  │                                                     │"
echo "  │  AZURE_CLIENT_ID:       $APP_ID │"
echo "  │  AZURE_TENANT_ID:       $TENANT_ID │"
echo "  │  AZURE_SUBSCRIPTION_ID: $SUBSCRIPTION_ID │"
echo "  └─────────────────────────────────────────────────────┘"

# ── Step 4: Register required Azure providers ──────────────────────────────────
echo "▶ [4/5] Registering Azure resource providers..."
PROVIDERS=(
  "Microsoft.ContainerService"
  "Microsoft.DocumentDB"
  "Microsoft.Storage"
  "Microsoft.ContainerRegistry"
  "Microsoft.KeyVault"
  "Microsoft.ManagedIdentity"
  "Microsoft.Network"
)
for p in "${PROVIDERS[@]}"; do
  az provider register --namespace "$p" --output none 2>/dev/null || true
  echo "  ✓ $p"
done

# ── Step 5: Set GitHub secrets (requires gh CLI) ───────────────────────────────
echo "▶ [5/5] Setting GitHub Actions secrets..."
if command -v gh &>/dev/null && gh auth status &>/dev/null; then
  gh secret set AZURE_CLIENT_ID       --body "$APP_ID"           --repo "$REPO"
  gh secret set AZURE_TENANT_ID       --body "$TENANT_ID"        --repo "$REPO"
  gh secret set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"  --repo "$REPO"
  echo "  ✓ Secrets pushed via gh CLI"
else
  echo "  ⚠ gh CLI not available or not authenticated — set secrets manually (see above)"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Bootstrap complete! Next steps:                    ║"
echo "║                                                      ║"
echo "║   1. Push code to main → GH Actions will deploy     ║"
echo "║   2. Or: Actions → Terraform Apply → Run workflow    ║"
echo "║   3. Get ingress IP from workflow logs               ║"
echo "║   4. Add A record: arena.mysak.fun → <ingress IP>   ║"
echo "╚══════════════════════════════════════════════════════╝"
