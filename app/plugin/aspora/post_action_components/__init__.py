"""
Aspora Post-Action Components

Exports all Aspora-specific delete post-action components. These mirror the
default cascade-soft-delete behaviour (variable_mst rows + resource row →
SOFT_DELETED + queue item is_deleted) and are shared by the aspora and vance
tenants. Only delete_* case_refs are handled here — create/update flows still
run through the "default" post-action map.
"""

from app.plugin.aspora.post_action_components.aspora_dynamodb_post_action_component import AsporaDynamoDbPostActionComponent
from app.plugin.aspora.post_action_components.aspora_s3_post_action_component import AsporaS3PostActionComponent
from app.plugin.aspora.post_action_components.aspora_sqs_post_action_component import AsporaSqsPostActionComponent
from app.plugin.aspora.post_action_components.aspora_eks_service_post_action_component import AsporaEksServicePostActionComponent

__all__ = [
    "AsporaDynamoDbPostActionComponent",
    "AsporaS3PostActionComponent",
    "AsporaSqsPostActionComponent",
    "AsporaEksServicePostActionComponent",
]
