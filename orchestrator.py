"""
orchestrator.py — the command line entry point.

The pipeline logic used to live here. It now lives in handlers.py (what each
agent does) and runtime.py (what drives them), because those two need to be
callable from a Lambda as well as from a terminal. What is left here is
argument parsing and printing.

    python orchestrator.py                 run one full cycle, block until done
    python orchestrator.py --backends      show the AWS probe and exit
    python orchestrator.py --publish       publish run.requested and exit
    python orchestrator.py --agent dev     run one agent as a long-lived consumer
    python orchestrator.py --dlq           inspect the dead letter queue

The `--agent` mode is the useful hybrid: point it at a deployed stack by
exporting SCRUM_TOPIC_ARN and friends, and this process becomes a consumer of
the real SQS queue — the same code a Lambda would run, but with a debugger
attached and logs in your terminal instead of CloudWatch.
"""

from __future__ import annotations

import argparse
import sys

from scrum.config import SETTINGS, resolve_backends
from scrum.runtime import publish_run_request, run_pipeline
from scrum import ui_state


def _print_banner() -> None:
    decision = resolve_backends(SETTINGS)
    ui_state.set_backend(decision.mode, decision.reason)
    print("\n=== AGENTIC SCRUM ===")
    print(decision.banner())
    print()


def _run_single_agent(name: str) -> int:
    """
    Run one agent as a standalone consumer, forever.

    This is what a Lambda does, minus the Lambda. Against the local bus it is
    not much use on its own (nothing else is publishing); against a deployed
    stack it is the fastest way to debug an agent, because you get a real
    traceback instead of a CloudWatch entry.
    """
    from scrum.bus import get_bus
    from scrum.handlers import AGENT_HANDLERS

    if name not in AGENT_HANDLERS:
        print(f"Unknown agent '{name}'. Choose from: {', '.join(AGENT_HANDLERS)}")
        return 1

    _print_banner()
    bus = get_bus(SETTINGS)
    handler = AGENT_HANDLERS[name]
    print(f"[{name}] consuming its queue — ctrl-c to stop\n")

    try:
        while True:
            for message in bus.receive(name, wait_seconds=SETTINGS.long_poll_seconds):
                event = message.event
                print(f"[{name}] <- {event.describe()}")
                try:
                    emitted = handler(event, SETTINGS) or []
                except Exception as exc:  # noqa: BLE001
                    print(f"[{name}] handler raised {type(exc).__name__}: {exc} — not acking")
                    bus.nack(message)
                    continue
                for outgoing in emitted:
                    bus.publish(outgoing)
                    print(f"[{name}] -> {outgoing.describe()}")
                bus.ack(message)
    except KeyboardInterrupt:
        print(f"\n[{name}] stopped")
        return 0


def _show_dlq() -> int:
    """What got stuck. On a healthy system this is empty."""
    from scrum.bus import get_bus

    bus = get_bus(SETTINGS)
    if bus.mode == "local":
        letters = getattr(bus, "dead_letters", [])
        if not letters:
            print("Dead letter queue is empty.")
            return 0
        print(f"{len(letters)} dead letter(s):")
        for event in letters:
            print(f"  {event.describe()}")
        return 0

    print("On AWS, inspect the DLQ with:")
    print("  aws sqs receive-message --queue-url $(aws sqs get-queue-url "
          "--queue-name agentic-scrum-dlq --query QueueUrl --output text) "
          "--max-number-of-messages 10")
    return 0


def main() -> int:
    try:
        return _main()
    except RuntimeError as exc:
        # The one RuntimeError we raise deliberately: AGENTIC_SCRUM_BACKEND=aws
        # with a failed probe. A one-line message beats a traceback.
        print(f"\n{exc}\n")
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


def _main() -> int:
    parser = argparse.ArgumentParser(description="Agentic Scrum — multi-agent bug fixing")
    parser.add_argument("--backends", action="store_true",
                        help="show which backend the probe chose, and why")
    parser.add_argument("--publish", action="store_true",
                        help="publish run.requested and exit without waiting")
    parser.add_argument("--agent", metavar="NAME",
                        help="run one agent (pm/dev/qa/supervisor) as a consumer")
    parser.add_argument("--dlq", action="store_true", help="inspect the dead letter queue")
    parser.add_argument("--timeout", type=float, default=600,
                        help="give up on a run after this many seconds (default: 600)")
    args = parser.parse_args()

    if args.backends:
        _print_banner()
        return 0

    if args.dlq:
        return _show_dlq()

    if args.agent:
        return _run_single_agent(args.agent)

    if args.publish:
        _print_banner()
        run_id = publish_run_request(SETTINGS)
        print(f"Published run.requested for run {run_id}.")
        print("Nothing is waiting on it — the agents pick it up from the queue.")
        return 0

    outcome = run_pipeline(SETTINGS, timeout=args.timeout)
    return 0 if outcome.get("verdict") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
