from typing import Dict, Tuple, Optional

from app.infra_chat_agent.config.config_models import (
    # Type identifiers
    TenantId,
    InfraTypeCode,
    ParamName,
    ParamKey,
    QueryRef,
    # Enums
    Operation,
    GroupName,
    ParamType,
    UiWidget,
    ValueSourceType,
    PromptSourceType,
    InfraVendorEnum,
    # Models
    Option,
    ValidationRule,
    ConditionalRequirement,
    UiMeta,
    StaticSource,
    DatabaseSource,
    ApiSource,
    InternalSource,
    StaticPrompt,
    InternalPrompt,
    PromptSpec,
    ParameterMeta,
    ParameterGroupMeta,
    ResourceMeta,
)


class ResourceMetaRepo:
    """
    Static, in-memory repository for ResourceMeta.

    Design rules:
    - Metadata is registered ONLY inside __init__()
    - Repo is immutable after construction
    - One global instance per process
    """

    def __init__(self) -> None:
        self._store: Dict[
            Tuple[TenantId, InfraTypeCode],
            ResourceMeta
        ] = {}

        # ---- one-time registration ----
        self._register_all()

    # ----------------------------
    # Public API (read-only)
    # ----------------------------

    def get(
        self,
        tenant_id: TenantId,
        infra_type: InfraTypeCode,
    ) -> Optional[ResourceMeta]:
        return self._store.get((tenant_id, infra_type))

    def list_for_tenant(
        self,
        tenant_id: TenantId,
    ) -> Dict[InfraTypeCode, ResourceMeta]:
        return {
            infra: meta
            for (t_id, infra), meta in self._store.items()
            if t_id == tenant_id
        }

    def get_placement_options(
        self,
        tenant_id: TenantId,
        infra_type: InfraTypeCode,
        param_key: str,
    ) -> list:
        """
        Get valid options for a placement parameter from resource metadata.

        Returns:
            List of dicts with 'label' and 'value' keys, or empty list.
        """
        meta = self.get(tenant_id, infra_type)
        if not meta:
            return []
        for param in meta.placement.parameters:
            if param.key == param_key and isinstance(param.value_source, StaticSource):
                return [{"label": opt.label, "value": opt.value} for opt in param.value_source.options]
        return []

    def resolve_placement_value(
        self,
        tenant_id: TenantId,
        infra_type: InfraTypeCode,
        param_key: str,
        user_input: str,
    ) -> str:
        """
        Resolve a user-provided placement value to its canonical form
        using the options defined in resource metadata.

        Tries in order:
        1. Exact value match (e.g., 'prod' -> 'prod')
        2. Case-insensitive label match (e.g., 'Core' -> UUID)
        3. Case-insensitive value match (e.g., 'Prod' -> 'prod')
        4. Substring match (e.g., 'mumbai' -> 'region-aspora-mumbai')
        5. Returns user_input as-is if no match (service layer will validate)
        """
        if not user_input:
            return user_input

        meta = self.get(tenant_id, infra_type)
        if not meta:
            return user_input

        options = []
        for param in meta.placement.parameters:
            if param.key == param_key and isinstance(param.value_source, StaticSource):
                options = param.value_source.options
                break

        if not options:
            return user_input

        input_lower = user_input.lower().strip()

        # 1. Exact value match
        for opt in options:
            if opt.value == user_input:
                return opt.value

        # 2. Case-insensitive label match
        for opt in options:
            if opt.label.lower() == input_lower:
                return opt.value

        # 3. Case-insensitive value match
        for opt in options:
            if opt.value.lower() == input_lower:
                return opt.value

        # 4. Substring match (e.g., 'mumbai' in 'region-aspora-mumbai')
        for opt in options:
            if input_lower in opt.value.lower():
                return opt.value

        # 5. No match — return as-is, service layer will validate
        return user_input

    def get_all_environment_values(self) -> list[str]:
        """
        Collect all unique environment values across every registered resource.
        Single source of truth for valid environment choices.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for meta in self._store.values():
            for param in meta.placement.parameters:
                if param.key == "environment_enum" and isinstance(param.value_source, StaticSource):
                    for opt in param.value_source.options:
                        if opt.value not in seen:
                            seen.add(opt.value)
                            ordered.append(opt.value)
        return ordered

    # ----------------------------
    # Internal registration logic
    # ----------------------------

    def _register(self, meta: ResourceMeta) -> None:
        """
        Internal-only registration.
        """
        key = (TenantId(meta.tenantId), InfraTypeCode(meta.infra_type))
        if key in self._store:
            raise RuntimeError(
                f"Duplicate ResourceMeta for "
                f"tenant={meta.tenantId}, infra_type={meta.infra_type}"
            )
        self._store[key] = meta

    def _register_all(self) -> None:
        """
        Register ALL resource metadata here.

        This method is called exactly once
        from the constructor.
        """

        # ==================================================
        # ASPORA : S3
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("s3_infrastructuretype_ref"),
                infra_display_name="AWS S3 Bucket",
                cases=["create_bucket"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create S3 Bucket", "create_bucket"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Bucket Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the S3 bucket to create (must be globally unique). Users will say 'bucket name', 'bucket', or 'name' to refer to this parameter.",
                            examples=[
                                "my-data-bucket",
                                "app-storage-prod",
                                "user-uploads-2024",
                                "my data bucket",
                                "app storage prod",
                                "user uploads",
                                "MyDataBucket",
                                "APP-STORAGE",
                                "User_Uploads_2024",
                                "bucket name: my-bucket → identifier=my-bucket",
                                "name it data-store → identifier=data-store",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "bukcet → bucket (fix typo)",
                                "stoarge → storage (fix typo)",
                                "uplods → uploads (fix typo)",
                                "dta-bucket → data-bucket (fix typo)",
                                "asstes → assets (fix typo)",
                                "documetns → documents (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("versioning"),
                            name=ParamName("Versioning"),
                            type=ParamType.BOOL,
                            order=2,
                            required=False,
                            default=False,
                            description="Enable versioning to keep multiple versions of objects",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                # Natural language patterns → True
                                "need versioning → versioning=True",
                                "want versioning → versioning=True",
                                "enable versioning → versioning=True",
                                "with versioning → versioning=True",
                                "add versioning → versioning=True",
                                # Natural language patterns → False
                                "no versioning → versioning=False",
                                "disable versioning → versioning=False",
                                "without versioning → versioning=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("enable_s3_replication"),
                            name=ParamName("Enable S3 Replication"),
                            type=ParamType.BOOL,
                            order=3,
                            required=False,
                            default=False,
                            description="Enable cross-region replication for the S3 bucket",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                # Natural language patterns → True
                                "need replication → enable_s3_replication=True",
                                "want replication → enable_s3_replication=True",
                                "enable replication → enable_s3_replication=True",
                                "with replication → enable_s3_replication=True",
                                "add replication → enable_s3_replication=True",
                                # Natural language patterns → False
                                "no replication → enable_s3_replication=False",
                                "disable replication → enable_s3_replication=False",
                                "without replication → enable_s3_replication=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("cross_account_account_id"),
                            name=ParamName("Cross Account ID"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            description="AWS account ID (12-digit number) for cross-account access",
                            examples=["123456789012", "919497413314"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^\d{12}$"
                            ),
                            pii_exempt=True,
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create S3 bucket."
                ),
            )
        )

        # ==================================================
        # ASPORA : SQS
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("sqs_infrastructuretype_ref"),
                infra_display_name="AWS SQS Queue",
                cases=["create_queue"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create SQS Queue", "create_queue"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Queue Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the SQS queue to create. Users will say 'queue name', 'queue', or 'name' to refer to this parameter.",
                            examples=[
                                "core-messages",
                                "user-events-queue",
                                "payment-processor",
                                "core messages",
                                "user events queue",
                                "payment processor",
                                "CoreMessages",
                                "USER-EVENTS",
                                "Payment_Processor_Queue",
                                "my queue",        # Short casual name
                                "test logs",       # Short casual name
                                "app events",      # Short casual name
                                "aju logs",        # Short informal name
                                "data sync",       # Short casual name
                                "dlf service",     # Service-style name
                                "api service",     # Service-style name
                                "user service",    # Service-style name
                                "data service",    # Service-style name
                                "queue name: user-events → identifier=user-events",
                                "name it notifications-queue → identifier=notifications-queue",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "mesages → messages (fix typo)",
                                "evnets → events (fix typo)",
                                "payemnt → payment (fix typo)",
                                "processer → processor (fix typo)",
                                "notificatons → notifications (fix typo)",
                                "queu → queue (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            pii_exempt=True,
                        ),
                        ParameterMeta(
                            key=ParamKey("fifo"),
                            name=ParamName("FIFO"),
                            type=ParamType.BOOL,
                            order=2,
                            required=True,
                            default=True,
                            description="Enable FIFO (First-In-First-Out) queue for ordered message processing",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                # Natural language patterns → True
                                "need fifo → fifo=True",
                                "want fifo → fifo=True",
                                "fifo queue → fifo=True",
                                "with fifo → fifo=True",
                                "make it fifo → fifo=True",
                                # Natural language patterns → False
                                "no fifo → fifo=False",
                                "standard queue → fifo=False",
                                "not fifo → fifo=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("dlq"),
                            name=ParamName("DLQ"),
                            type=ParamType.BOOL,
                            order=3,
                            required=True,
                            default=True,
                            description="Enable Dead Letter Queue for handling failed messages",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                # Natural language patterns → True
                                "need dlq → dlq=True",
                                "want dlq → dlq=True",
                                "with dlq → dlq=True",
                                "need dead letter queue → dlq=True",
                                "add dlq → dlq=True",
                                # Natural language patterns → False
                                "no dlq → dlq=False",
                                "disable dlq → dlq=False",
                                "without dlq → dlq=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("max_receive_count"),
                            name=ParamName("Max Receive Count"),
                            type=ParamType.INT,
                            order=4,
                            required=False,
                            description="Number of times a message can be received before moving to DLQ",
                            examples=["3", "5", "10"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("visibility_timeout_seconds"),
                            name=ParamName("Visibility Timeout Seconds"),
                            type=ParamType.INT,
                            order=5,
                            required=False,
                            description="Time in seconds that a message is invisible after being received",
                            examples=["30", "60", "300", "900"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("main_queue_retention_seconds"),
                            name=ParamName("Main Queue Retention Seconds"),
                            type=ParamType.INT,
                            order=6,
                            required=False,
                            description="How long messages are retained in the main queue (in seconds)",
                            examples=["86400", "345600", "1209600"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("dlq_retention_seconds"),
                            name=ParamName("DLQ Retention Seconds"),
                            type=ParamType.INT,
                            order=7,
                            required=False,
                            description="How long messages are retained in the dead letter queue (in seconds)",
                            examples=["86400", "345600", "1209600"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("cross_account_ids"),
                            name=ParamName("Cross Access Account IDs"),
                            type=ParamType.JSON,
                            order=8,
                            required=False,
                            description="List of AWS account IDs (12-digit numbers) for cross-account access. Can provide multiple IDs separated by commas.",
                            examples=["123456789012", "919497413314, 989876543421", "[\"123456789012\", \"919497413314\"]"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^\d{12}$"
                            ),
                            pii_exempt=True,
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create SQS queue."
                ),
            )
        )

        # ==================================================
        # ASPORA : DynamoDB
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("dynamodb_infrastructuretype_ref"),
                infra_display_name="AWS DynamoDB",
                cases=["table_management"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create DynamoDB Table", "table_management"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Table Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the DynamoDB table to create. Users will say 'table name', 'table', or 'name' to refer to this parameter.",
                            examples=[
                                # Simple single-word names
                                "users",
                                "orders",
                                "products",
                                "customers",
                                "items",
                                "data",
                                "logs",
                                "events",
                                # Compound names
                                "users-table",
                                "orders-prod",
                                "sessions-db",
                                "users table",
                                "orders prod",
                                "UsersTable",
                                "ORDERS-PROD",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "studens → students (fix typo)",
                                "studnets → students (fix typo)",
                                "usres → users (fix typo)",
                                "ordres → orders (fix typo)",
                                "custoemrs → customers (fix typo)",
                                "produts → products (fix typo)",
                                # Multi-value extraction (first value = identifier)
                                "users, department, S → identifier=users",
                                "orders, order_id, N → identifier=orders",
                                "products, sku S → identifier=products",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            pii_exempt=True,
                        ),
                        ParameterMeta(
                            key=ParamKey("partition_key"),
                            name=ParamName("Partition Key"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Name of the partition key (hash key) for the table. Users may say 'partition key', 'hash key', 'primary key', or 'pk' to refer to this parameter.",
                            examples=[
                                "user-id",
                                "order-id",
                                "session-id",
                                "user_id",
                                "order_id",
                                "session_id",
                                "user id",
                                "order id",
                                "session id",
                                "UserId",
                                "OrderId",
                                "USER_ID",
                                "ORDER_ID",
                                "id",
                                "pk",
                                "key",
                                "department",
                                "sku",
                                "email",
                                # Short casual two-word names (like SQS)
                                "roll no",         # Casual two-word key name
                                "test key",        # Casual two-word key name
                                "my key",          # Casual two-word key name
                                "app id",          # Casual two-word key name
                                "data key",        # Casual two-word key name
                                "item id",         # Casual two-word key name
                                # Multi-value extraction (second value = partition_key)
                                "users, department, S → partition_key=department",
                                "orders, order_id, N → partition_key=order_id",
                                "products, sku S → partition_key=sku",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "deparment → department (fix typo)",
                                "depratment → department (fix typo)",
                                "sesion → session (fix typo)",
                                "sesson → session (fix typo)",
                                "ordr → order (fix typo)",
                                "usr → user (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("partition_key_type"),
                            name=ParamName("Partition Key Type"),
                            type=ParamType.ENUM,
                            order=3,
                            required=True,
                            description="Data type of the partition key. Accepts: S/String, N/Number, B/Binary. Users may say 'type', 'key type', 'data type', 'string', 'number', or 'binary'.",
                            examples=[
                                "S", "N", "B",
                                "string", "String", "STRING",
                                "number", "Number", "NUMBER",
                                "binary", "Binary", "BINARY",
                                "str", "num", "bin",
                                # Multi-value extraction (third value = partition_key_type)
                                "users, department, S → partition_key_type=S",
                                "orders, order_id, N → partition_key_type=N",
                                "products, sku S → partition_key_type=S",
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.DROPDOWN),
                            validation=ValidationRule(
                                allowed=["S", "N", "B"]
                            ),
                            value_source=StaticSource(
                                options=[
                                    Option("String (S)", "S"),
                                    Option("Number (N)", "N"),
                                    Option("Binary (B)", "B"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create DynamoDB table."
                ),
            )
        )

        # ==================================================
        # ASPORA : Database Creation
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("database_infrastructuretype_ref"),
                infra_display_name="Database Creation",
                cases=["database_creation"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create Database", "database_creation"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("database_name"),
                            name=ParamName("Database Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the database to create. Users will say 'database name', 'database', or 'db' to refer to this parameter.",
                            examples=[
                                # Simple single-word names
                                "users",
                                "orders",
                                "products",
                                "customers",
                                "inventory",
                                "analytics",
                                "logs",
                                "sessions",
                                # Compound names
                                "users-db",
                                "orders-prod",
                                "app-database",
                                "analytics-db",
                                "user database",
                                "orders prod",
                                "UsersDB",
                                "ANALYTICS-DB",
                                "app_database",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "databse → database (fix typo)",
                                "databas → database (fix typo)",
                                "dataabse → database (fix typo)",
                                "usres → users (fix typo)",
                                "ordres → orders (fix typo)",
                                "custoemrs → customers (fix typo)",
                                "produts → products (fix typo)",
                                "inventroy → inventory (fix typo)",
                                "analtyics → analytics (fix typo)",
                                # Full sentence examples
                                "create database users → database_name=users",
                                "add database named orders → database_name=orders",
                                "I need a database called products → database_name=products",
                                "database name is customers → database_name=customers",
                                "new database analytics → database_name=analytics",
                                "name it sessions-db → database_name=sessions-db",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("db_server_name"),
                            name=ParamName("Database Server"),
                            type=ParamType.STRING,
                            order=2,
                            required=False,
                            description="Name of the database server to add the database to. If not specified, will be auto-detected from the file location.",
                            examples=[
                                "common-mysql",
                                "common-pg",
                                "mysql-server-prod",
                                "postgres-server-qa",
                                "db-server-stage",
                                "mysql prod",
                                "postgres qa",
                                "common-mysql → db_server_name=common-mysql",
                                "common-pg → db_server_name=common-pg",
                                "add to common-mysql → db_server_name=common-mysql",
                                "on common-pg server → db_server_name=common-pg",
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                            value_source=StaticSource(
                                options=[
                                    Option("Common MySQL", "common-mysql"),
                                    Option("Common PostgreSQL", "common-pg"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create a new database on an existing database server."
                ),
            )
        )

        # ==================================================
        # ASPORA : Kong Gateway
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("kong_gateway"),
                infra_display_name="Kong Gateway",
                cases=["add_route"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.ENUM,
                            order=4,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("bbps-worker", "9dca29b5-f16c-4751-9d3e-28e886985bb3"),
                                    Option("beneficiary", "cb244962-6585-45c4-9fda-dd04189f5b0f"),
                                    Option("backoffice", "0252b039-6eec-49bf-a924-790e1e155bfe"),
                                    Option("app-server-internal", "b11db949-8a62-488b-86bd-d048fdf088d1"),
                                    Option("rhythm", "1cf0e862-6ab4-45d3-b7c7-57c01c5268d7"),
                                    Option("casa", "671bcbf4-90b4-498d-b69f-ad5370af1087"),
                                    Option("ponzim", "b9155a5f-5df5-4cc0-938b-06acc8d060b9"),
                                    Option("lucifer", "1907a0ef-0d5f-4a22-a642-f65cf7d52c2a"),
                                    Option("qbee", "f8b1fe5b-e3a2-49cd-a727-d32b2c255e10"),
                                    Option("app-server", "d90c2a75-9142-41ac-a4ce-f0075733ad83"),
                                    Option("pulse-backend", "f531d014-4ef5-4390-925e-961912061842"),
                                    Option("alphadesk-api", "78355504-5d29-4f4c-b8b3-d2506b581812"),
                                    Option("bbps", "4dbbac4f-e4ff-4cf2-9705-4a7e8dce2b3a"),
                                    Option("cron", "6fdc78bb-9a6d-48ae-9c4e-fac630cb1ffd"),
                                    Option("notification", "9c43e5cd-6489-4206-8a9d-3253bd9ca833"),
                                    Option("email", "24f82528-39c7-4f97-9634-e9e893442a80"),
                                    Option("recon", "677845cb-5e57-4d0a-bc43-e9f041bcce6b"),
                                    Option("eventbus", "87ce3086-fb5a-4ade-8b59-624be4794b51"),
                                    Option("rewards-api", "0c16857d-f9ea-4e27-b79f-b98ef6be216e"),
                                    Option("goblin", "caaa926b-c91a-4a93-a4a4-493c3aec607f"),
                                    Option("rewards-worker", "0a05b95e-2255-4931-bd5b-75df0f2b6c0b"),
                                    Option("goms", "ac5b3bb7-22ba-4ab9-b1ad-3b577e7f97e5"),
                                    Option("settlements-api", "78436652-d273-4644-8407-de008567bd3f"),
                                    Option("settlements-worker", "b1f079df-c810-485b-8bb7-86005b097cfb"),
                                    Option("user-vault", "90606d1e-9ac1-4fb7-96a1-d651581f4733"),
                                    Option("lulu-fulfillment", "356c2d96-dc54-44f3-beb7-b77037e95b21"),
                                    Option("verification", "40b931f1-2251-4582-8307-9464cf469e51"),
                                    Option("workflow", "f7513b9e-c6ab-4a8e-a61b-b1afeb604829"),
                                    Option("ybl-fulfillment", "d4b39b60-c55d-4f45-a3cc-6b2237d29b84"),
                                    Option("recko", "463aa85c-e5be-473a-8dad-a3a9f6e1651a"),
                                    Option("devops-sandbox", "35ca15cc-5337-4fad-b9b9-318530db1e79"),
                                    Option("fx", "e029d221-25bd-4f46-808b-a0f2c1d48c55"),
                                    Option("fx-api", "77a51f13-849c-483e-8b9a-b4778eeb5158"),
                                    Option("fx_worker", "607ee48c-bd81-4ad0-ab5e-5c51bf7bf369"),
                                    Option("kabutar", "0b69d044-785d-4974-b1d7-75e293aa2f07"),
                                    Option("devops-sandbox-golang", "a4db0a36-5b36-4c2d-8070-5bbd32b1d77e"),
                                    Option("DevLift Test", "85c9007d-860a-4b06-96cd-ec8a7f953279"),
                                    Option("devlift-test", "13b1696e-cdbc-45f6-89fa-c60952062588"),
                                    Option("falcon-worker", "53757661-a66b-47cc-bcb7-33e5fed67c4e"),
                                    Option("falcon-api", "474a9e21-3e62-439d-ac41-54c3c0a8c62b"),
                                    Option("falcon-consumer", "06f876e4-c0d8-48d3-9d29-9c3fd464761f"),
                                    Option("ai-bot", "6fca4932-3750-4497-a088-85fc81fdfa12"),
                                    Option("test_ops", "4f63660a-1425-436c-9a8b-400049bc4b9e"),
                                    Option("reminder", "7b4d2b7e-492e-4066-8d9e-e4fcfb72aed4"),
                                    Option("reminder-worker", "d16ddf6b-d2d6-46f8-aff4-2923bc92faa4"),
                                    Option("payment-platform", "6b94e2ef-e2bb-4a2e-917e-e6aff7b7e563"),
                                    Option("test_worker", "5fcb7f8e-8f0f-4484-82b6-52f7363a180c"),
                                    Option("canvas", "6c023456-bc6b-4334-83a7-27a325969d74"),
                                    Option("beneficiary-service", "51201e49-caeb-4562-81ec-8bf701443b0d"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Add Kong Gateway Route", "add_route"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("method"),
                            name=ParamName("HTTP Method"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            description="HTTP method for the Kong Gateway route",
                            examples=[
                                "GET",
                                "POST",
                                "PUT",
                                "get",        # lowercase
                                "post",       # lowercase
                                "Get",        # mixed case
                                "Post",       # mixed case
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.DROPDOWN),
                            validation=ValidationRule(
                                allowed=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
                            ),
                            value_source=StaticSource(
                                options=[
                                    Option("GET", "GET"),
                                    Option("POST", "POST"),
                                    Option("PUT", "PUT"),
                                    Option("DELETE", "DELETE"),
                                    Option("PATCH", "PATCH"),
                                    Option("HEAD", "HEAD"),
                                    Option("OPTIONS", "OPTIONS"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("route"),
                            name=ParamName("Route"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Kong regex route pattern. Must start with ~/ and end with $",
                            examples=[
                                # Simple API patterns
                                "~/api/users$",
                                "~/api/v1/users$",
                                "~/api/v1/orders$",
                                # Wildcard patterns
                                "~/v1/orders/.*$",
                                "~/api/v1/items/.*$",
                                # Named capture groups (common REST pattern)
                                "~/api/v1/users/(?<id>[^/]+)$",
                                "~/api/v1/orders/(?<id>[^/]+)$",
                                # Numeric ID patterns
                                "~/products/[0-9]+$",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^~/.*\$$",
                                validate_as_regex=True
                            ),
                        ),
                    ],
                ),

                # pre_attributes_collect_validators=[
                #     InternalSource(
                #         type=ValueSourceType.INTERNAL,
                #         class_name="AsporaKongValidator",
                #         method_name="validate",
                #     ),
                # ],
                pre_attributes_collect_validators=[],

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create Kong Gateway route."
                ),
            )
        )

        # ==================================================
        # VANCE : S3
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("s3_infrastructuretype_ref"),
                infra_display_name="AWS S3 Bucket",
                cases=["create_bucket"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("HR Payroll", "f91ff295-6b39-4c4c-a857-d8110fb2494d"),
                                    Option("Finance", "7b318c1e-1707-4313-8e57-8cd20e51b15d"),
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                    Option("Dockerr", "2f2669f3-fccc-437a-a75f-42e34931fdaa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                    Option("Mumbai Aspora", "region-aspora-mumbai"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create S3 Bucket", "create_bucket"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Bucket Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the S3 bucket to create (must be globally unique). Users will say 'bucket name', 'bucket', or 'name' to refer to this parameter.",
                            examples=[
                                "my-data-bucket",
                                "app-storage-prod",
                                "user-uploads-2024",
                                "my data bucket",
                                "app storage prod",
                                "user uploads",
                                "MyDataBucket",
                                "APP-STORAGE",
                                "User_Uploads_2024",
                                "bucket name: my-bucket → identifier=my-bucket",
                                "name it data-store → identifier=data-store",
                                "bukcet → bucket (fix typo)",
                                "stoarge → storage (fix typo)",
                                "uplods → uploads (fix typo)",
                                "dta-bucket → data-bucket (fix typo)",
                                "asstes → assets (fix typo)",
                                "documetns → documents (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("versioning"),
                            name=ParamName("Versioning"),
                            type=ParamType.BOOL,
                            order=2,
                            required=False,
                            default=False,
                            description="Enable versioning to keep multiple versions of objects",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                "need versioning → versioning=True",
                                "want versioning → versioning=True",
                                "enable versioning → versioning=True",
                                "with versioning → versioning=True",
                                "add versioning → versioning=True",
                                "no versioning → versioning=False",
                                "disable versioning → versioning=False",
                                "without versioning → versioning=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("enable_s3_replication"),
                            name=ParamName("Enable S3 Replication"),
                            type=ParamType.BOOL,
                            order=3,
                            required=False,
                            default=False,
                            description="Enable cross-region replication for the S3 bucket",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                "need replication → enable_s3_replication=True",
                                "want replication → enable_s3_replication=True",
                                "enable replication → enable_s3_replication=True",
                                "with replication → enable_s3_replication=True",
                                "add replication → enable_s3_replication=True",
                                "no replication → enable_s3_replication=False",
                                "disable replication → enable_s3_replication=False",
                                "without replication → enable_s3_replication=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("cross_account_account_id"),
                            name=ParamName("Cross Account ID"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            description="AWS account ID (12-digit number) for cross-account access",
                            examples=["123456789012", "919497413314"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^\d{12}$"
                            ),
                            pii_exempt=True,
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create S3 bucket."
                ),
            )
        )

        # ==================================================
        # VANCE : SQS
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("sqs_infrastructuretype_ref"),
                infra_display_name="AWS SQS Queue",
                cases=["create_queue"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("HR Payroll", "f91ff295-6b39-4c4c-a857-d8110fb2494d"),
                                    Option("Finance", "7b318c1e-1707-4313-8e57-8cd20e51b15d"),
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                    Option("Dockerr", "2f2669f3-fccc-437a-a75f-42e34931fdaa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                    Option("Mumbai Aspora", "region-aspora-mumbai"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create SQS Queue", "create_queue"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Queue Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the SQS queue to create. Users will say 'queue name', 'queue', or 'name' to refer to this parameter.",
                            examples=[
                                "core-messages",
                                "user-events-queue",
                                "payment-processor",
                                "core messages",
                                "user events queue",
                                "payment processor",
                                "CoreMessages",
                                "USER-EVENTS",
                                "Payment_Processor_Queue",
                                "my queue",
                                "test logs",
                                "app events",
                                "aju logs",
                                "data sync",
                                "dlf service",
                                "api service",
                                "user service",
                                "data service",
                                "queue name: user-events → identifier=user-events",
                                "name it notifications-queue → identifier=notifications-queue",
                                "mesages → messages (fix typo)",
                                "evnets → events (fix typo)",
                                "payemnt → payment (fix typo)",
                                "processer → processor (fix typo)",
                                "notificatons → notifications (fix typo)",
                                "queu → queue (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("fifo"),
                            name=ParamName("FIFO"),
                            type=ParamType.BOOL,
                            order=2,
                            required=True,
                            default=True,
                            description="Enable FIFO (First-In-First-Out) queue for ordered message processing",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                "need fifo → fifo=True",
                                "want fifo → fifo=True",
                                "fifo queue → fifo=True",
                                "with fifo → fifo=True",
                                "make it fifo → fifo=True",
                                "no fifo → fifo=False",
                                "standard queue → fifo=False",
                                "not fifo → fifo=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("dlq"),
                            name=ParamName("DLQ"),
                            type=ParamType.BOOL,
                            order=3,
                            required=True,
                            default=True,
                            description="Enable Dead Letter Queue for handling failed messages",
                            examples=[
                                "true", "false", "yes", "no", "enable", "disable",
                                "need dlq → dlq=True",
                                "want dlq → dlq=True",
                                "with dlq → dlq=True",
                                "need dead letter queue → dlq=True",
                                "add dlq → dlq=True",
                                "no dlq → dlq=False",
                                "disable dlq → dlq=False",
                                "without dlq → dlq=False",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TOGGLE),
                        ),
                        ParameterMeta(
                            key=ParamKey("max_receive_count"),
                            name=ParamName("Max Receive Count"),
                            type=ParamType.INT,
                            order=4,
                            required=False,
                            description="Number of times a message can be received before moving to DLQ",
                            examples=["3", "5", "10"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("visibility_timeout_seconds"),
                            name=ParamName("Visibility Timeout Seconds"),
                            type=ParamType.INT,
                            order=5,
                            required=False,
                            description="Time in seconds that a message is invisible after being received",
                            examples=["30", "60", "300", "900"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("main_queue_retention_seconds"),
                            name=ParamName("Main Queue Retention Seconds"),
                            type=ParamType.INT,
                            order=6,
                            required=False,
                            description="How long messages are retained in the main queue (in seconds)",
                            examples=["86400", "345600", "1209600"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("dlq_retention_seconds"),
                            name=ParamName("DLQ Retention Seconds"),
                            type=ParamType.INT,
                            order=7,
                            required=False,
                            description="How long messages are retained in the dead letter queue (in seconds)",
                            examples=["86400", "345600", "1209600"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("cross_account_ids"),
                            name=ParamName("Cross Access Account IDs"),
                            type=ParamType.JSON,
                            order=8,
                            required=False,
                            description="List of AWS account IDs (12-digit numbers) for cross-account access. Can provide multiple IDs separated by commas.",
                            examples=["123456789012", "919497413314, 989876543421", "[\"123456789012\", \"919497413314\"]"],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^\d{12}$"
                            ),
                            pii_exempt=True,
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create SQS queue."
                ),
            )
        )

        # ==================================================
        # VANCE : DynamoDB
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("dynamodb_infrastructuretype_ref"),
                infra_display_name="AWS DynamoDB",
                cases=["table_management"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("HR Payroll", "f91ff295-6b39-4c4c-a857-d8110fb2494d"),
                                    Option("Finance", "7b318c1e-1707-4313-8e57-8cd20e51b15d"),
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                    Option("Dockerr", "2f2669f3-fccc-437a-a75f-42e34931fdaa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                    Option("Mumbai Aspora", "region-aspora-mumbai"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create DynamoDB Table", "table_management"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("identifier"),
                            name=ParamName("Table Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the DynamoDB table to create. Users will say 'table name', 'table', or 'name' to refer to this parameter.",
                            examples=[
                                "users",
                                "orders",
                                "products",
                                "customers",
                                "items",
                                "data",
                                "logs",
                                "events",
                                "users-table",
                                "orders-prod",
                                "sessions-db",
                                "users table",
                                "orders prod",
                                "UsersTable",
                                "ORDERS-PROD",
                                "studens → students (fix typo)",
                                "studnets → students (fix typo)",
                                "usres → users (fix typo)",
                                "ordres → orders (fix typo)",
                                "custoemrs → customers (fix typo)",
                                "produts → products (fix typo)",
                                "users, department, S → identifier=users",
                                "orders, order_id, N → identifier=orders",
                                "products, sku S → identifier=products",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("partition_key"),
                            name=ParamName("Partition Key"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Name of the partition key (hash key) for the table. Users may say 'partition key', 'hash key', 'primary key', or 'pk' to refer to this parameter.",
                            examples=[
                                "user-id",
                                "order-id",
                                "session-id",
                                "user_id",
                                "order_id",
                                "session_id",
                                "user id",
                                "order id",
                                "session id",
                                "UserId",
                                "OrderId",
                                "USER_ID",
                                "ORDER_ID",
                                "id",
                                "pk",
                                "key",
                                "department",
                                "sku",
                                "email",
                                "roll no",
                                "test key",
                                "my key",
                                "app id",
                                "data key",
                                "item id",
                                "users, department, S → partition_key=department",
                                "orders, order_id, N → partition_key=order_id",
                                "products, sku S → partition_key=sku",
                                "deparment → department (fix typo)",
                                "depratment → department (fix typo)",
                                "sesion → session (fix typo)",
                                "sesson → session (fix typo)",
                                "ordr → order (fix typo)",
                                "usr → user (fix typo)",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("partition_key_type"),
                            name=ParamName("Partition Key Type"),
                            type=ParamType.ENUM,
                            order=3,
                            required=True,
                            description="Data type of the partition key. Accepts: S/String, N/Number, B/Binary. Users may say 'type', 'key type', 'data type', 'string', 'number', or 'binary'.",
                            examples=[
                                "S", "N", "B",
                                "string", "String", "STRING",
                                "number", "Number", "NUMBER",
                                "binary", "Binary", "BINARY",
                                "str", "num", "bin",
                                "users, department, S → partition_key_type=S",
                                "orders, order_id, N → partition_key_type=N",
                                "products, sku S → partition_key_type=S",
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.DROPDOWN),
                            validation=ValidationRule(
                                allowed=["S", "N", "B"]
                            ),
                            value_source=StaticSource(
                                options=[
                                    Option("String (S)", "S"),
                                    Option("Number (N)", "N"),
                                    Option("Binary (B)", "B"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create DynamoDB table."
                ),
            )
        )


        # ==================================================
        # ASPORA : Database Creation
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("database_infrastructuretype_ref"),
                infra_display_name="Database Creation",
                cases=["database_creation"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Create Database", "database_creation"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("database_name"),
                            name=ParamName("Database Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the database to create. Users will say 'database name', 'database', or 'db' to refer to this parameter.",
                            examples=[
                                # Simple single-word names
                                "users",
                                "orders",
                                "products",
                                "customers",
                                "inventory",
                                "analytics",
                                "logs",
                                "sessions",
                                # Compound names
                                "users-db",
                                "orders-prod",
                                "app-database",
                                "analytics-db",
                                "user database",
                                "orders prod",
                                "UsersDB",
                                "ANALYTICS-DB",
                                "app_database",
                                # TYPO CORRECTIONS - fix obvious spelling mistakes
                                "databse → database (fix typo)",
                                "databas → database (fix typo)",
                                "dataabse → database (fix typo)",
                                "usres → users (fix typo)",
                                "ordres → orders (fix typo)",
                                "custoemrs → customers (fix typo)",
                                "produts → products (fix typo)",
                                "inventroy → inventory (fix typo)",
                                "analtyics → analytics (fix typo)",
                                # Full sentence examples
                                "create database users → database_name=users",
                                "add database named orders → database_name=orders",
                                "I need a database called products → database_name=products",
                                "database name is customers → database_name=customers",
                                "new database analytics → database_name=analytics",
                                "name it sessions-db → database_name=sessions-db",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("db_server_name"),
                            name=ParamName("Database Server"),
                            type=ParamType.STRING,
                            order=2,
                            required=False,
                            description="Name of the database server to add the database to. If not specified, will be auto-detected from the file location.",
                            examples=[
                                "common-mysql",
                                "common-pg",
                                "mysql-server-prod",
                                "postgres-server-qa",
                                "db-server-stage",
                                "mysql prod",
                                "postgres qa",
                                "common-mysql → db_server_name=common-mysql",
                                "common-pg → db_server_name=common-pg",
                                "add to common-mysql → db_server_name=common-mysql",
                                "on common-pg server → db_server_name=common-pg",
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                            value_source=StaticSource(
                                options=[
                                    Option("Common MySQL", "common-mysql"),
                                    Option("Common PostgreSQL", "common-pg"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create a new database on an existing database server."
                ),
            )
        )

        # ==================================================
        # VANCE : Database User Management
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("database_user_infrastructuretype_ref"),
                infra_display_name="Database User Management",
                cases=["user_management"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                    Option("Dev", "dev"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Database User Management", "user_management"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("server_name"),
                            name=ParamName("Server Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the database server (e.g., 'common-mysql', 'common-pg').",
                            examples=[
                                "common-mysql",
                                "common-pg",
                                "mysql-server-prod",
                                "postgres-server-qa",
                            ],
                        ),
                        ParameterMeta(
                            key=ParamKey("username"),
                            name=ParamName("Username"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Username for the new database user.",
                            examples=[
                                "appuser",
                                "dbuser",
                                "readonly_user",
                                "admin_user",
                            ],
                        ),
                        ParameterMeta(
                            key=ParamKey("password"),
                            name=ParamName("Password"),
                            type=ParamType.STRING,
                            order=3,
                            required=True,
                            description="Password for the new database user.",
                        ),
                        ParameterMeta(
                            key=ParamKey("db_type"),
                            name=ParamName("Database Type"),
                            type=ParamType.ENUM,
                            order=4,
                            required=True,
                            description="Type of database (MySQL or PostgreSQL).",
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("MySQL", "mysql"),
                                    Option("PostgreSQL", "postgresql"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create a database user with grants on an existing database server."
                ),
            )
        )

        # ==================================================
        # ASPORA : Database User Management
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="aspora",
                infra_type=InfraTypeCode("database_user_infrastructuretype_ref"),
                infra_display_name="Database User Management",
                cases=["user_management"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Core", "d19899af-78e8-44aa-b95f-afd932a019e3"),
                                    Option("Falcon", "6800d09d-3532-422e-8de9-ee16c9c08f2e"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.STRING,
                            order=4,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Database User Management", "user_management"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN,slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("server_name"),
                            name=ParamName("Server Name"),
                            type=ParamType.STRING,
                            order=1,
                            required=True,
                            description="Name of the database server (e.g., 'common-mysql', 'common-pg').",
                            examples=[
                                "common-mysql",
                                "common-pg",
                                "mysql-server-prod",
                                "postgres-server-qa",
                            ],
                        ),
                        ParameterMeta(
                            key=ParamKey("username"),
                            name=ParamName("Username"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Username for the new database user.",
                            examples=[
                                "appuser",
                                "dbuser",
                                "readonly_user",
                                "admin_user",
                            ],
                        ),
                        ParameterMeta(
                            key=ParamKey("password"),
                            name=ParamName("Password"),
                            type=ParamType.STRING,
                            order=3,
                            required=True,
                            description="Password for the new database user.",
                        ),
                        ParameterMeta(
                            key=ParamKey("db_type"),
                            name=ParamName("Database Type"),
                            type=ParamType.ENUM,
                            order=4,
                            required=True,
                            description="Type of database (MySQL or PostgreSQL).",
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("MySQL", "mysql"),
                                    Option("PostgreSQL", "postgresql"),
                                ]
                            ),
                        ),
                    ],
                ),

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create a database user with grants on an existing database server."
                ),
            )
        )

        # ==================================================
        # VANCE : Kong Gateway
        # ==================================================
        self._register(
            ResourceMeta(
                tenantId="vance",
                infra_type=InfraTypeCode("kong_gateway"),
                infra_display_name="Kong Gateway",
                cases=["add_route"],

                placement=ParameterGroupMeta(
                    group=GroupName.PLACEMENT,
                    order=1,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("infra_vendor_enum"),
                            name=ParamName("Infrastructure Vendor"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("AWS", "aws"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("applications_mst_code"),
                            name=ParamName("Product"),
                            type=ParamType.ENUM,
                            order=2,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("HR Payroll", "f91ff295-6b39-4c4c-a857-d8110fb2494d"),
                                    Option("Finance", "7b318c1e-1707-4313-8e57-8cd20e51b15d"),
                                    Option("Core", "178d48fc-8b2c-4e79-aca9-e18f089a05f9"),
                                    Option("Falcon", "7cfdf597-a879-410c-95b0-fbebfe88abb7"),
                                    Option("Dockerr", "2f2669f3-fccc-437a-a75f-42e34931fdaa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("resource_group_mst_code"),
                            name=ParamName("Resource Group"),
                            type=ParamType.STRING,
                            order=3,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT),
                        ),
                        ParameterMeta(
                            key=ParamKey("service_mst_code"),
                            name=ParamName("Service"),
                            type=ParamType.ENUM,
                            order=4,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.TEXT, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Payroll Processing", "54a8be5c-1a29-4358-8b5b-b0f53de2365f"),
                                    Option("Employee Reimbursements", "c53f6507-8a7f-4313-9312-5a7e38ff3152"),
                                    Option("Bookkeeping", "232694a1-5dd7-4bed-8a48-48f7e578d73e"),
                                    Option("Financial Planning", "3b2116cb-7f19-4c54-aefc-e410e18c2975"),
                                    Option("casa-service", "3056c066-79a5-4595-bbfc-c1a126c1ae8a"),
                                    Option("order-service", "19960788-c18a-4669-af13-bee85b6a1765"),
                                    Option("beneficiary-service", "aa20ef84-7046-4171-bd19-d949fdc4e70c"),
                                    Option("service-example-10", "5a0a960c-0a0e-4a17-b101-58fcf01d3acb"),
                                    Option("ajmal-ser", "840c3f5c-1513-4747-8ac8-ff201f84bb97"),
                                    Option("ajmal-background-service", "e5a4fdc4-53f4-4cad-9511-5db5f7e05ace"),
                                    Option("ajmal-background-ser-2", "954f2a3c-4ecf-4866-9853-c7462885fbcd"),
                                    Option("chat", "cec65970-ebab-49c3-a0a2-e52edf97c993"),
                                    Option("casa", "06adffab-1030-482c-971d-55a982b6d642"),
                                    Option("jkl", "a4c3d6e6-43b9-4044-836a-dc4ae148f56f"),
                                    Option("ninto-ser", "bfc9ac1f-c23e-4caf-b77a-052b6d210f54"),
                                    Option("ajmal-testing-1", "b0f010a4-b45f-4ec3-8f2a-42d4b70f32c6"),
                                    Option("ajmal-new", "4e028d1d-945b-42fd-b9fa-abf3f7089668"),
                                    Option("ajmal-new-2", "8f2aac9a-ac47-46ac-812a-9ea7a6d27ba6"),
                                    Option("goms", "c6732309-97ec-4fc4-a45f-5e4785e341f4"),
                                    Option("rda", "95c1efe9-0026-4932-ae17-6f9997500c05"),
                                    Option("new-ser", "27f208c6-22a9-4450-8dd8-4a8f8b25ffcc"),
                                    Option("new-ser-2", "c6dd3e3f-8e28-47a2-aca3-f3a829f0013c"),
                                    Option("new-ser-3", "c4d7257e-0b2e-4f21-8f40-9cbf8a41cee5"),
                                    Option("Local Server", "b4cc1f47-1b3d-4082-b9e7-c09650bf104e"),
                                    Option("verification", "0150376c-b4c1-426c-bac1-fcfabbfc553d"),
                                    Option("workflow", "c15fcb00-eaff-4cc4-930f-8b0a7e8d5cba"),
                                    Option("new-ser-4", "946d9c1c-6db8-4867-88ae-001126309882"),
                                    Option("new-ser-5", "f8cc3629-224b-4efb-b5ee-2ae030f91221"),
                                    Option("listener", "7c331caf-1c26-4902-8936-6076acaeb48b"),
                                    Option("aslam-test-1", "417a9c2a-d2a4-4be6-969a-0f85e8d0e877"),
                                    Option("aslam-test-2", "56061718-3bda-44c7-b0f4-d091ca53b5c8"),
                                    Option("aslam-test-3", "2742663c-9b87-4220-8401-0026a3302041"),
                                    Option("yah-test-1", "c3706117-c158-4e3c-9610-9436aad5d50a"),
                                    Option("ninto-testing-100", "295e81be-0fc0-4dad-8a38-b38f99dfa0c9"),
                                    Option("ninto-testing-101", "d1821182-a2da-41f9-bfb0-d8080722ede1"),
                                    Option("ninto-testing=102", "e645a21e-80be-446f-b528-44e704af8fca"),
                                    Option("ninto-104", "1ed00837-055c-4cc4-bc17-8901a05421bf"),
                                    Option("ninto-105", "8f1baa1a-5415-4242-ab97-f94cdd7b913b"),
                                    Option("ninto-106", "27384fae-ccbe-46a4-b5de-dc27a347b2ae"),
                                    Option("ninto-107", "932068a7-3a0a-486e-bbdd-adf08ff92318"),
                                    Option("ninto-108", "081e8346-2c7e-4984-a527-0805be30c8b7"),
                                    Option("ninto-109", "86b532a1-5c51-4f18-8eed-bc297fb77c69"),
                                    Option("ninto-110", "fa5f399f-600e-4b1a-a1e9-dc605fbcead2"),
                                    Option("surya-1", "511db0d1-ea0c-4e90-9540-2d436c004ee0"),
                                    Option("surya-2", "d2d9e95c-7218-460e-a5fc-fdf3879a2f3a"),
                                    Option("amal-ser", "9c8c51c7-9cbd-4d30-baf9-5a0d6eb3ca4a"),
                                    Option("amal-serr", "5d6d5143-d2bc-4407-87f1-8c00be46b879"),
                                    Option("form-test-ser", "631a4d98-9dd2-4496-b4aa-c849d755278c"),
                                    Option("form-test-ser2", "c59365f6-bcd5-4af3-b0fd-ae5595dbe2ef"),
                                    Option("form-test-3", "eb96f56b-a826-452e-bd7f-78ecdcb785de"),
                                    Option("form-test4", "d51c00bb-33c3-4650-85d8-c722faefca40"),
                                    Option("form-test-5", "4ffe59b2-6a53-4680-9b1b-1b9529d24dfc"),
                                    Option("form-test6", "8c044fde-4f12-4048-a9e1-43616f7f78dd"),
                                    Option("form-test7", "446736c5-26a7-4cc5-96f6-930c292c55c6"),
                                    Option("form-test-8", "77b9f39f-4415-48bd-ad82-aa20ae440a91"),
                                    Option("testsdlkjf", "8bf627d3-5cc8-4d4f-9762-aeb8b6296f6d"),
                                    Option("form-test-9", "e9c8aa3a-b94c-4d60-9329-e4b5ece08d93"),
                                    Option("form-test-11", "57f8f0f9-ea43-48d2-aea2-10b1fecff46f"),
                                    Option("form-test-12", "f779d453-b3d8-46cb-a5b4-5703170e972c"),
                                    Option("form-test-14", "5984ca51-3600-4549-b6b1-5832fafa826b"),
                                    Option("devops-sandbox", "3122e1bc-648c-43c3-8e4e-fe623af57eae"),
                                    Option("nasil-1", "929e2956-d60c-4883-bc5f-db9a97c4dc33"),
                                    Option("user-service", "639e814e-395a-48cc-ad47-b5c399c1af73"),
                                    Option("auth-service", "2e8e7e25-be8c-4e18-8b11-ccfcc6559f9f"),
                                    Option("payment-service1", "a7ddc47e-86b5-4e8c-841e-3e6f3baaf474"),
                                    Option("order-service", "998800a8-1093-4f14-80d4-b6e5b399a906"),
                                    Option("notification-service", "6b2da496-f76a-4e82-a2b9-e22d4ef71652"),
                                    Option("inventory-service", "b4acbbdc-e232-4b62-867c-bc376e63e4df"),
                                    Option("customer-service", "6c010401-f736-49df-a375-880b47254972"),
                                    Option("profile-service", "a2622ad6-6bab-46ef-8cbe-ec396603779c"),
                                    Option("billing", "edf4ca83-5cb6-40be-ac81-2cdffa693551"),
                                    Option("refund", "d439b895-ca0b-4904-a4e1-ff079432d279"),
                                    Option("shipment", "ec360123-b4da-4ada-b5b3-0c1faa20f810"),
                                    Option("rda", "8d92dfc3-0070-4cc1-9525-5c1b9ab3330d"),
                                    Option("auth", "0e650b02-c691-486d-9d1f-c3d5bbca5dcd"),
                                    Option("cart", "49aade5e-0e14-4bb2-9822-2583bac8102c"),
                                    Option("checkout", "62673540-b8fd-4f5f-9b62-551570bca9f7"),
                                    Option("b", "59484fc3-0702-4c87-9f67-8a1016de9cb3"),
                                    Option("c", "fc2e57f4-4245-440a-a410-c07103230929"),
                                    Option("d", "993d6bef-a781-45eb-a8e3-50ab09eeaa0e"),
                                    Option("falcon-api", "2dad92a9-40ab-400a-8db6-821c2faec325"),
                                    Option("Durty", "5c0d6697-4518-4969-8631-213ffd72f4a2"),
                                    Option("e", "06deee0d-4f2b-4061-9742-27e17d6ffa31"),
                                    Option("bnm", "8c8d38f1-293b-44ab-a6f0-7e4fa6c174ad"),
                                    Option("cvb", "b6304fb4-04ce-46e6-94ca-c732bbcff181"),
                                    Option("view", "3617bcb2-cc97-4ff6-9ffd-9d7ec7a1a4c0"),
                                    Option("parc", "b4cf7208-b716-4b84-93a9-0085b4afdc0f"),
                                    Option("park", "379b2eab-0edc-494b-8096-457da20c4e79"),
                                    Option("sage", "a7baf6c2-3139-4c53-8573-0b14e6af5ce6"),
                                    Option("iris", "c6c3d759-2152-46ad-b946-d647faebe103"),
                                    Option("testService123", "14b820b5-01f0-40fc-8f51-3ee5dd4ea42b"),
                                    Option("testservice1234", "abb27489-2256-4654-bb14-92f860a1e769"),
                                    Option("test123", "e77197a3-4dfe-431d-bc40-827122f0c88f"),
                                    Option("test23", "c637e5ad-cd94-4e62-8580-9a9b31c6ce38"),
                                    Option("test34", "a87cc070-78fb-4a98-b6aa-ef8c63b0c97f"),
                                    Option("test45", "4f0086f8-f35d-4557-94db-866ebccc0c91"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("environment_enum"),
                            name=ParamName("Environment"),
                            type=ParamType.ENUM,
                            order=5,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Prod", "prod"),
                                    Option("Stage", "stage"),
                                    Option("QA", "qa"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("geo_loc_mst_code"),
                            name=ParamName("Geo Location"),
                            type=ParamType.ENUM,
                            order=6,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Mumbai", "region-aspora-mumbai"),
                                    Option("London", "region-aspora-london"),
                                    Option("Mumbai Aspora", "region-aspora-mumbai"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_type_ref_code"),
                            name=ParamName("Case Type"),
                            type=ParamType.ENUM,
                            order=7,
                            required=True,
                            ui=UiMeta(form=UiWidget.DROPDOWN),
                            value_source=StaticSource(
                                options=[
                                    Option("Add Kong Gateway Route", "add_route"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("case_code"),
                            name=ParamName("Case Code"),
                            type=ParamType.STRING,
                            order=8,
                            required=False,
                            ui=UiMeta(form=UiWidget.DROPDOWN, slack=UiWidget.DROPDOWN),
                        ),
                    ],
                ),

                attributes=ParameterGroupMeta(
                    group=GroupName.ATTRIBUTES,
                    order=2,
                    parameters=[
                        ParameterMeta(
                            key=ParamKey("method"),
                            name=ParamName("HTTP Method"),
                            type=ParamType.ENUM,
                            order=1,
                            required=True,
                            description="HTTP method for the Kong Gateway route",
                            examples=[
                                "GET",
                                "POST",
                                "PUT",
                                "get",
                                "post",
                                "Get",
                                "Post",
                            ],
                            ui=UiMeta(form=UiWidget.DROPDOWN, chatbot=UiWidget.DROPDOWN),
                            validation=ValidationRule(
                                allowed=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
                            ),
                            value_source=StaticSource(
                                options=[
                                    Option("GET", "GET"),
                                    Option("POST", "POST"),
                                    Option("PUT", "PUT"),
                                    Option("DELETE", "DELETE"),
                                    Option("PATCH", "PATCH"),
                                    Option("HEAD", "HEAD"),
                                    Option("OPTIONS", "OPTIONS"),
                                ]
                            ),
                        ),
                        ParameterMeta(
                            key=ParamKey("route"),
                            name=ParamName("Route"),
                            type=ParamType.STRING,
                            order=2,
                            required=True,
                            description="Kong regex route pattern. Must start with ~/ and end with $",
                            examples=[
                                "~/api/users$",
                                "~/api/v1/users$",
                                "~/api/v1/orders$",
                                "~/v1/orders/.*$",
                                "~/api/v1/items/.*$",
                                "~/api/v1/users/(?<id>[^/]+)$",
                                "~/api/v1/orders/(?<id>[^/]+)$",
                                "~/products/[0-9]+$",
                            ],
                            ui=UiMeta(chatbot=UiWidget.TEXT),
                            validation=ValidationRule(
                                regex=r"^~/.*\$$",
                                validate_as_regex=True
                            ),
                        ),
                    ],
                ),

                # pre_attributes_collect_validators=[
                #     InternalSource(
                #         type=ValueSourceType.INTERNAL,
                #         class_name="AsporaKongValidator",
                #         method_name="validate",
                #     ),
                # ],
                pre_attributes_collect_validators=[],

                prompt=StaticPrompt(
                    type=PromptSourceType.STATIC,
                    text="Create Kong Gateway route."
                ),
            )
        )


# ----------------------------
# STATIC SINGLETON INSTANCE
# ----------------------------

resource_meta_repo = ResourceMetaRepo()
