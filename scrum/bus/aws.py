"""
bus/aws.py — the real thing: one SNS topic, one SQS queue per agent.

Topology
--------

                        +---------------------------+
    publish ----------> |  SNS topic: agentic-scrum |
                        +------------+--------------+
                                     | filter policy on the `event_type`
                                     | message attribute
             +-----------------------+-----------------------+
             v                       v                       v
        sqs: ...-pm            sqs: ...-dev            sqs: ...-qa
        {run.requested}   {ticket.created, qa.failed}  {patch.submitted}
             |                       |                       |
             v                       v                       v
        Lambda pm_handler       Lambda dev_handler      Lambda qa_handler

Each queue has a redrive policy pointing at a shared DLQ. The supervisor has
its own queue subscribed to every event type; it is the only stateful consumer.

Why a topic in front of the queues, rather than agents writing to each other's
queues directly: the publisher then has to know the full subscriber list, and
adding a fifth agent means editing and redeploying the other four. With a
topic, adding a Security agent is a new queue plus a filter policy — no
existing code changes. That is the whole reason this is pub/sub and not RPC.

Everything here is written to survive the two things that actually bite in
production: SNS delivers *at least once* (so handlers must be idempotent, which
is why events carry `event_id`), and SQS gives no ordering guarantee on a
standard queue (so the supervisor treats `attempt` as authoritative rather than
trusting arrival order).
"""

from __future__ import annotations

import json

from scrum.bus.base import EventBus, ReceivedEvent
from scrum.events import AgentEvent


class SnsSqsEventBus(EventBus):
    """EventBus backed by SNS fan-out into per-agent SQS queues."""

    mode = "aws"

    def __init__(self, settings, sns_client=None, sqs_client=None):
        import boto3
        from botocore.config import Config as BotoConfig

        self.settings = settings
        self.topic_arn = settings.topic_arn

        # Long polling means read_timeout has to exceed WaitTimeSeconds, or
        # every poll dies on a socket timeout instead of returning empty.
        cfg = BotoConfig(
            connect_timeout=5,
            read_timeout=settings.long_poll_seconds + 10,
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self.sns = sns_client or boto3.client("sns", region_name=settings.region, config=cfg)
        self.sqs = sqs_client or boto3.client("sqs", region_name=settings.region, config=cfg)
        self._published = 0

    # ── publish ───────────────────────────────────────────────────────────────

    def publish(self, event: AgentEvent) -> str:
        """
        One publish, N deliveries. The filter policies on each subscription
        decide who actually gets it — see message_attributes() in events.py.
        """
        response = self.sns.publish(
            TopicArn=self.topic_arn,
            Subject=event.type[:99],                 # SNS caps Subject at 100
            Message=event.to_json(),
            MessageAttributes=event.message_attributes(),
        )
        self._published += 1
        return response["MessageId"]

    # ── receive ───────────────────────────────────────────────────────────────

    def receive(self, subscriber: str, wait_seconds: int = 5,
                max_messages: int = 1) -> list[ReceivedEvent]:
        queue_url = self._queue_url(subscriber)
        response = self.sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=max(1, min(max_messages, 10)),
            # Long polling: hold the connection open instead of returning empty
            # immediately. Short polling here would mean ~20x the SQS requests
            # for the same latency, and SQS bills per request.
            WaitTimeSeconds=max(0, min(wait_seconds, 20)),
            VisibilityTimeout=self.settings.visibility_timeout,
            MessageAttributeNames=["All"],
            AttributeNames=["ApproximateReceiveCount"],
        )

        out: list[ReceivedEvent] = []
        for message in response.get("Messages", []):
            event = self._unwrap(message["Body"])
            if event is None:
                # Unparseable: let it redeliver and eventually hit the DLQ
                # rather than silently deleting evidence of a bug.
                continue
            count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", 1))
            out.append(ReceivedEvent(
                event=event,
                receipt=message["ReceiptHandle"],
                subscriber=subscriber,
                receive_count=count,
            ))
        return out

    @staticmethod
    def _unwrap(body: str) -> AgentEvent | None:
        """
        Handle both delivery shapes.

        With RawMessageDelivery=false (the default) SNS wraps our JSON in its
        own envelope, so the payload is body["Message"] — a JSON string inside
        a JSON string. With RawMessageDelivery=true the body *is* our JSON.
        The SAM template turns raw delivery on, but accepting both means a
        console-created subscription does not silently break the consumer.
        """
        try:
            outer = json.loads(body)
        except json.JSONDecodeError:
            return None

        if isinstance(outer, dict) and "Message" in outer and "TopicArn" in outer:
            try:
                return AgentEvent.from_json(outer["Message"])
            except (json.JSONDecodeError, TypeError):
                return None
        try:
            return AgentEvent(**outer)
        except TypeError:
            return None

    # ── ack / nack ────────────────────────────────────────────────────────────

    def ack(self, received: ReceivedEvent) -> None:
        """Delete only after the work is durable — otherwise a crash loses it."""
        self.sqs.delete_message(
            QueueUrl=self._queue_url(received.subscriber),
            ReceiptHandle=received.receipt,
        )

    def nack(self, received: ReceivedEvent) -> None:
        """
        Hand it straight back instead of waiting out the visibility timeout.
        Setting the timeout to 0 makes it immediately visible to another
        consumer, and bumps ApproximateReceiveCount so the DLQ still catches a
        message that keeps failing.
        """
        self.sqs.change_message_visibility(
            QueueUrl=self._queue_url(received.subscriber),
            ReceiptHandle=received.receipt,
            VisibilityTimeout=0,
        )

    def extend_visibility(self, received: ReceivedEvent, seconds: int) -> None:
        """
        Heartbeat for a slow agent turn. An LLM call that outruns the
        visibility timeout gets its message redelivered underneath it and the
        work runs twice; extending mid-flight is the cheap fix.
        """
        self.sqs.change_message_visibility(
            QueueUrl=self._queue_url(received.subscriber),
            ReceiptHandle=received.receipt,
            VisibilityTimeout=seconds,
        )

    # ── helpers ───────────────────────────────────────────────────────────────

    def _queue_url(self, subscriber: str) -> str:
        url = self.settings.queue_url(subscriber)
        if url:
            return url
        # Last resort: resolve by convention. Costs one API call, cached by the
        # settings lookup above on subsequent calls in a warm Lambda.
        return self.sqs.get_queue_url(QueueName=f"agentic-scrum-{subscriber}")["QueueUrl"]

    def stats(self) -> dict:
        depths = {}
        for name in ("pm", "dev", "qa", "supervisor"):
            try:
                attrs = self.sqs.get_queue_attributes(
                    QueueUrl=self._queue_url(name),
                    AttributeNames=["ApproximateNumberOfMessages"],
                )
                depths[name] = int(attrs["Attributes"]["ApproximateNumberOfMessages"])
            except Exception:  # noqa: BLE001 — stats must never break a run
                depths[name] = None
        return {"mode": self.mode, "published": self._published,
                "topic": self.topic_arn, "depths": depths}
