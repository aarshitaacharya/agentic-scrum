"""
config.py — decides, once per process, which half of the infrastructure is live.

Every piece of infrastructure in this project has two implementations sitting
behind one interface:

    event bus   SNS + SQS   <->  in-process queues      (bus/local.py)
    artifacts   S3          <->  the workspace/ folder  (store/local.py)
    run state   DynamoDB    <->  a JSON file            (store/local.py)
    compute     Lambda      <->  threads in one process (supervisor.py)

`resolve_backends()` picks a half. It does not guess: it probes. boto3 has to
import, the resource identifiers have to be configured, and STS has to actually
answer before we call ourselves AWS-backed. If any check fails we fall back to
the local implementation and keep the reason, so the startup banner and the
/backends endpoint can say exactly which check failed and why.

That is the whole trick to the fallback: the *decision* is centralised here and
the *semantics* are identical either way, so nothing downstream knows or cares.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

try:  # optional — a plain local run does not need it
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ── Settings ──────────────────────────────────────────────────────────────────

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    """Everything tunable, read from the environment exactly once."""

    # Which backend to use: "auto" probes AWS and falls back, "aws" demands it,
    # "local" skips the probe entirely (what you want on a plane / in CI).
    backend: str = os.environ.get("AGENTIC_SCRUM_BACKEND", "auto").lower()

    # ── AWS resource identifiers ──
    region: str = os.environ.get("AWS_REGION", "us-east-1")
    topic_arn: str = os.environ.get("SCRUM_TOPIC_ARN", "")
    bucket: str = os.environ.get("SCRUM_BUCKET", "")
    state_table: str = os.environ.get("SCRUM_STATE_TABLE", "")
    # Queue URLs, one per subscriber. Set individually, or let them be derived
    # from a common prefix (what the SAM template produces).
    queue_prefix: str = os.environ.get("SCRUM_QUEUE_PREFIX", "")
    queue_urls: dict = field(default_factory=lambda: {
        name: os.environ.get(f"SCRUM_QUEUE_{name.upper()}", "")
        for name in ("pm", "dev", "qa", "supervisor")
    })

    # ── Model ──
    # Locally the key comes from .env. In Lambda it must NOT: environment
    # variables are visible to anyone with lambda:GetFunctionConfiguration and
    # are printed in the console. There, only the secret's ARN is passed and
    # the value is fetched at cold start — see resolve_gemini_api_key().
    gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
    gemini_secret_arn: str = os.environ.get("GEMINI_API_KEY_SECRET_ARN", "")
    model_name: str = os.environ.get("SCRUM_MODEL", "gemini-2.5-flash")
    temperature: float = float(os.environ.get("SCRUM_TEMPERATURE", "0.2"))

    # ── Agent loop limits ──
    max_attempts: int = _int_env("SCRUM_MAX_ATTEMPTS", 3)        # Dev↔QA cycles
    max_react_steps: int = _int_env("SCRUM_MAX_REACT_STEPS", 8)  # per agent turn
    # Circuit breaker. A healthy run publishes ~8 events; 60 means something is
    # looping and should be killed before it invoices you for the privilege.
    max_events_per_run: int = _int_env("SCRUM_MAX_EVENTS_PER_RUN", 60)
    tool_timeout: int = _int_env("SCRUM_TOOL_TIMEOUT", 15)       # seconds

    # Where the UI read model is written. Lambda's working directory is
    # read-only, so there it is either pointed at /tmp or disabled entirely.
    ui_state_file: str = os.environ.get("SCRUM_UI_STATE_FILE", "workspace/state.json")

    # ── Queue semantics (mirrored by the local bus so behaviour matches) ──
    visibility_timeout: int = _int_env("SCRUM_VISIBILITY_TIMEOUT", 120)
    max_receives: int = _int_env("SCRUM_MAX_RECEIVES", 3)  # then -> DLQ
    long_poll_seconds: int = _int_env("SCRUM_LONG_POLL", 5)

    def queue_url(self, subscriber: str) -> str:
        """Explicit URL wins; otherwise derive it from the prefix."""
        explicit = self.queue_urls.get(subscriber, "")
        if explicit:
            return explicit
        if self.queue_prefix:
            return f"{self.queue_prefix.rstrip('/')}/agentic-scrum-{subscriber}"
        return ""


SETTINGS = Settings()


# ── Backend probe ─────────────────────────────────────────────────────────────

@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class BackendDecision:
    """The answer to 'are we on AWS?' plus the evidence for it."""

    mode: str            # "aws" | "local"
    reason: str          # one line, shown in the banner and the UI
    checks: list         # list[Check] — the full audit trail

    @property
    def is_aws(self) -> bool:
        return self.mode == "aws"

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "checks": [c.as_dict() for c in self.checks],
        }

    def banner(self) -> str:
        head = "AWS (SNS + SQS + S3)" if self.is_aws else "LOCAL (in-process bus + workspace/)"
        lines = [f"  backend : {head}", f"  reason  : {self.reason}"]
        for c in self.checks:
            lines.append(f"    [{'x' if c.ok else ' '}] {c.name}: {c.detail}")
        return "\n".join(lines)


_DECISION: BackendDecision | None = None


def resolve_backends(settings: Settings = SETTINGS, force: bool = False) -> BackendDecision:
    """
    Run the probe (once; cached afterwards) and return the decision.

    The probe is deliberately ordered cheapest-first so the common local case
    costs nothing: a string compare, then an import, then env vars, and only
    then a network call to STS.
    """
    global _DECISION
    if _DECISION is not None and not force:
        return _DECISION

    checks: list[Check] = []

    # 1. Explicit opt-out. No probe, no import, no network.
    if settings.backend == "local":
        _DECISION = BackendDecision(
            "local",
            "AGENTIC_SCRUM_BACKEND=local — AWS probe skipped",
            [Check("backend_flag", True, "forced local")],
        )
        return _DECISION

    # 2. Is the SDK even installed?
    try:
        import boto3  # noqa: F401
        from botocore.config import Config as BotoConfig  # noqa: F401
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: F401

        checks.append(Check("boto3_installed", True, f"boto3 {boto3.__version__}"))
    except ImportError as exc:
        checks.append(Check("boto3_installed", False, str(exc)))
        return _finish(settings, "local", "boto3 is not installed", checks)

    # 3. Are the resources configured? Without a topic ARN there is nothing to
    #    publish to, and a half-configured AWS run is worse than a local one.
    missing = [
        label
        for label, value in (
            ("SCRUM_TOPIC_ARN", settings.topic_arn),
            ("SCRUM_BUCKET", settings.bucket),
        )
        if not value
    ]
    if missing:
        checks.append(Check("resources_configured", False, f"unset: {', '.join(missing)}"))
        return _finish(settings, "local", f"{missing[0]} is not set", checks)
    checks.append(Check("resources_configured", True, f"topic + bucket set ({settings.region})"))

    # 4. Do we hold usable credentials *right now*? Expired student creds show
    #    up here as ExpiredToken / InvalidClientTokenId rather than as a
    #    mystery timeout three calls later.
    try:
        from botocore.config import Config as BotoConfig

        sts = boto3.client(
            "sts",
            region_name=settings.region,
            config=BotoConfig(
                connect_timeout=3,
                read_timeout=3,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        identity = sts.get_caller_identity()
        account = identity.get("Account", "?")
        checks.append(Check("credentials_valid", True, f"account {account}"))
    except Exception as exc:  # noqa: BLE001 — any failure means "not on AWS"
        detail = f"{type(exc).__name__}: {exc}"
        checks.append(Check("credentials_valid", False, detail[:200]))
        return _finish(settings, "local", "AWS credentials unusable (expired or absent)", checks)

    return _finish(settings, "aws", f"verified against account {account}", checks)


def _finish(settings: Settings, mode: str, reason: str, checks: list) -> BackendDecision:
    """Cache the decision, honouring backend=aws as a hard requirement."""
    global _DECISION
    if mode == "local" and settings.backend == "aws":
        raise RuntimeError(
            f"AGENTIC_SCRUM_BACKEND=aws was requested but the probe failed: {reason}. "
            "Unset it (or set it to 'auto') to allow the local fallback."
        )
    _DECISION = BackendDecision(mode, reason, checks)
    return _DECISION


# ── Secrets ───────────────────────────────────────────────────────────────────

_API_KEY: str | None = None


def resolve_gemini_api_key(settings: Settings = SETTINGS) -> str:
    """
    Get the model API key, from the environment locally or Secrets Manager on AWS.

    Cached at module scope on purpose. Module scope in Lambda survives warm
    invocations, so this is one Secrets Manager call per cold start rather than
    one per agent turn — which matters for latency far more than for the
    $0.05-per-10,000-calls price.

    Order is deliberate: an explicit GEMINI_API_KEY always wins, so you can
    point a local process at a deployed stack without it trying to read a
    secret your laptop has no IAM permission for.
    """
    global _API_KEY
    if _API_KEY:
        return _API_KEY

    if settings.gemini_api_key:
        _API_KEY = settings.gemini_api_key
        return _API_KEY

    if not settings.gemini_secret_arn:
        return ""

    try:
        import boto3

        client = boto3.client("secretsmanager", region_name=settings.region)
        response = client.get_secret_value(SecretId=settings.gemini_secret_arn)
    except Exception as exc:  # noqa: BLE001
        # Loud, but not a crash here — llm.py raises MissingAPIKey with
        # instructions, which is a far more useful failure than a boto
        # traceback out of a cold start.
        print(f"[config] could not read the API key secret: {type(exc).__name__}: {exc}")
        return ""

    secret = response.get("SecretString", "")
    # Accept either a bare string or the {"GEMINI_API_KEY": "..."} shape the
    # console produces if you create the secret as key/value instead of plaintext.
    if secret.startswith("{"):
        import json

        try:
            secret = json.loads(secret).get("GEMINI_API_KEY", "")
        except json.JSONDecodeError:
            pass

    _API_KEY = secret.strip()
    return _API_KEY
