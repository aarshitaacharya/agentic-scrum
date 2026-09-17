#!/usr/bin/env python3
"""
infra/smoke_test.py — drive one real run through the deployed stack.

This is the test that the README cannot fake: it publishes to the real SNS
topic, lets the real Lambdas do the work, polls DynamoDB for the verdict, then
pulls the artifacts out of S3 and *runs the patched file locally* to check the
bug is genuinely fixed rather than merely declared fixed.

    python infra/smoke_test.py                    # uses workspace/buggy_script.py
    python infra/smoke_test.py path/to/file.py    # or your own

It is read-mostly and cheap: one run is a handful of Lambda invocations and a
few dozen KB of S3. Expect two to four minutes, most of it waiting on Gemini.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REGION = os.environ.get("AWS_REGION", "us-east-1")
STACK = os.environ.get("STACK_NAME", "agentic-scrum")
TERMINAL = {"passed", "escalated", "aborted"}


def stack_outputs(cfn) -> dict:
    try:
        stacks = cfn.describe_stacks(StackName=STACK)["Stacks"]
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not read stack '{STACK}' in {REGION}: {exc}\n"
                 f"Deploy it first: ./infra/deploy.sh")
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end test against the deployed stack")
    parser.add_argument("file", nargs="?", default="workspace/buggy_script.py",
                        help="the buggy Python file to fix")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()

    if not os.path.exists(args.file):
        sys.exit(f"No such file: {args.file}")

    import boto3

    from scrum.events import AgentEvent, RUN_REQUESTED, new_run_id
    from scrum.store.base import BUGGY_SCRIPT, PATCHED, QA_REVIEW, TICKET, TRACE

    cfn = boto3.client("cloudformation", region_name=REGION)
    outputs = stack_outputs(cfn)
    topic = outputs["TopicArn"]
    bucket = outputs["ArtifactBucket"]

    s3 = boto3.client("s3", region_name=REGION)
    sns = boto3.client("sns", region_name=REGION)
    dynamo = boto3.client("dynamodb", region_name=REGION)
    table = os.environ.get("SCRUM_STATE_TABLE", "agentic-scrum-runs")

    run_id = new_run_id()
    source = open(args.file).read()

    print(f"\n{'=' * 62}")
    print("  AGENTIC SCRUM — live smoke test")
    print(f"{'=' * 62}")
    print(f"  stack   {STACK} ({REGION})")
    print(f"  run_id  {run_id}")
    print(f"  file    {args.file} ({len(source.splitlines())} lines)")
    print(f"{'=' * 62}\n")

    # ── 1. Upload the work item. The event carries a pointer, not the file. ──
    key = f"runs/{run_id}/{BUGGY_SCRIPT}"
    s3.put_object(Bucket=bucket, Key=key, Body=source.encode(),
                  ContentType="text/plain; charset=utf-8")
    print(f"[1/4] uploaded s3://{bucket}/{key}")

    # ── 2. Kick it off. Nothing waits on this; the Lambdas take over. ──
    event = AgentEvent(type=RUN_REQUESTED, run_id=run_id, attempt=1,
                       source="smoke-test", payload={"artifact": BUGGY_SCRIPT})
    sns.publish(TopicArn=topic, Subject=RUN_REQUESTED, Message=event.to_json(),
                MessageAttributes=event.message_attributes())
    print(f"[2/4] published run.requested to {topic.split(':')[-1]}")

    # ── 3. Poll for the verdict. ──
    print(f"[3/4] waiting (up to {args.timeout:.0f}s)...\n")
    started = time.time()
    last = None
    state: dict = {}

    while time.time() - started < args.timeout:
        try:
            item = dynamo.get_item(TableName=table, Key={"run_id": {"S": run_id}},
                                   ConsistentRead=True).get("Item")
        except Exception as exc:  # noqa: BLE001
            print(f"      (dynamo read failed: {exc})")
            item = None

        if item:
            state = json.loads(item["state"]["S"])
            summary = (f"status={state.get('status', '?'):10} "
                       f"attempt={state.get('attempt', '?')} "
                       f"events={state.get('events_seen', '?')}")
            if summary != last:
                print(f"      [{time.time() - started:5.0f}s] {summary}")
                last = summary
            if state.get("status") in TERMINAL:
                break
        time.sleep(5)

    elapsed = time.time() - started
    status = state.get("status", "unknown")

    if status not in TERMINAL:
        print(f"\n  TIMED OUT after {elapsed:.0f}s — last seen: {state or 'nothing in DynamoDB'}")
        print("\n  Where to look:")
        print(f"    aws logs tail /aws/lambda/agentic-scrum-pm --since 15m --region {REGION}")
        print(f"    aws sqs get-queue-attributes --queue-url $(aws sqs get-queue-url "
              f"--queue-name agentic-scrum-dlq --query QueueUrl --output text --region {REGION}) "
              f"--attribute-names ApproximateNumberOfMessages --region {REGION}")
        return 1

    # ── 4. Pull the artifacts and check the work. ──
    print(f"\n[4/4] finished in {elapsed:.0f}s — status: {status}\n")

    def fetch(name: str) -> str:
        try:
            return s3.get_object(Bucket=bucket, Key=f"runs/{run_id}/{name}")["Body"].read().decode()
        except Exception:  # noqa: BLE001
            return ""

    ticket, patched, review, trace = (fetch(n) for n in (TICKET, PATCHED, QA_REVIEW, TRACE))

    print("-" * 62)
    print("TICKET (from the PM agent)")
    print("-" * 62)
    print(ticket.strip() or "(none produced)")

    print()
    print("-" * 62)
    print("QA REVIEW")
    print("-" * 62)
    print(review.strip() or "(none produced)")

    if trace:
        steps = [json.loads(line) for line in trace.splitlines() if line.strip()]
        print()
        print("-" * 62)
        print(f"REACT TRACE — {len(steps)} steps")
        print("-" * 62)
        for step in steps:
            if step.get("action"):
                first = (step.get("thought") or "").split("\n")[0][:64]
                print(f"  [{step['agent']:3}] {step['action']:<14} {first}")

    # The part that cannot be faked: run the output.
    verdict = 1
    if patched:
        print()
        print("-" * 62)
        print("RUNNING THE PATCHED FILE LOCALLY")
        print("-" * 62)
        tmp = f"/tmp/smoke-{run_id}.py"
        with open(tmp, "w") as fh:
            fh.write(patched)
        result = subprocess.run([sys.executable, tmp], capture_output=True, text=True, timeout=30)
        print(result.stdout or "(no stdout)")
        if result.stderr:
            print("stderr:", result.stderr[:500])
        print(f"exit code: {result.returncode}")
        os.remove(tmp)
        verdict = 0 if (result.returncode == 0 and status == "passed") else 1
    else:
        print("\n  No patched file was produced.")

    print()
    print("=" * 62)
    print(f"  {'PASS' if verdict == 0 else 'FAIL'} — status={status}, "
          f"attempts={state.get('attempt')}, {elapsed:.0f}s")
    print(f"  artifacts: aws s3 ls s3://{bucket}/runs/{run_id}/")
    print("=" * 62)
    print()
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
