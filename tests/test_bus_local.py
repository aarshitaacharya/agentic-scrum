"""
The local bus has to behave like SQS, or the fallback is a lie.

These tests pin the four behaviours that change how a consumer must be
written. If any of them regress, code that passes locally will break on AWS.
"""

import time

import pytest

from scrum.bus.local import LocalEventBus
from scrum.events import AgentEvent, QA_FAILED, TICKET_CREATED, new_run_id


@pytest.fixture
def bus():
    return LocalEventBus(visibility_timeout=1, max_receives=2, log_path=None)


def _event(event_type=TICKET_CREATED):
    return AgentEvent(type=event_type, run_id=new_run_id(), source="test")


def test_fan_out_respects_the_filter_policy(bus):
    """ticket.created reaches dev and the supervisor — and nobody else."""
    bus.publish(_event(TICKET_CREATED))

    assert bus.receive("dev", wait_seconds=0)
    assert bus.receive("supervisor", wait_seconds=0)
    assert bus.receive("qa", wait_seconds=0) == []
    assert bus.receive("pm", wait_seconds=0) == []


def test_qa_failed_does_not_reach_dev(bus):
    """
    The retry budget lives in the supervisor. If Dev heard qa.failed directly
    it would retry forever, so this routing rule is load-bearing.
    """
    bus.publish(_event(QA_FAILED))

    assert bus.receive("supervisor", wait_seconds=0)
    assert bus.receive("dev", wait_seconds=0) == []


def test_ack_removes_the_message(bus):
    bus.publish(_event())
    received = bus.receive("dev", wait_seconds=0)[0]
    bus.ack(received)

    time.sleep(1.1)   # past the visibility timeout
    assert bus.receive("dev", wait_seconds=0) == []


def test_unacked_messages_redeliver(bus):
    """A handler that crashes must not lose the work."""
    bus.publish(_event())
    first = bus.receive("dev", wait_seconds=0)[0]
    assert first.receive_count == 1

    time.sleep(1.1)
    second = bus.receive("dev", wait_seconds=0)[0]
    assert second.receive_count == 2


def test_poison_messages_end_up_in_the_dlq(bus):
    """Redelivery is bounded: a message that always fails must stop looping."""
    bus.publish(_event())

    for _ in range(3):
        bus.receive("dev", wait_seconds=0)
        time.sleep(1.1)

    assert bus.receive("dev", wait_seconds=0) == []
    assert len(bus.dead_letters) == 1


def test_nack_redelivers_immediately(bus):
    bus.publish(_event())
    received = bus.receive("dev", wait_seconds=0)[0]
    bus.nack(received)

    again = bus.receive("dev", wait_seconds=0)
    assert again and again[0].receive_count == 2


def test_each_subscriber_gets_an_independent_copy(bus):
    """Acking on one queue must not affect the other — that is SNS fan-out."""
    bus.publish(_event())
    dev = bus.receive("dev", wait_seconds=0)[0]
    bus.ack(dev)

    supervisor = bus.receive("supervisor", wait_seconds=0)
    assert supervisor, "acking dev's copy should not consume the supervisor's"
