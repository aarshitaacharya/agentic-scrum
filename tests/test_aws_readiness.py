"""
The things that break only once you deploy.

Each test here corresponds to a bug that costs real money: it does not fail
locally, it fails as twelve Lambda invocations draining into a dead letter
queue while you read CloudWatch trying to work out why.
"""

import json
import os

import pytest

from scrum import config
from scrum import ui_state
from scrum.config import Settings, resolve_gemini_api_key


# ── The read-only filesystem ──────────────────────────────────────────────────

def test_ui_state_survives_a_read_only_filesystem(monkeypatch, capsys):
    """
    Lambda's working directory is read-only. ui_state is a cosmetic projection
    for a UI that does not exist there, so a failed write must never propagate
    — it would kill a handler that had already done its real work, the message
    would redeliver, and the whole expensive agent turn would run again.
    """
    monkeypatch.setattr(ui_state, "STATE_FILE", "/nonexistent-readonly-dir/state.json")
    monkeypatch.setattr(ui_state, "_CAN_WRITE", None)

    ui_state.set_state("pm", "analysing", "should not raise", 1)
    ui_state.set_state("dev", "patching", "still should not raise", 2)

    assert ui_state._CAN_WRITE is False, "it must stop retrying a filesystem it cannot write"
    output = capsys.readouterr().out
    assert "ui_state" in output, "the state should still be logged for CloudWatch"


def test_ui_state_logs_valid_json_when_it_cannot_write(monkeypatch, capsys):
    """CloudWatch Insights can only query it if it is real JSON."""
    monkeypatch.setattr(ui_state, "STATE_FILE", "/nonexistent-readonly-dir/state.json")
    monkeypatch.setattr(ui_state, "_CAN_WRITE", False)

    ui_state.set_state("qa", "reviewing", "check", 2, verdict="fail")

    line = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")][-1]
    payload = json.loads(line)
    assert payload["ui_state"]["agent"] == "qa"
    assert payload["ui_state"]["verdict"] == "fail"


def test_ui_state_still_writes_when_it_can(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_state, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ui_state, "_CAN_WRITE", None)

    ui_state.set_state("dev", "patching", "local run", 1)

    assert json.loads((tmp_path / "state.json").read_text())["agent"] == "dev"
    assert ui_state._CAN_WRITE is True


# ── The API key ───────────────────────────────────────────────────────────────

def test_env_key_beats_the_secret(monkeypatch):
    """
    So you can point a local process at a deployed stack without it trying to
    read a secret your laptop has no IAM permission for.
    """
    monkeypatch.setattr(config, "_API_KEY", None)
    settings = Settings(gemini_api_key="from-env", gemini_secret_arn="arn:aws:secretsmanager:x")

    assert resolve_gemini_api_key(settings) == "from-env"


def test_no_key_and_no_secret_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "_API_KEY", None)
    assert resolve_gemini_api_key(Settings(gemini_api_key="", gemini_secret_arn="")) == ""


def test_secret_is_read_from_secrets_manager(monkeypatch):
    """The Lambda path: only an ARN is passed, the value is fetched at cold start."""
    monkeypatch.setattr(config, "_API_KEY", None)

    import boto3
    from botocore.stub import Stubber

    client = boto3.client("secretsmanager", region_name="us-east-1",
                          aws_access_key_id="t", aws_secret_access_key="t")
    stub = Stubber(client)
    stub.add_response("get_secret_value", {"SecretString": "key-from-secrets-manager"},
                      expected_params={"SecretId": "arn:aws:secretsmanager:fake"})
    stub.activate()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)

    settings = Settings(gemini_api_key="", gemini_secret_arn="arn:aws:secretsmanager:fake")
    assert resolve_gemini_api_key(settings) == "key-from-secrets-manager"


