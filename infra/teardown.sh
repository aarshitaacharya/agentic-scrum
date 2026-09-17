#!/usr/bin/env bash
#
# infra/teardown.sh — delete everything, then PROVE it is gone.
#
#   ./infra/teardown.sh
#
# Three things make teardown less trivial than "sam delete":
#
#   1. CloudFormation cannot delete a non-empty S3 bucket. The stack delete
#      fails halfway, leaves you in DELETE_FAILED, and it is easy to assume it
#      worked. So we empty the bucket first.
#
#   2. Deleting a Secrets Manager secret normally SCHEDULES deletion 30 days
#      out — and you are billed $0.40/month for the whole recovery window. The
#      only way to actually stop paying is --force-delete-without-recovery.
#
#   3. Log groups that Lambda auto-created (rather than ones the template
#      declares) survive the stack. We declare ours, but a pre-existing one
#      from an earlier deploy would linger, so we check.

set -uo pipefail

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-agentic-scrum}"
SECRET_NAME="${SECRET_NAME:-agentic-scrum/gemini-api-key}"

echo
echo "================ AGENTIC SCRUM — TEARDOWN ================"
echo "  stack:  $STACK_NAME"
echo "  region: $REGION"
echo

read -r -p "Delete the stack and all its data? This cannot be undone. [y/N] " CONFIRM
[[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]] || { echo "Aborted."; exit 0; }
echo

# ── 1. Empty the artifact bucket ──────────────────────────────────────────────
echo "==> Emptying the artifact bucket..."
BUCKET="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`ArtifactBucket`].OutputValue' \
  --output text 2>/dev/null)"

if [[ -n "$BUCKET" && "$BUCKET" != "None" ]]; then
  aws s3 rm "s3://$BUCKET" --recursive --region "$REGION" 2>/dev/null | tail -3
  # Versioned buckets keep old versions and delete markers that `s3 rm` leaves
  # behind; those still block the stack delete.
  aws s3api list-object-versions --bucket "$BUCKET" --region "$REGION" \
    --output json 2>/dev/null | python3 -c '
import json, subprocess, sys, os
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
items = data.get("Versions", []) + data.get("DeleteMarkers", [])
if not items:
    sys.exit(0)
bucket, region = os.environ["B"], os.environ["R"]
for chunk in (items[i:i+900] for i in range(0, len(items), 900)):
    payload = {"Objects": [{"Key": o["Key"], "VersionId": o["VersionId"]} for o in chunk]}
    subprocess.run(["aws","s3api","delete-objects","--bucket",bucket,
                    "--region",region,"--delete",json.dumps(payload)],
                   stdout=subprocess.DEVNULL)
print(f"  removed {len(items)} object versions")
' B="$BUCKET" R="$REGION"
  echo "    emptied $BUCKET"
else
  echo "    no bucket found in stack outputs (already gone?)"
fi
echo

# ── 2. Delete the stack ───────────────────────────────────────────────────────
echo "==> Deleting the stack (this takes a few minutes)..."
if command -v sam >/dev/null; then
  sam delete --stack-name "$STACK_NAME" --region "$REGION" --no-prompts
else
  aws cloudformation delete-stack --stack-name "$STACK_NAME" --region "$REGION"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK_NAME" --region "$REGION"
fi
echo

# ── 3. Force-delete the secret ────────────────────────────────────────────────
# Not part of the stack (deploy.sh creates it outside CloudFormation so the key
# survives stack churn), so it must be removed explicitly.
echo "==> Deleting the API key secret..."
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  aws secretsmanager delete-secret --secret-id "$SECRET_NAME" --region "$REGION" \
    --force-delete-without-recovery --output text >/dev/null 2>&1 \
    && echo "    deleted $SECRET_NAME (immediately — no 30-day billed recovery window)" \
    || echo "    could not delete $SECRET_NAME; do it in the console"
else
  echo "    no secret named $SECRET_NAME"
fi
echo

# ── 4. Verify ─────────────────────────────────────────────────────────────────
echo "==> Verifying nothing is left..."
LEFTOVERS=0

if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "  [!] the stack still exists — check the console for DELETE_FAILED"
  LEFTOVERS=$((LEFTOVERS+1))
else
  echo "  [ok] stack gone"
fi

if [[ -n "$BUCKET" && "$BUCKET" != "None" ]] && aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  echo "  [!] bucket $BUCKET still exists"
  LEFTOVERS=$((LEFTOVERS+1))
else
  echo "  [ok] artifact bucket gone"
fi

ORPHANS="$(aws logs describe-log-groups --region "$REGION" \
  --log-group-name-prefix "/aws/lambda/agentic-scrum" \
  --query 'logGroups[].logGroupName' --output text 2>/dev/null)"
if [[ -n "$ORPHANS" && "$ORPHANS" != "None" ]]; then
  echo "  [!] orphaned log groups:"
  for GROUP in $ORPHANS; do
    echo "        $GROUP"
    aws logs delete-log-group --log-group-name "$GROUP" --region "$REGION" 2>/dev/null \
      && echo "        ...deleted"
  done
else
  echo "  [ok] no orphaned log groups"
fi

QUEUES="$(aws sqs list-queues --region "$REGION" --queue-name-prefix agentic-scrum \
  --query 'QueueUrls' --output text 2>/dev/null)"
if [[ -n "$QUEUES" && "$QUEUES" != "None" ]]; then
  echo "  [!] queues still present: $QUEUES"
  LEFTOVERS=$((LEFTOVERS+1))
else
  echo "  [ok] no queues left"
fi
echo

echo "==========================================================="
if (( LEFTOVERS == 0 )); then
  echo "  Clean. Nothing from this project is still billable."
else
  echo "  $LEFTOVERS leftover(s) above — deal with those or they keep costing."
fi
cat <<'NOTE'

  Two things this does NOT delete, on purpose:
    - the SAM deployment bucket (aws-sam-cli-managed-default-*). It is shared
      with any other SAM project and holds a few MB. Costs cents.
    - the budget alarm, if you created it outside the stack.

  Confirm the bill zeroes out in a day or two:
    https://console.aws.amazon.com/billing/home#/bills
NOTE
echo "==========================================================="
echo
