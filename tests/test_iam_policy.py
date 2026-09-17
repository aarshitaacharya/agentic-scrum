"""
The QA function's S3 permissions.

Two mistakes are possible here and both reached AWS in some form:

  too tight   S3ReadPolicy alone — QA cannot write its own verdict, so every
              run dies with AccessDenied on qa_review.txt. This is what
              actually happened on the first live deploy.

  too loose   S3CrudPolicy — QA can overwrite patched_script.py, which quietly
              destroys the independent review the whole pipeline exists for.
              A reviewer that can edit the code under review is not a reviewer.

The correct answer is neither: read anything, write only the two objects QA
owns. That intent lives in a hand-written IAM statement, which is exactly the
kind of thing that gets "simplified" by a future edit — so pin it.
"""

import os

import pytest

yaml = pytest.importorskip("yaml")

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "infra", "template.yaml")


class _Loader(yaml.SafeLoader):
    """
    Resolve CloudFormation intrinsics to their raw text.

    !Sub 'arn:aws:s3:::${Bucket}/runs/*/qa_review.txt' has to come back as that
    string, not as a placeholder — the whole point is asserting on the object
    names inside it.
    """


def _intrinsic(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_Loader.add_multi_constructor("!", _intrinsic)


@pytest.fixture(scope="module")
def qa_policies():
    with open(TEMPLATE) as fh:
        template = yaml.load(fh, Loader=_Loader)
    return template["Resources"]["QaFunction"]["Properties"]["Policies"]


def _statements(policies):
    for policy in policies:
        if isinstance(policy, dict) and "Statement" in policy:
            return policy["Statement"]
    return []


def test_qa_can_write_its_own_verdict(qa_policies):
    """The bug that broke the first live run."""
    writes = [
        resource
        for statement in _statements(qa_policies)
        if "s3:PutObject" in _as_list(statement["Action"])
        for resource in _as_list(statement["Resource"])
    ]
    assert any("qa_review.txt" in r for r in writes), \
        "QA must be able to write qa_review.txt or every run fails with AccessDenied"


def test_qa_can_append_to_the_trace(qa_policies):
    writes = [
        resource
        for statement in _statements(qa_policies)
        if "s3:PutObject" in _as_list(statement["Action"])
        for resource in _as_list(statement["Resource"])
    ]
    assert any("trace.jsonl" in r for r in writes)


def test_qa_cannot_overwrite_the_patch(qa_policies):
    """
    The restriction that matters. QA's write grants must name specific objects;
    a wildcard would let it rewrite the very file it is judging.
    """
    writes = [
        resource
        for statement in _statements(qa_policies)
        if "s3:PutObject" in _as_list(statement["Action"])
        for resource in _as_list(statement["Resource"])
    ]
    assert writes, "QA has no write grant at all"
    for resource in writes:
        assert resource.rstrip().endswith((".txt", ".jsonl")), \
            f"QA write grant '{resource}' is not scoped to a specific object"
        assert "patched_script" not in resource


def test_qa_does_not_hold_blanket_s3_crud(qa_policies):
    """S3CrudPolicy is the lazy fix for the AccessDenied. It is the wrong one."""
    named = [p for p in qa_policies if isinstance(p, dict) for k in p if k == "S3CrudPolicy"]
    assert not named, "S3CrudPolicy would let QA overwrite the patch under review"


def test_qa_can_still_read_everything(qa_policies):
    reads = [
        action
        for statement in _statements(qa_policies)
        for action in _as_list(statement["Action"])
    ]
    assert "s3:GetObject" in reads, "QA must read the patch it is reviewing"


def test_dev_is_the_only_agent_with_broad_write():
    """Cross-check the code-level restriction against the infrastructure one."""
    from scrum.agents.workers import TOOLS_FOR

    assert "write_patch" in TOOLS_FOR["dev"]
    assert "write_patch" not in TOOLS_FOR["qa"]
    assert "write_patch" not in TOOLS_FOR["pm"]


def _as_list(value):
    return value if isinstance(value, list) else [value]