def test_secret_accepts_the_key_value_json_shape(monkeypatch):
    """The console creates key/value secrets as JSON; plaintext is a separate choice."""
    monkeypatch.setattr(config, "_API_KEY", None)

    import boto3
    from botocore.stub import Stubber

    client = boto3.client("secretsmanager", region_name="us-east-1",
                          aws_access_key_id="t", aws_secret_access_key="t")
    stub = Stubber(client)
    stub.add_response("get_secret_value",
                      {"SecretString": json.dumps({"GEMINI_API_KEY": "nested-key"})},
                      expected_params={"SecretId": "arn:fake"})
    stub.activate()
    monkeypatch.setattr(boto3, "client", lambda *a, **k: client)

    assert resolve_gemini_api_key(
        Settings(gemini_api_key="", gemini_secret_arn="arn:fake")) == "nested-key"


def test_an_unreachable_secret_does_not_crash(monkeypatch, capsys):
    """A missing IAM permission should produce MissingAPIKey, not a boto traceback."""
    monkeypatch.setattr(config, "_API_KEY", None)

    import boto3

    def explode(*args, **kwargs):
        raise RuntimeError("AccessDeniedException")

    monkeypatch.setattr(boto3, "client", explode)
    assert resolve_gemini_api_key(Settings(gemini_api_key="", gemini_secret_arn="arn:x")) == ""
    assert "could not read the API key secret" in capsys.readouterr().out


# ── The circuit breaker ───────────────────────────────────────────────────────

