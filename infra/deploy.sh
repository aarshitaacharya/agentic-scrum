#!/usr/bin/env bash
#
# infra/deploy.sh — put the stack on AWS.
#
#   export GEMINI_API_KEY=...
#   ./infra/preflight.sh      # read-only checks, costs nothing — run this first
#   ./infra/deploy.sh
#
# When you are done:  ./infra/teardown.sh
#
# Overridable: AWS_REGION, STACK_NAME, SECRET_NAME, BUDGET_EMAIL,
#              MONTHLY_BUDGET, SUPERVISOR_CONCURRENCY, LOG_RETENTION_DAYS

set -euo pipefail

STACK_NAME="${STACK_NAME:-agentic-scrum}"
REGION="${AWS_REGION:-us-east-1}"
SECRET_NAME="${SECRET_NAME:-agentic-scrum/gemini-api-key}"
BUDGET_EMAIL="${BUDGET_EMAIL:-aarshita08@gmail.com}"
MONTHLY_BUDGET="${MONTHLY_BUDGET:-10}"
LOG_RETENTION_DAYS="${LOG_RETENTION_DAYS:-7}"
# Set to -1 if the deploy fails with "reserved concurrency below minimum" —
# AWS requires 100 unreserved, so a low account quota forbids reserving any.
SUPERVISOR_CONCURRENCY="${SUPERVISOR_CONCURRENCY:-1}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Pick up GEMINI_API_KEY from .env if it is not already exported, so the key
# lives in one gitignored place rather than in your shell history.
if [[ -z "${GEMINI_API_KEY:-}" && -f "$ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; source "$ROOT/.env"; set +a
  [[ -n "${GEMINI_API_KEY:-}" ]] && echo "==> Loaded GEMINI_API_KEY from .env"
fi

# Size the deployment to this account's Lambda concurrency quota. New accounts
# get 10, and AWS refuses any reserved concurrency unless 100 would remain
# unreserved — so on a new account the supervisor simply cannot reserve, and
# the per-queue caps have to stay under the total.
QUOTA="$(aws service-quotas get-service-quota \
  --service-code lambda --quota-code L-B99A9384 \
  --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "")"
QUOTA="${QUOTA%.*}"

if [[ -n "$QUOTA" && "$QUOTA" =~ ^[0-9]+$ ]]; then
  echo "==> Lambda concurrency quota: $QUOTA"
  if (( QUOTA < 200 )) && [[ "$SUPERVISOR_CONCURRENCY" == "1" ]]; then
    echo "    Too low to reserve any concurrency — deploying without a reservation."
    echo "    (The supervisor's conditional writes handle contention anyway; the"
    echo "     reservation only made it rarer.)"
    SUPERVISOR_CONCURRENCY=-1
  fi
  # Keep 4 x per-queue concurrency below the quota, with room for the HTTP
  # functions. Otherwise throttled invocations retry and drain into the DLQ.
  MAX_PER_QUEUE_DEFAULT=$(( (QUOTA - 2) / 4 ))
  (( MAX_PER_QUEUE_DEFAULT < 2 )) && MAX_PER_QUEUE_DEFAULT=2
  MAX_PER_QUEUE="${MAX_PER_QUEUE:-$MAX_PER_QUEUE_DEFAULT}"
  echo "    Per-queue concurrency cap: $MAX_PER_QUEUE (4 queues => $(( MAX_PER_QUEUE * 4 )) max)"
else
  MAX_PER_QUEUE="${MAX_PER_QUEUE:-2}"
  echo "==> Could not read the concurrency quota; using a conservative cap of $MAX_PER_QUEUE"
fi
echo

command -v sam >/dev/null || {
  echo "The SAM CLI is not installed:  brew install aws-sam-cli"
  exit 1
}

# Fail the deploy if the code's routing table and the template's filter
# policies disagree. The mismatch errors nowhere at runtime — an agent just
# silently never wakes up — so it has to be caught here.
echo "==> Checking routing table against template..."
# check_routing.py needs PyYAML, which the system python usually lacks. Prefer
# the project venv so the check runs for real rather than dying on an import
# and aborting the deploy.
if [[ -x "$ROOT/.venv/bin/python" ]] && "$ROOT/.venv/bin/python" -c "import yaml" 2>/dev/null; then
  "$ROOT/.venv/bin/python" "$ROOT/infra/check_routing.py"
elif python3 -c "import yaml" 2>/dev/null; then
  python3 "$ROOT/infra/check_routing.py"
else
  echo "    SKIPPED: PyYAML is not installed for any python on PATH."
  echo "    Install it (pip install -r requirements-dev.txt) so this check can run —"
  echo "    routing drift fails silently in production, which is why it exists."
fi
echo

# The API key goes in Secrets Manager, not a template parameter: parameters are
# readable in the CloudFormation console and in `describe-stacks` output. The
# secret lives OUTSIDE the stack deliberately, so tearing the stack down and
# redeploying does not make you re-enter the key.
echo "==> Ensuring the API key secret exists..."
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  echo "    reusing $SECRET_NAME"
else
  if [[ -z "${GEMINI_API_KEY:-}" ]]; then
    echo "    GEMINI_API_KEY is not set in this shell, and the secret does not exist yet."
    echo "    export GEMINI_API_KEY=... and re-run."
    exit 1
  fi
  aws secretsmanager create-secret \
    --name "$SECRET_NAME" \
    --description "Gemini API key for the agentic-scrum agents" \
    --secret-string "$GEMINI_API_KEY" \
    --region "$REGION" >/dev/null
  echo "    created $SECRET_NAME"
fi

SECRET_ARN="$(aws secretsmanager describe-secret \
  --secret-id "$SECRET_NAME" --region "$REGION" --query ARN --output text)"
echo

# This project ships 13 compiled extensions (pydantic-core, orjson, cryptography
# ...). If any is built for macOS the Lambda dies at cold start with an opaque
# ImportError. --use-container builds inside the Lambda runtime image, which
# guarantees they match. Without Docker, SAM's pip builder still resolves
# manylinux wheels and produces a correct bundle for this dependency set — so
# we fall back rather than refuse, then verify before deploying either way.
echo "==> Building..."
if docker info >/dev/null 2>&1; then
  echo "    Docker is up — building inside the Lambda runtime image."
  sam build --use-container --template "$ROOT/infra/template.yaml"
else
  echo "    Docker is not running — using SAM's pip builder instead."
  echo "    That normally resolves manylinux wheels correctly; verified below."
  sam build --template "$ROOT/infra/template.yaml"
fi

# Whichever path we took, prove no macOS binaries made it into the bundle.
# This check costs a second and saves an afternoon of reading CloudWatch.
echo "==> Verifying the bundle targets Linux..."
MACHO="$(find "$ROOT/.aws-sam/build" \( -name "*.so" -o -name "*.dylib" \) \
         -exec file {} \; 2>/dev/null | grep -ci "mach-o" || true)"
ELF="$(find "$ROOT/.aws-sam/build" -name "*.so" \
       -exec file {} \; 2>/dev/null | grep -ci "ELF" || true)"
if [[ "$MACHO" -gt 0 ]]; then
  echo "    FAIL: $MACHO macOS binaries in the bundle. They will not load on Lambda."
  echo "    Start Docker Desktop and re-run — --use-container fixes this."
  exit 1
fi
echo "    ok: $ELF Linux extensions, 0 macOS binaries"
echo

echo "==> Deploying..."
sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_IAM \
  --resolve-s3 \
  --no-confirm-changeset \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "GeminiApiKeySecretArn=$SECRET_ARN" \
    "BudgetEmail=$BUDGET_EMAIL" \
    "MonthlyBudgetUsd=$MONTHLY_BUDGET" \
    "SupervisorReservedConcurrency=$SUPERVISOR_CONCURRENCY" \
    "MaxConcurrencyPerQueue=$MAX_PER_QUEUE" \
    "LogRetentionDays=$LOG_RETENTION_DAYS"
echo

echo "==========================================================="
echo "  Deployed."
echo
echo "  Test it end to end:"
echo "      python infra/smoke_test.py"
echo
echo "  Point a local process at the stack:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`LocalEnv`].OutputValue' --output text | sed 's/^/      /'
echo
echo "  Confirm the budget email AWS just sent you, or the alarm is decorative."
echo
echo "  When you are done:   ./infra/teardown.sh"
echo "==========================================================="
