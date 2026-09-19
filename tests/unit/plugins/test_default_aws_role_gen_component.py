"""Unit tests for the per-tenant default IAM role policy-doc manipulation.

Covers:
- HCL parse: empty ``""`` value, populated heredoc, missing field (error).
- HCL splice: empty → heredoc on first add, heredoc → ``""`` on final remove.
- Mutation: add dedup, remove no-op, statement removed when Resource empties.
- End-to-end sequence for two secrets + one dynamo with mixed ops.
"""

import json
from typing import Any, Dict

import pytest

from app.domain.policies import SUPPORTED_KINDS
from app.plugin.default.default_aws_role_gen_component import (
    _apply_mutation,
    _extract_policy_doc,
    _splice_policy_doc,
)


SECRETS_READ = SUPPORTED_KINDS["secrets-read"]
DYNAMODB_RW = SUPPORTED_KINDS["dynamodb-rw"]


def _hcl_with_empty_policy() -> str:
    return (
        'terraform { source = "x" }\n'
        'inputs = {\n'
        '  identifier               = "default"\n'
        '  custom_policy_json       = ""\n'
        '  managed_policy_arns      = []\n'
        '}\n'
    )


def _hcl_with_heredoc(doc: Dict[str, Any]) -> str:
    body = json.dumps(doc, indent=2)
    return (
        'inputs = {\n'
        '  identifier               = "default"\n'
        f'  custom_policy_json       = <<EOT\n{body}\nEOT\n'
        '  managed_policy_arns      = []\n'
        '}\n'
    )


class TestExtractPolicyDoc:
    def test_empty_value_returns_empty_doc(self):
        doc = _extract_policy_doc(_hcl_with_empty_policy())
        assert doc == {"Version": "2012-10-17", "Statement": []}

    def test_heredoc_is_parsed(self):
        original = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "TenantDefaultSecretsRead",
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": ["arn:aws:secretsmanager:us-east-1:1:secret:foo"],
                }
            ],
        }
        doc = _extract_policy_doc(_hcl_with_heredoc(original))
        assert doc["Statement"] == original["Statement"]

    def test_missing_field_raises(self):
        with pytest.raises(ValueError):
            _extract_policy_doc('inputs = { identifier = "default" }\n')


class TestSplicePolicyDoc:
    def test_empty_to_heredoc(self):
        hcl = _hcl_with_empty_policy()
        doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "X", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:a"]},
            ],
        }
        result = _splice_policy_doc(hcl, doc)
        assert 'custom_policy_json       = <<EOT' in result
        assert '"Sid": "X"' in result
        # Round-trip
        parsed = _extract_policy_doc(result)
        assert parsed["Statement"] == doc["Statement"]

    def test_heredoc_back_to_empty(self):
        existing_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "X", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:a"]},
            ],
        }
        hcl = _hcl_with_heredoc(existing_doc)
        empty = {"Version": "2012-10-17", "Statement": []}
        result = _splice_policy_doc(hcl, empty)
        assert 'custom_policy_json       = ""' in result
        assert "EOT" not in result

    def test_heredoc_replaced_in_place(self):
        hcl = _hcl_with_heredoc({
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "X", "Effect": "Allow", "Action": ["a"], "Resource": ["r1"]},
            ],
        })
        new_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {"Sid": "X", "Effect": "Allow", "Action": ["a"], "Resource": ["r1", "r2"]},
            ],
        }
        result = _splice_policy_doc(hcl, new_doc)
        parsed = _extract_policy_doc(result)
        assert parsed["Statement"][0]["Resource"] == ["r1", "r2"]


