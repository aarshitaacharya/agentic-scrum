"""
store/aws.py — S3 for artifacts, DynamoDB for run state.

Two different stores because they are two different problems:

  S3        big, write-once blobs (source files, tickets, QA transcripts).
            Cheap per GB, keyed by run, versioned if you want an audit trail.

  DynamoDB  one small, hot, read-modify-write item per run. Single-digit-ms
            reads and — the part that matters — *conditional* writes, so two
            Lambdas racing on the same run cannot both increment `attempt`.

Putting run state in S3 instead would work right up until two events for one
run land concurrently, at which point last-write-wins silently eats a retry.
"""

from __future__ import annotations

import json

from scrum.store.base import ArtifactStore, ConcurrentUpdate, RunStateStore


class S3ArtifactStore(ArtifactStore):
    """Artifacts under s3://<bucket>/runs/<run_id>/<name>."""

    mode = "aws"

    def __init__(self, settings, s3_client=None):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = settings.bucket
        self.prefix = "runs"
        self.s3 = s3_client or boto3.client(
            "s3",
            region_name=settings.region,
            config=BotoConfig(connect_timeout=5, read_timeout=15,
                              retries={"max_attempts": 3, "mode": "standard"}),
        )

    def _key(self, run_id: str, name: str) -> str:
        return f"{self.prefix}/{run_id}/{name}"

    def read(self, run_id: str, name: str) -> str:
        from botocore.exceptions import ClientError

        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=self._key(run_id, name))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
                # Translate to the same exception the local store raises, so
                # callers need exactly one except clause.
                raise FileNotFoundError(self._key(run_id, name)) from exc
            raise
        return obj["Body"].read().decode("utf-8")

    def write(self, run_id: str, name: str, content: str) -> str:
        key = self._key(run_id, name)
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        return f"s3://{self.bucket}/{key}"

    def exists(self, run_id: str, name: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(run_id, name))
            return True
        except ClientError:
            return False

    def append(self, run_id: str, name: str, line: str) -> None:
        """
        S3 objects are immutable — there is no append. Read-modify-write is
        fine for a trace file written by one agent at a time; if this ever
        became genuinely concurrent the answer is one object per line
        (<name>/<timestamp>.json) or a Kinesis Firehose, not a lock.
        """
        existing = self.read_or(run_id, name, "")
        self.write(run_id, name, existing + line.rstrip("\n") + "\n")


class DynamoRunStateStore(RunStateStore):
    """
    One item per run, guarded by a version attribute.

    The conditional write is the point. `attribute_not_exists(run_id) OR
    version = :expected` means the update only applies if nobody has written
    since we read — DynamoDB rejects the loser with ConditionalCheckFailed
    rather than letting it clobber.
    """

    mode = "aws"

    def __init__(self, settings, dynamo_client=None):
        import boto3
        from botocore.config import Config as BotoConfig

        self.table = settings.state_table or "agentic-scrum-runs"
        self.dynamo = dynamo_client or boto3.client(
            "dynamodb",
            region_name=settings.region,
            config=BotoConfig(connect_timeout=3, read_timeout=5,
                              retries={"max_attempts": 3, "mode": "standard"}),
        )

    def get(self, run_id: str) -> dict:
        response = self.dynamo.get_item(
            TableName=self.table,
            Key={"run_id": {"S": run_id}},
            ConsistentRead=True,   # the supervisor must not read its own stale write
        )
        item = response.get("Item")
        if not item:
            return {}
        return json.loads(item["state"]["S"]) | {"version": int(item["version"]["N"])}

    def put(self, run_id: str, state: dict, expected_version: int | None = None) -> dict:
        from botocore.exceptions import ClientError

        current = self.get(run_id)
        current_version = current.get("version", 0)
        if expected_version is not None and current_version != expected_version:
            raise ConcurrentUpdate(
                f"run {run_id} is at version {current_version}, expected {expected_version}"
            )

        merged = {**current, **state}
        merged.pop("version", None)
        next_version = current_version + 1

        expression_values = {
            ":state": {"S": json.dumps(merged)},
            ":next": {"N": str(next_version)},
        }

        # The condition is attached ONLY when the caller pinned a version.
        #
        # This looked harmless as `attribute_not_exists(run_id)` plus an
        # optional OR, but it is not: with no pin, that condition means "only
        # if this run has never been written", so every unpinned update to an
        # existing item failed with ConditionalCheckFailed. The local store
        # treats expected_version=None as "no check", so the two
        # implementations disagreed and only real DynamoDB showed it — the
        # supervisor failed on every single event.
        #
        # An unpinned write is an explicit last-write-wins. Callers that care
        # about races pass expected_version; those are the ones that get a
        # condition.
        request = {
            "TableName": self.table,
            "Key": {"run_id": {"S": run_id}},
            "UpdateExpression": "SET #s = :state, version = :next",
            "ExpressionAttributeNames": {"#s": "state"},
            "ExpressionAttributeValues": expression_values,
        }
        if expected_version is not None:
            expression_values[":expected"] = {"N": str(expected_version)}
            request["ConditionExpression"] = (
                "attribute_not_exists(run_id) OR version = :expected"
            )

        try:
            self.dynamo.update_item(**request)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConcurrentUpdate(f"run {run_id} changed underneath us") from exc
            raise

        return merged | {"version": next_version}
