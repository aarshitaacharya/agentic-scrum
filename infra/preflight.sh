#!/usr/bin/env bash
#
# infra/preflight.sh — everything worth knowing BEFORE you spend anything.
#
# Read-only. Creates nothing, deletes nothing, costs nothing. Run it first.
#
#   ./infra/preflight.sh

set -uo pipefail   # deliberately no -e: a failed check should report, not abort

REGION="${AWS_REGION:-us-east-1}"
STACK_NAME="${STACK_NAME:-agentic-scrum}"
SECRET_NAME="${SECRET_NAME:-agentic-scrum/gemini-api-key}"

PASS=0; WARN=0; FAIL=0
ok()   { echo "  [ok]   $*"; PASS=$((PASS+1)); }
warn() { echo "  [warn] $*"; WARN=$((WARN+1)); }
bad()  { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }

echo
echo "================ AGENTIC SCRUM — PREFLIGHT ================"
echo

# ── Tooling ───────────────────────────────────────────────────────────────────
echo "Tooling"
command -v aws >/dev/null && ok "aws cli $(aws --version 2>&1 | cut -d' ' -f1 | cut -d/ -f2)" \
                          || bad "aws cli missing — brew install awscli"
# Do NOT suggest `brew install aws-sam-cli` here. On Intel macOS that pulls
# pydantic/rpds-py, which have no bottles for this platform, so Homebrew builds
# rust AND llvm from source — literally hours. pip has prebuilt wheels.
if command -v sam >/dev/null; then
  ok "sam cli $(sam --version 2>&1 | awk '{print $4}')"
else
  bad "sam cli missing. Install it with pip, NOT brew:"
  echo "           python3 -m venv ~/.sam-cli-venv"
  echo "           ~/.sam-cli-venv/bin/pip install aws-sam-cli"
  echo "           ln -sf ~/.sam-cli-venv/bin/sam /usr/local/bin/sam"
  echo "         (brew builds llvm from source on Intel macs — it takes hours)"
fi
command -v docker >/dev/null && ok "docker present (used by 'sam build --use-container')" \
                             || warn "docker missing — build will use your local python instead"
python3 -c 'import sys; exit(0 if sys.version_info >= (3,9) else 1)' 2>/dev/null \
  && ok "python3 $(python3 -V 2>&1 | awk '{print $2}')" || warn "python3 is old or missing"
echo

# ── Identity ──────────────────────────────────────────────────────────────────
echo "Account"
IDENTITY="$(aws sts get-caller-identity --output json 2>&1)"
if echo "$IDENTITY" | grep -q '"Account"'; then
  ACCOUNT="$(echo "$IDENTITY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Account"])')"
  ARN="$(echo "$IDENTITY" | python3 -c 'import json,sys;print(json.load(sys.stdin)["Arn"])')"
  ok "account $ACCOUNT"
  ok "identity $ARN"
  case "$ARN" in
    *":root") warn "you are using ROOT credentials. Fine for a weekend test, but create an IAM user with AdministratorAccess and use that instead." ;;
  esac
else
  bad "no usable credentials. Run 'aws configure' (you need an access key from IAM)."
  echo "$IDENTITY" | head -2 | sed 's/^/         /'
  echo; echo "Stopping here — nothing else can be checked without credentials."
  exit 1
fi
ok "region $REGION"
echo

# ── Billing ───────────────────────────────────────────────────────────────────
# This is the check that matters for "will my deploy mysteriously fail".
# An account without a valid payment method can create IAM users but silently
# fails on resource creation.
echo "Billing"
COST="$(aws ce get-cost-and-usage \
  --time-period "Start=$(date -u -v1d +%Y-%m-%d 2>/dev/null || date -u -d "$(date +%Y-%m-01)" +%Y-%m-%d),End=$(date -u +%Y-%m-%d)" \
  --granularity MONTHLY --metrics UnblendedCost --output json 2>&1)"
if echo "$COST" | grep -q "Amount"; then
  SPEND="$(echo "$COST" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d.get("ResultsByTime",[])
print(r[0]["Total"]["UnblendedCost"]["Amount"][:6] if r else "0")' 2>/dev/null)"
  ok "month-to-date spend: \$${SPEND:-0}"
else
  warn "Cost Explorer not readable (it needs enabling once in the console, and takes ~24h)."
  warn "Not a blocker. Check https://console.aws.amazon.com/billing/home#/ manually."
fi
echo

# ── Quotas ────────────────────────────────────────────────────────────────────
echo "Quotas"
CONCURRENCY="$(aws service-quotas get-service-quota \
  --service-code lambda --quota-code L-B99A9384 \
  --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null)"
if [[ -n "$CONCURRENCY" && "$CONCURRENCY" != "None" ]]; then
  CONC_INT="${CONCURRENCY%.*}"
  if (( CONC_INT >= 200 )); then
    ok "lambda concurrent executions: $CONC_INT"
  else
    warn "lambda concurrency is only $CONC_INT (the new-account default is 10)."
    warn "AWS requires 100 to remain unreserved, so nothing can reserve concurrency."
    warn "deploy.sh detects this and adjusts automatically — no action needed."
    warn "Per-queue caps will be sized to fit; enough for one run at a time."
  fi