class TestApplyMutation:
    def test_add_to_empty_creates_statement(self):
        doc = {"Version": "2012-10-17", "Statement": []}
        new = _apply_mutation(doc, SECRETS_READ, "arn:a", remove=False)
        assert len(new["Statement"]) == 1
        s = new["Statement"][0]
        assert s["Sid"] == SECRETS_READ.sid
        assert s["Resource"] == ["arn:a"]
        # Original untouched
        assert doc["Statement"] == []

    def test_add_dedupes(self):
        doc = _apply_mutation(
            {"Version": "2012-10-17", "Statement": []}, SECRETS_READ, "arn:a", remove=False,
        )
        unchanged = _apply_mutation(doc, SECRETS_READ, "arn:a", remove=False)
        assert unchanged is doc  # returns original reference on no-op

    def test_add_appends_to_existing_statement(self):
        doc = _apply_mutation(
            {"Version": "2012-10-17", "Statement": []}, SECRETS_READ, "arn:a", remove=False,
        )
        doc = _apply_mutation(doc, SECRETS_READ, "arn:b", remove=False)
        assert doc["Statement"][0]["Resource"] == ["arn:a", "arn:b"]

    def test_add_different_kind_creates_second_statement(self):
        doc = _apply_mutation(
            {"Version": "2012-10-17", "Statement": []}, SECRETS_READ, "arn:a", remove=False,
        )
        doc = _apply_mutation(doc, DYNAMODB_RW, "arn:t", remove=False)
        assert len(doc["Statement"]) == 2
        sids = {s["Sid"] for s in doc["Statement"]}
        assert sids == {SECRETS_READ.sid, DYNAMODB_RW.sid}

    def test_remove_shrinks_resource_list(self):
        doc = {"Version": "2012-10-17", "Statement": []}
        for arn in ("arn:a", "arn:b"):
            doc = _apply_mutation(doc, SECRETS_READ, arn, remove=False)
        doc = _apply_mutation(doc, SECRETS_READ, "arn:a", remove=True)
        assert doc["Statement"][0]["Resource"] == ["arn:b"]

    def test_remove_last_arn_drops_statement(self):
        doc = _apply_mutation(
            {"Version": "2012-10-17", "Statement": []}, SECRETS_READ, "arn:a", remove=False,
        )
        doc = _apply_mutation(doc, SECRETS_READ, "arn:a", remove=True)
        assert doc["Statement"] == []

    def test_remove_unknown_arn_is_noop(self):
        doc = _apply_mutation(
            {"Version": "2012-10-17", "Statement": []}, SECRETS_READ, "arn:a", remove=False,
        )
        unchanged = _apply_mutation(doc, SECRETS_READ, "arn:z", remove=True)
        assert unchanged["Statement"] == doc["Statement"]

    def test_remove_from_empty_is_noop(self):
        doc = {"Version": "2012-10-17", "Statement": []}
        result = _apply_mutation(doc, SECRETS_READ, "arn:x", remove=True)
        assert result is doc


class TestFullSequence:
    def test_two_secrets_plus_dynamodb_with_mixed_ops(self):
        hcl = _hcl_with_empty_policy()

        def mutate(h, kind, arn, remove=False):
            doc = _extract_policy_doc(h)
            new = _apply_mutation(doc, kind, arn, remove=remove)
            return _splice_policy_doc(h, new)

        hcl = mutate(hcl, SECRETS_READ, "arn:sec:a")
        hcl = mutate(hcl, SECRETS_READ, "arn:sec:b")
        hcl = mutate(hcl, DYNAMODB_RW, "arn:dyn:t1")
        parsed = _extract_policy_doc(hcl)
        assert len(parsed["Statement"]) == 2
        secrets = next(s for s in parsed["Statement"] if s["Sid"] == SECRETS_READ.sid)
        dynamo = next(s for s in parsed["Statement"] if s["Sid"] == DYNAMODB_RW.sid)
        assert secrets["Resource"] == ["arn:sec:a", "arn:sec:b"]
        assert dynamo["Resource"] == ["arn:dyn:t1"]

        hcl = mutate(hcl, SECRETS_READ, "arn:sec:a", remove=True)
        parsed = _extract_policy_doc(hcl)
        secrets = next(s for s in parsed["Statement"] if s["Sid"] == SECRETS_READ.sid)
        assert secrets["Resource"] == ["arn:sec:b"]

        hcl = mutate(hcl, SECRETS_READ, "arn:sec:b", remove=True)
        parsed = _extract_policy_doc(hcl)
        assert [s["Sid"] for s in parsed["Statement"]] == [DYNAMODB_RW.sid]

        hcl = mutate(hcl, DYNAMODB_RW, "arn:dyn:t1", remove=True)
        assert 'custom_policy_json       = ""' in hcl
