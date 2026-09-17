#!/usr/bin/env python3
"""
infra/check_routing.py — assert the deployed routing matches the code's routing.

There are two copies of the routing table: SUBSCRIPTIONS in events.py, which
the local bus uses, and the FilterPolicy blocks in template.yaml, which SNS
uses. Two copies of anything drift, and this particular drift is nasty: the
pipeline works perfectly on a laptop and then stalls silently in production,
because a queue is subscribed to an event nobody publishes — or worse, is
missing a subscription and its agent simply never wakes up. Nothing errors.
The run just stops.

So: fail the build instead. Run this in CI, before `sam deploy`.

    python infra/check_routing.py
"""

from __future__ import annotations

import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrum.events import ALL_EVENTS, SUBSCRIPTIONS  # noqa: E402

TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.yaml")

# Map the CloudFormation logical resource name back to our subscriber name.
SUBSCRIPTION_RESOURCES = {
    "PmSubscription": "pm",
    "DevSubscription": "dev",
    "QaSubscription": "qa",
    "SupervisorSubscription": "supervisor",
}


class CloudFormationLoader(yaml.SafeLoader):
    """SafeLoader that does not choke on !Ref / !GetAtt / !Sub."""


def _ignore_intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return {f"!{tag_suffix}": loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {f"!{tag_suffix}": loader.construct_sequence(node)}
    return {f"!{tag_suffix}": loader.construct_mapping(node)}


CloudFormationLoader.add_multi_constructor("!", _ignore_intrinsic)


def main() -> int:
    with open(TEMPLATE) as fh:
        template = yaml.load(fh, Loader=CloudFormationLoader)

    resources = template.get("Resources", {})
    problems: list[str] = []

    for resource_name, subscriber in SUBSCRIPTION_RESOURCES.items():
        resource = resources.get(resource_name)
        if resource is None:
            problems.append(f"{resource_name} is missing from template.yaml entirely")
            continue

        deployed = set(resource["Properties"].get("FilterPolicy", {}).get("event_type", []))
        in_code = set(SUBSCRIPTIONS.get(subscriber, []))

        only_deployed = deployed - in_code
        only_in_code = in_code - deployed

        if only_deployed:
            problems.append(
                f"{subscriber}: template subscribes to {sorted(only_deployed)}, "
                f"but events.py does not — the queue will fill with messages "
                f"no handler expects"
            )
        if only_in_code:
            problems.append(
                f"{subscriber}: events.py expects {sorted(only_in_code)}, but the "
                f"template does not subscribe — on AWS this agent never wakes up "
                f"for those events and the run stalls silently"
            )

    # Every event must have at least one listener, or publishing it is a no-op.
    for event in ALL_EVENTS:
        if not any(event in types for types in SUBSCRIPTIONS.values()):
            problems.append(f"{event} is published by nobody and heard by nobody")

    if problems:
        print("Routing mismatch between events.py and infra/template.yaml:\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("Routing OK — events.py and template.yaml agree:")
    for subscriber, types in SUBSCRIPTIONS.items():
        print(f"  {subscriber:11} <- {', '.join(types)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