else
  warn "could not read the Lambda concurrency quota (needs servicequotas:GetServiceQuota)"
fi
echo

# ── Collisions ────────────────────────────────────────────────────────────────
echo "Name collisions"
if aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" >/dev/null 2>&1; then
  STATUS="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --region "$REGION" \
            --query 'Stacks[0].StackStatus' --output text)"
  warn "stack '$STACK_NAME' already exists (status: $STATUS) — deploying will UPDATE it"
  case "$STATUS" in
    ROLLBACK_COMPLETE|CREATE_FAILED)
      bad "that status cannot be updated. Delete it first: aws cloudformation delete-stack --stack-name $STACK_NAME --region $REGION" ;;
  esac
else
  ok "no existing '$STACK_NAME' stack — this will be a clean create"
fi

BUCKET="agentic-scrum-artifacts-${ACCOUNT}-${REGION}"
if aws s3api head-bucket --bucket "$BUCKET" 2>/dev/null; then
  warn "bucket $BUCKET already exists (yours, from a previous run)"
else
  ok "bucket name $BUCKET is free"
fi

if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  DELETION="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" \
              --query 'DeletedDate' --output text 2>/dev/null)"
  if [[ "$DELETION" != "None" && -n "$DELETION" ]]; then
    bad "secret '$SECRET_NAME' is pending deletion. Restore it, or pick a different SECRET_NAME."
  else
    ok "secret '$SECRET_NAME' already exists — it will be reused"
  fi
else
  ok "secret '$SECRET_NAME' does not exist yet — deploy.sh will create it"
fi
echo

# ── API key ───────────────────────────────────────────────────────────────────
echo "Model access"
ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  ok "GEMINI_API_KEY is set in this shell (${#GEMINI_API_KEY} chars)"
elif [[ -f "$ROOT_DIR/.env" ]] && grep -qE '^GEMINI_API_KEY=.+' "$ROOT_DIR/.env"; then
  # deploy.sh sources .env, so a key living only there is perfectly fine.
  ok "GEMINI_API_KEY found in .env"
elif aws secretsmanager describe-secret --secret-id "$SECRET_NAME" --region "$REGION" >/dev/null 2>&1; then
  ok "GEMINI_API_KEY not in the shell, but the secret already exists"
else
  bad "no Gemini API key anywhere. Put it in .env (which is gitignored):"
  echo "           echo 'GEMINI_API_KEY=your-key' > $ROOT_DIR/.env"
fi
echo

# ── Routing ───────────────────────────────────────────────────────────────────
echo "Code"
# Use the project venv if there is one: check_routing.py needs PyYAML, and
# system python3 usually does not have it. Reporting "routing drift" because an
# import failed would be a false alarm — worse than not checking at all, since
# it sends you hunting a bug that is not there.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  CHECK_PY="$ROOT_DIR/.venv/bin/python"
else
  CHECK_PY="python3"
fi

if ! "$CHECK_PY" -c "import yaml" 2>/dev/null; then
  warn "cannot check routing: PyYAML is not installed for $CHECK_PY"
  warn "  pip install -r requirements-dev.txt"
elif ROUTING_OUT="$("$CHECK_PY" "$ROOT_DIR/infra/check_routing.py" 2>&1)"; then
  ok "events.py and template.yaml agree on routing"
else
  bad "routing drift between events.py and template.yaml:"
  echo "$ROUTING_OUT" | sed 's/^/         /'
fi
echo

# ── What this will cost ───────────────────────────────────────────────────────
cat <<'COST'
Expected cost
  Lambda      1M requests + 400k GB-s/month always free    ~$0.00
  SNS         1M publishes/month always free               ~$0.00
  SQS         1M requests/month always free                ~$0.00
  CloudWatch  5GB ingestion/month always free              ~$0.00
  DynamoDB    on-demand, a few hundred tiny writes         ~$0.00
  S3          a few MB                                     ~$0.00
  Secrets Mgr $0.40/secret/month, NO free tier             ~$0.40
  ------------------------------------------------------------
  A week of testing, realistically:                        under $1

  No VPC and no NAT gateway anywhere in this stack — that is the usual way a
  hobby project turns into $32/month.

  The budget alarm ALERTS, it does not enforce. Nothing here stops spending
  automatically. When you are done: ./infra/teardown.sh
COST
echo
echo "==========================================================="
printf "  %d ok, %d warnings, %d failures\n" "$PASS" "$WARN" "$FAIL"
echo "==========================================================="
echo
if (( FAIL > 0 )); then
  echo "Fix the failures above before running ./infra/deploy.sh"
  exit 1
fi
echo "Good to deploy:  ./infra/deploy.sh"
echo
