"""
The SNS/SQS bus, tested without AWS.

botocore's Stubber intercepts calls at the client layer and asserts the exact
parameters we send, so this verifies the real API contract — the filter
attributes, the SNS envelope handling, the delete-on-ack — without credentials,
a deployed stack, or a bill. It is how the AWS half of this project stays
honest while the AWS account is not available.
"""

import json

import boto3
import pytest
from botocore.stub import ANY, Stubber

from scrum.bus.aws import SnsSqsEventBus
from scrum.config import Settings
from scrum.events import AgentEvent, TICKET_CREATED

TOPIC = "arn:aws:sns:us-east-1:123456789012:agentic-scrum-events"
QUEUES = {
    "pm": "https://sqs.us-east-1.amazonaws.com/123456789012/agentic-scrum-pm",
    "dev": "https://sqs.us-east-1.amazonaws.com/123456789012/agentic-scrum-dev",
    "qa": "https://sqs.us-east-1.amazonaws.com/123456789012/agentic-scrum-qa",
    "supervisor": "https://sqs.us-east-1.amazonaws.com/123456789012/agentic-scrum-supervisor",
}


@pytest.fixture
def wired():
    """A bus with both clients stubbed."""
    settings = Settings(backend="aws", region="us-east-1", topic_arn=TOPIC,
                        bucket="test-bucket", queue_urls=dict(QUEUES))
    credentials = {"aws_access_key_id": "test", "aws_secret_access_key": "test"}
    sns = boto3.client("sns", region_name="us-east-1", **credentials)
    sqs = boto3.client("sqs", region_name="us-east-1", **credentials)
    sns_stub, sqs_stub = Stubber(sns), Stubber(sqs)
    sns_stub.activate()
    sqs_stub.activate()
    yield SnsSqsEventBus(settings, sns_client=sns, sqs_client=sqs), sns_stub, sqs_stub
    sns_stub.deactivate()
    sqs_stub.deactivate()


def test_publish_sends_the_filter_attributes(wired):
    """
    The filter policy matches on `event_type`. If this attribute is missing or
    misnamed, SNS matches nothing, every queue stays empty, and the pipeline
    stalls without a single error being raised anywhere.
    """
    bus, sns_stub, _ = wired
    event = AgentEvent(type=TICKET_CREATED, run_id="run-1", attempt=1, source="pm")

    sns_stub.add_response(
        "publish",
        {"MessageId": "msg-123"},
        expected_params={
            "TopicArn": TOPIC,
            "Subject": TICKET_CREATED,
            "Message": ANY,
            "MessageAttributes": {
                "event_type": {"DataType": "String", "StringValue": TICKET_CREATED},
                "run_id": {"DataType": "String", "StringValue": "run-1"},
                "source": {"DataType": "String", "StringValue": "pm"},
            },
        },
    )

    assert bus.publish(event) == "msg-123"
    sns_stub.assert_no_pending_responses()


def test_receive_long_polls_the_right_queue(wired):
    bus, _, sqs_stub = wired
    event = AgentEvent(type=TICKET_CREATED, run_id="run-1", source="pm")

    sqs_stub.add_response(
        "receive_message",
        {"Messages": [{
            "MessageId": "m1",
            "ReceiptHandle": "receipt-abc",
            "Body": event.to_json(),
            "Attributes": {"ApproximateReceiveCount": "1"},
        }]},
        expected_params={
            "QueueUrl": QUEUES["dev"],
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 5,
            "VisibilityTimeout": ANY,
            "MessageAttributeNames": ["All"],
            "AttributeNames": ["ApproximateReceiveCount"],
        },
    )

    received = bus.receive("dev", wait_seconds=5)
    assert len(received) == 1
    assert received[0].event.type == TICKET_CREATED
    assert received[0].receipt == "receipt-abc"
    sqs_stub.assert_no_pending_responses()


def test_receive_unwraps_the_sns_envelope(wired):
    """
    With RawMessageDelivery off, SNS wraps our JSON in its own envelope. A
    subscription created by hand in the console defaults to off, so the
    consumer must handle both shapes or it silently drops everything.
    """
    bus, _, sqs_stub = wired
    inner = AgentEvent(type=TICKET_CREATED, run_id="run-9", source="pm").to_json()
    envelope = json.dumps({"Type": "Notification", "TopicArn": TOPIC, "Message": inner})

    sqs_stub.add_response(
        "receive_message",
        {"Messages": [{"MessageId": "m1", "ReceiptHandle": "r1", "Body": envelope,
                       "Attributes": {"ApproximateReceiveCount": "1"}}]},
        expected_params={"QueueUrl": ANY, "MaxNumberOfMessages": ANY, "WaitTimeSeconds": ANY,
                         "VisibilityTimeout": ANY, "MessageAttributeNames": ANY,
                         "AttributeNames": ANY},
    )

    received = bus.receive("qa", wait_seconds=1)
    assert received[0].event.run_id == "run-9"


def test_undecodable_message_is_skipped_not_deleted(wired):
    """
    Garbage stays on the queue so it redelivers into the DLQ. Deleting it would
    destroy the only evidence of whatever published it.
    """
    bus, _, sqs_stub = wired
    sqs_stub.add_response(
        "receive_message",
        {"Messages": [{"MessageId": "m1", "ReceiptHandle": "r1", "Body": "<html>nope</html>",
                       "Attributes": {"ApproximateReceiveCount": "1"}}]},
        expected_params={"QueueUrl": ANY, "MaxNumberOfMessages": ANY, "WaitTimeSeconds": ANY,
                         "VisibilityTimeout": ANY, "MessageAttributeNames": ANY,
                         "AttributeNames": ANY},
    )

    assert bus.receive("dev", wait_seconds=1) == []


def test_ack_deletes_and_nack_zeroes_visibility(wired):
    bus, _, sqs_stub = wired
    event = AgentEvent(type=TICKET_CREATED, run_id="run-1", source="pm")

    sqs_stub.add_response("delete_message", {}, expected_params={
        "QueueUrl": QUEUES["dev"], "ReceiptHandle": "receipt-abc"})
    sqs_stub.add_response("change_message_visibility", {}, expected_params={
        "QueueUrl": QUEUES["dev"], "ReceiptHandle": "receipt-abc", "VisibilityTimeout": 0})

    from scrum.bus.base import ReceivedEvent

    received = ReceivedEvent(event=event, receipt="receipt-abc", subscriber="dev", receive_count=1)
    bus.ack(received)
    bus.nack(received)
    sqs_stub.assert_no_pending_responses()


def test_visibility_can_be_extended_mid_turn(wired):
    """An agent turn outrunning the visibility timeout gets run twice; this is the fix."""
    bus, _, sqs_stub = wired
    sqs_stub.add_response("change_message_visibility", {}, expected_params={
        "QueueUrl": QUEUES["qa"], "ReceiptHandle": "r", "VisibilityTimeout": 300})

    from scrum.bus.base import ReceivedEvent

    bus.extend_visibility(
        ReceivedEvent(event=AgentEvent(type=TICKET_CREATED, run_id="r"), receipt="r",
                      subscriber="qa", receive_count=1),
        300,
    )
    sqs_stub.assert_no_pending_responses()
