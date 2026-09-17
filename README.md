# Agentic Scrum Office

> Three LLM agents — PM, Dev and QA — triage, patch and verify bugs in Python
> files, handing work to each other over an event bus. You watch them do it in
> a pixel-art office.

Runs two ways from one codebase: **on AWS** (SNS → SQS → Lambda), or **locally**
with an in-process bus that reproduces the same queue semantics. It picks
automatically.

Deployed and verified on AWS. See [Status](#status).

---

## Demo

https://github.com/user-attachments/assets/ad9e88f4-7e0a-479e-802c-19c6aaeb086f

---

## Quick start

```bash
pip install -r requirements.txt
echo 'GEMINI_API_KEY=your-key' > .env     # free: https://aistudio.google.com/app/apikey
python server.py                          # -> http://localhost:8000
```

Drop a `.py` file, hit **RUN**. No AWS account needed — it falls back to the
local bus and tells you so in the status bar.

---

## What it does

| Agent | Character | Job |
|---|---|---|
| **PM** | Michael | Reads the file, **runs it**, writes a ticket citing what it observed |
| **Dev** | Jim | Patches the code, runs the patch, critiques its own work before handing off |
| **QA** | Dwight | Reads the diff, **runs the patched file**, returns PASS or a specific FAIL |

A FAIL goes back to Dev with the objection attached. After three failed cycles
the run escalates instead of looping.

What makes them agents rather than three prompts in a row: **they use tools.**
The PM's ticket quotes a real traceback. QA's verdict comes from executing the
patch, not from reasoning about whether it looks right.

---

## Architecture

Two tiers. The supervisor decides *who runs next*; the workers just work.

```
  human ──run.requested──▶ PM
                            └──ticket.created──▶ DEV
                                                  └──patch.submitted──▶ QA
                                                                         │
                        ┌────────────────────────────────────────────────┤
                  qa.passed                                         qa.failed
                        ▼                                                ▼
                  SUPERVISOR                                       SUPERVISOR
                        │                                       (checks budget)
                 run.completed                             ┌───────────┴──────────┐
                                                    budget left            budget spent
                                                           │                      │
                                                   retry.requested          run.escalated
                                                           └──▶ DEV (again)
```

Dev does **not** subscribe to `qa.failed`. A rejection goes to the supervisor,
which checks the retry budget and only then re-tasks Dev. The budget is policy,
and policy lives one tier up.

The ReAct loop is written out in [`scrum/agents/react.py`](scrum/agents/react.py)
rather than imported, so every Thought/Action/Observation is inspectable — that
is what drives the office narration and `trace.jsonl`.

Only Dev can write. A reviewer that can edit the code under review is not a
reviewer. Enforced twice: by the tool dict in code, and by IAM on AWS.

---

## Running on AWS

```bash
export GEMINI_API_KEY=...
./infra/preflight.sh      # read-only checks, costs nothing
./infra/deploy.sh         # ~8 minutes
python infra/smoke_test.py
./infra/teardown.sh       # deletes everything, then proves it
```

Needs the AWS CLI and SAM CLI. Install SAM with **pip, not Homebrew** — on an
Intel Mac, brew builds rust and llvm from source, which takes hours:

```bash
python3 -m venv ~/.sam-cli-venv
~/.sam-cli-venv/bin/pip install aws-sam-cli
ln -sf ~/.sam-cli-venv/bin/sam /usr/local/bin/sam
```

**Cost:** a week of testing is under $1. Lambda, SNS, SQS and CloudWatch all
sit in always-free tiers at this volume; the only guaranteed charge is Secrets
Manager at $0.40/month. No VPC and no NAT gateway anywhere — that is the usual
way a hobby project becomes $32/month.

Guardrails: a budget alarm, 7-day log retention, a DLQ alarm, and a circuit
breaker that aborts a run after 60 events.

---

## Local fallback

The AWS path and the local path are two implementations of one interface. A
probe picks: `boto3` importable → resources configured → STS answers. Any
failure falls back and records why.

```bash
python orchestrator.py --backends   # show the decision and every check
curl localhost:8000/backends        # same, as JSON
```

| | On AWS | Fallback |
|---|---|---|
| Event bus | SNS + 4 SQS queues | in-process queues |
| Compute | 1 Lambda per agent | 1 thread per agent |
| Artifacts | S3 | `workspace/` |
| Run state | DynamoDB | a JSON file |

The fallback is a simulator, not a shortcut: it reproduces fan-out filtering,
visibility timeouts, redelivery and dead-letter queues, so a handler written
locally survives the swap.

---

## Layout

```
server.py            entry: the office UI
orchestrator.py      entry: the CLI
lambda_handlers.py   entry: AWS Lambda

scrum/
  config.py          the AWS probe and the fallback decision
  events.py          event vocabulary + routing table
  handlers.py        what each agent does — transport-agnostic
  runtime.py         local adapter: a thread per queue
  agents/            ReAct loop, tools, reflection, the three workers
  bus/               SNS+SQS, or in-process
  store/             S3+DynamoDB, or the workspace directory

infra/               template.yaml, preflight/deploy/teardown, smoke_test
tests/               61 tests, no credentials, no API calls
```

`handlers.py` knows nothing about how it was invoked — `runtime.py` and
`lambda_handlers.py` are both thin adapters over it. That is why the fallback
is credible rather than a parallel codebase.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

61 tests, no credentials and no API calls. The AWS paths are driven through
botocore's `Stubber`, which asserts the exact API parameters — filter
attributes, SNS envelope handling, DynamoDB conditions — without an account.

---

## Status

Deployed and verified on AWS (`us-east-1`, 17 Sep 2026). Run
`1789678748-f1aa7c`: a full PM → Dev → QA cycle through a real SNS topic, four
SQS queues and six Lambdas, **passing in 46 seconds** on the first attempt.

Two bugs reached production during testing — a DynamoDB conditional-write
default that differed from the local store, and an IAM policy that was too
strict in the wrong way. Both are now covered by regression tests (see
`tests/test_aws_readiness.py` and `tests/test_iam_policy.py`).

Known limits:
- Agent quality is non-deterministic. In one local run the PM found 3 of 4
  seeded bugs; on AWS it found all three ticketed ones and QA wrote its own
  negative-number and empty-list cases unprompted.
- Code execution is sandboxed (workspace-only, timeout, minimal environment) but
  is **not** a security boundary for untrusted code.
- One run at a time locally by design; on AWS, S3 keys are namespaced per run.

---

## What's next

- A Docs or Security agent — should be a new subscription and nothing else.
  That is the real test of whether the event design earned its keep.
- Replace QA's judgement with a generated test suite, so PASS means "tests went
  green", not "the reviewer was convinced".
- Step Functions for the supervisor: the retry budget is a state machine, and
  it is currently a state machine written in Python.