@pytest.fixture
def supervisor_env(tmp_path, monkeypatch):
    from scrum import store
    from scrum.store.local import LocalArtifactStore, LocalRunStateStore

    monkeypatch.setattr(store, "_ARTIFACTS", LocalArtifactStore(str(tmp_path)))
    monkeypatch.setattr(store, "_RUN_STATE", LocalRunStateStore(str(tmp_path / "runs.json")))
    monkeypatch.setattr(ui_state, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(ui_state, "_CAN_WRITE", None)
    return store


def test_the_circuit_breaker_aborts_a_looping_run(supervisor_env):
    """
    A run that keeps generating events is the one failure the DLQ cannot catch,
    because every message is being handled *successfully*. Left alone it bills
    you all night.
    """
    from scrum.events import AgentEvent, RUN_ESCALATED, TICKET_CREATED
    from scrum.handlers import handle_supervisor

    settings = Settings(backend="local", max_events_per_run=5)
    emitted = []
    for _ in range(8):
        emitted = handle_supervisor(
            AgentEvent(type=TICKET_CREATED, run_id="looper", source="pm"), settings)
        if emitted:
            break

    assert emitted, "the breaker should have fired"
    assert emitted[0].type == RUN_ESCALATED
    assert "circuit breaker" in emitted[0].payload["reason"]


def test_the_breaker_fires_only_once(supervisor_env):
    """Otherwise the abort itself becomes the loop."""
    from scrum.events import AgentEvent, TICKET_CREATED
    from scrum.handlers import handle_supervisor

    settings = Settings(backend="local", max_events_per_run=3)
    event = AgentEvent(type=TICKET_CREATED, run_id="looper2", source="pm")

    fired = sum(1 for _ in range(10) if handle_supervisor(event, settings))
    assert fired == 1


def test_late_events_for_a_finished_run_are_ignored(supervisor_env):
    """
    SNS delivers at least once, so a duplicate after a run completes is routine
    — not a reason to restart the pipeline.
    """
    from scrum.events import AgentEvent, QA_FAILED, QA_PASSED
    from scrum.handlers import handle_supervisor

    settings = Settings(backend="local", max_attempts=3)
    run = "finished-run"

    passed = handle_supervisor(
        AgentEvent(type=QA_PASSED, run_id=run, attempt=1, source="qa"), settings)
    assert passed[0].type == "run.completed"

    handle_supervisor(passed[0], settings)          # supervisor marks it terminal
    late = handle_supervisor(
        AgentEvent(type=QA_FAILED, run_id=run, attempt=1, source="qa"), settings)

    assert late == [], "a late qa.failed must not re-open a completed run"


# ── DynamoDB run state ────────────────────────────────────────────────────────
# These exist because of a bug that reached production: `put` attached
# ConditionExpression="attribute_not_exists(run_id)" even when the caller had
# NOT pinned a version, which means "only write if this run has never been
# written". Every unpinned update to an existing item therefore failed, and the
# supervisor errored on every single event. The local store treats
# expected_version=None as "no check", so the suite stayed green while the
# deployed system could not update a row.

def _stubbed_dynamo():
    import boto3
    from botocore.stub import Stubber

    client = boto3.client("dynamodb", region_name="us-east-1",
                          aws_access_key_id="t", aws_secret_access_key="t")
    return client, Stubber(client)


def _store(client):
    from scrum.config import Settings
    from scrum.store.aws import DynamoRunStateStore

    return DynamoRunStateStore(Settings(state_table="runs"), dynamo_client=client)


def test_unpinned_write_sends_no_condition():
    """An unpinned write is last-write-wins and must always apply."""
    client, stub = _stubbed_dynamo()
    stub.add_response("get_item", {"Item": {
        "run_id": {"S": "r1"}, "state": {"S": '{"attempt": 1}'}, "version": {"N": "3"}}})
    stub.add_response("update_item", {}, expected_params={
        "TableName": "runs",
        "Key": {"run_id": {"S": "r1"}},
        "UpdateExpression": "SET #s = :state, version = :next",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {
            ":state": {"S": '{"attempt": 1, "events_seen": 2}'},
            ":next": {"N": "4"},
        },
        # No ConditionExpression key at all. Stubber fails the call if the
        # request carries one, which is exactly the regression we want caught.
    })
    stub.activate()

    result = _store(client).put("r1", {"events_seen": 2})
    assert result["version"] == 4
    stub.assert_no_pending_responses()


def test_pinned_write_does_send_a_condition():
    """A pinned write must still be guarded, or concurrent retries race."""
    client, stub = _stubbed_dynamo()
    stub.add_response("get_item", {"Item": {
        "run_id": {"S": "r1"}, "state": {"S": '{"attempt": 1}'}, "version": {"N": "2"}}})
    stub.add_response("update_item", {}, expected_params={
        "TableName": "runs",
        "Key": {"run_id": {"S": "r1"}},
        "UpdateExpression": "SET #s = :state, version = :next",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {
            ":state": {"S": '{"attempt": 2}'},
            ":next": {"N": "3"},
            ":expected": {"N": "2"},
        },
        "ConditionExpression": "attribute_not_exists(run_id) OR version = :expected",
    })
    stub.activate()

    _store(client).put("r1", {"attempt": 2}, expected_version=2)
    stub.assert_no_pending_responses()


def test_losing_a_pinned_race_raises_concurrent_update():
    from scrum.store.base import ConcurrentUpdate

    client, stub = _stubbed_dynamo()
    stub.add_response("get_item", {"Item": {
        "run_id": {"S": "r1"}, "state": {"S": "{}"}, "version": {"N": "5"}}})
    stub.activate()

    with pytest.raises(ConcurrentUpdate):
        _store(client).put("r1", {"attempt": 2}, expected_version=2)


def test_repeated_unpinned_writes_all_succeed():
    """
    The exact deployed failure: supervisor writes events_seen on every event.
    The second one must not be rejected just because the item now exists.
    """
    client, stub = _stubbed_dynamo()
    for version in (1, 2, 3):
        stub.add_response("get_item", {"Item": {
            "run_id": {"S": "r1"}, "state": {"S": "{}"}, "version": {"N": str(version)}}})
        stub.add_response("update_item", {})
    stub.activate()

    store = _store(client)
    for n in (1, 2, 3):
        store.put("r1", {"events_seen": n})     # must not raise
    stub.assert_no_pending_responses()
