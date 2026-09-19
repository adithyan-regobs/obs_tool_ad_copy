"""
Schemas for the resource-variable save API.

The request is a bare array of variable items. `value` is never persisted in
the DB — it goes to the audit-trail bucket (KMS-encrypted when type=secret).
"""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, SecretStr


class SaveVariableItem(BaseModel):
    transaction_code: str = Field(..., description="Code of the owning resource (e.g. service_config code)")
    table_name: str = Field(..., description="Owning table, e.g. 'service_config' or 'infrastructure'")
    key: str = Field(..., description="Variable/secret name, e.g. 's3_bucket_name'")
    value: SecretStr = Field(
        ...,
        description="Plain value; never persisted anywhere in Postgres. SecretStr: "
                    "every dump/repr/log renders '**********' — code that pushes the "
                    "value to S3/KMS/AWS must call .plain_value().",
    )
    type: Literal["variable", "secret"] = Field(..., description="'variable' (plaintext) or 'secret' (encrypted)")
    variable_code: Optional[str] = Field(
        default=None,
        description="variable_mst code of an EXISTING variable (from GET /resource-variable). "
                    "Present → update/rename by code, no DB lookup. Absent → a new add.",
    )
    operation: Optional[Literal["delete"]] = Field(
        default=None,
        description="Set to 'delete' to STAGE a deletion (applied on deploy). add/update are "
                    "derived from variable_code presence; only 'delete' is explicit.",
    )
    old_key: Optional[str] = Field(
        default=None,
        description="Previous key name when this save is a rename; deploy moves the value "
                    "under the new key (empty value = carry the old value over)",
    )
    referenced_transaction_code: Optional[str] = Field(
        default=None, description="Code of the resource this variable points at (e.g. infrastructure code)"
    )
    referenced_table_name: Optional[str] = Field(
        default=None, description="Table the referenced_transaction_code lives in, e.g. 'infrastructure'"
    )
    is_write_only: bool = Field(
        default=False,
        description="Mark this SECRET write-only: the value can be written and later "
                    "overwritten, but no read API ever returns it. One-way — a row that is "
                    "already write-only cannot be turned back into a readable one, and the "
                    "flag is rejected for type='variable'. Omitted/false leaves an existing "
                    "row's flag untouched.",
    )
    is_draft_value: bool = Field(
        default=False,
        description="Caller's verdict (from GET /project-variables/with-values) that this key "
                    "exists ONLY as its un-deployed draft: staged, never deployed by DevLift, "
                    "absent from AWS. When true (and cross-checked server-side), an edit keeps "
                    "operation 'add' and a delete PURGES the draft entry instead of staging a "
                    "delete. Omitted/false → classification behaves exactly as before.",
    )

    def plain_value(self) -> str:
        """The real value, for service-layer use only (S3 staging, KMS, AWS)."""
        return self.value.get_secret_value()


class SaveVariableResult(BaseModel):
    key: str
    variable_code: Optional[str] = None
    operation: Optional[str] = None  # add | update
    audit_files: List[str] = []
    status: Literal["success", "error"]
    error: Optional[str] = None


class SaveVariablesResponse(BaseModel):
    results: List[SaveVariableResult]
    staged_file: Optional[str] = None  # temp-bucket key holding the staged list


class CloneVariableItem(BaseModel):
    key: str = Field(..., description="Variable/secret key")
    value: SecretStr = Field(
        ...,
        description="Value the user is cloning (the live value shown in the picker). "
                    "SecretStr: masked in every dump/repr/log; use .plain_value().",
    )
    type: Literal["variable", "secret"] = Field(default="variable")

    def plain_value(self) -> str:
        """The real value, for service-layer use only (S3 staging, KMS, AWS)."""
        return self.value.get_secret_value()


class CloneVariablesRequest(BaseModel):
    transaction_code: str = Field(..., description="TARGET resource receiving the cloned variables")
    table_name: str = Field(..., description="Target table, e.g. 'service_config'")
    # Clone is assisted manual entry: the frontend sends the values the user
    # saw and chose to copy (WYSIWYG), so no source re-read at execute time.
    items: List[CloneVariableItem] = Field(..., description="Variables to clone, with values")
    source_transaction_code: str = Field(..., description="SOURCE resource the values came from (audit/reference)")
    source_table_name: str = Field(..., description="Source table, e.g. 'service_config'")


class CloneVariableResult(BaseModel):
    key: str
    variable_code: Optional[str] = None
    operation: Optional[str] = None  # add | update (classification on the TARGET)
    type: Optional[str] = None  # variable | secret (taken from the source audit entry)
    status: Literal["success", "error"]
    error: Optional[str] = None


class CloneVariablesResponse(BaseModel):
    results: List[CloneVariableResult]
    staged_file: Optional[str] = None


class DeployVariablesRequest(BaseModel):
    transaction_code: str = Field(..., description="Code of the owning resource (service_config code)")
    table_name: Optional[str] = Field(
        default=None, description="Owning table, e.g. 'service_config'; falls back to the staged file's table_name"
    )
    environment: Optional[str] = Field(
        default=None, description="Optional guard; must match the service_config environment when given"
    )


class DeployVariableResult(BaseModel):
    key: str
    operation: Optional[str] = None  # add | update
    cloud_identifier: Optional[str] = None  # Secrets Manager ARN or SSM parameter name
    audit_files: List[str] = []
    status: Literal["success", "error"]
    error: Optional[str] = None


class DeployVariablesResponse(BaseModel):
    results: List[DeployVariableResult]
    staged_file: Optional[str] = None  # temp file that was deployed (deleted on success)


class RevertVariablesRequest(BaseModel):
    transaction_code: str = Field(..., description="Code of the owning resource (service_config code)")
    environment: Optional[str] = Field(
        None, description="Guard: must match the resource's environment"
    )
    keys: List[str] = Field(..., description="Keys whose staged (un-deployed) change to discard")


class RevertVariablesResponse(BaseModel):
    reverted: List[str]  # keys whose staged entry was removed


class StageSyncRequest(BaseModel):
    """Stage a drift resolution as a normal draft; deploy applies it.

    restore_devlift: stage DevLift's last deployed value (audit bucket) so
    deploy pushes it back over a manual AWS change.
    accept_aws: stage the live AWS value so deploy adopts it as DevLift's
    new deployed baseline.
    """
    transaction_code: str = Field(..., description="Code of the owning resource (service_config code)")
    table_name: str = Field(default="service_config")
    keys: List[str] = Field(..., description="Drifted variable/secret keys to stage")
    direction: Literal["restore_devlift", "accept_aws"]


class StageSyncResult(BaseModel):
    key: str
    status: Literal["success", "error"]
    error: Optional[str] = None


class StageSyncResponse(BaseModel):
    results: List[StageSyncResult]
    staged_file: Optional[str] = None
