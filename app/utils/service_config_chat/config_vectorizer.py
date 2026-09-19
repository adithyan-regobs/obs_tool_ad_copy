"""
Config Vectorizer for semantic config search.

Indexes service configurations to Qdrant for semantic similarity search.
Enables queries like "Find services similar to payment-service" or
"Show me services with high memory and autoscaling".
"""
import logging
import hashlib
import uuid
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

try:
    from qdrant_client.http import models as qdrant_models
except ImportError:
    qdrant_models = None

from app.core.config import settings
from app.integrations.qdrant_integration import QdrantIntegration
from app.integrations.openai_integration import OpenAIIntegration

logger = logging.getLogger(__name__)


# Key parameters to include in summary text for embedding
# Add more parameters here as needed - just add the config key name
SUMMARY_PARAMETERS = [
    "cpu",
    "ram",
    "port",
    "health",
    "autoscaling",
    "alb_selection",
]


@dataclass
class ConfigDocument:
    """Document structure for config vectorization."""

    id: str
    tenant_code: str
    service_code: str
    service_name: str
    environment: str
    geo_loc: str
    summary_text: str
    config_json: Dict[str, Any]
    parameter_values: Dict[str, Any]


class ConfigVectorizer:
    """
    Vectorizes and indexes service configurations for semantic search.
    """

    def __init__(self):
        self._collection_name = settings.qdrant_configs_collection
        self._vector_size = settings.embedding_dimensions

    async def ensure_collection_exists(self) -> bool:
        """Ensure the service_configs collection exists in Qdrant."""
        return await QdrantIntegration.create_collection(
            collection_name=self._collection_name,
            vector_size=self._vector_size,
        )

    def generate_document_id(
        self,
        tenant_code: str,
        service_code: str,
        environment: str,
        geo_loc: str,
    ) -> str:
        """Generate unique document ID from config identifiers as UUID."""
        key = f"{tenant_code}_{service_code}_{environment}_{geo_loc}"
        # Create a UUID from the hash (UUID5 with namespace)
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, key))

    def generate_summary_text(
        self,
        service_name: str,
        service_code: str,
        environment: str,
        geo_loc: str,
        config: Dict[str, Any],
        sidecar_config: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """Generate summary text for embedding."""
        parts = [
            f"Service: {service_name} ({service_code})",
            f"Environment: {environment}",
            f"Region: {geo_loc}",
        ]

        # Add key parameter values
        for param in SUMMARY_PARAMETERS:
            if param in config:
                value = config[param]
                if isinstance(value, dict):
                    nested_parts = [f"{k}={v}" for k, v in value.items()]
                    parts.append(f"{param}: {', '.join(nested_parts)}")
                else:
                    parts.append(f"{param}: {value}")

        # Add sidecar info
        if sidecar_config:
            sidecar_names = [
                s.get("name", s.get("sidecar_config_code", "unknown"))
                for s in sidecar_config
                if s.get("enabled", True)
            ]
            if sidecar_names:
                parts.append(f"sidecars: {', '.join(sidecar_names)}")

        return ". ".join(parts)

    def flatten_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten nested config for Qdrant payload filtering."""
        result = {}
        for key, value in config.items():
            if isinstance(value, dict):
                for nested_key, nested_value in value.items():
                    result[f"{key}.{nested_key}"] = nested_value
            else:
                result[key] = value
        return result

    def prepare_document(
        self,
        tenant_code: str,
        service_code: str,
        service_name: str,
        environment: str,
        geo_loc: str,
        config: Dict[str, Any],
        sidecar_config: Optional[List[Dict[str, Any]]] = None,
    ) -> ConfigDocument:
        """Prepare a config document for vectorization."""
        doc_id = self.generate_document_id(
            tenant_code, service_code, environment, geo_loc
        )
        summary_text = self.generate_summary_text(
            service_name=service_name,
            service_code=service_code,
            environment=environment,
            geo_loc=geo_loc,
            config=config,
            sidecar_config=sidecar_config,
        )
        parameter_values = self.flatten_config(config)

        return ConfigDocument(
            id=doc_id,
            tenant_code=tenant_code,
            service_code=service_code,
            service_name=service_name,
            environment=environment,
            geo_loc=geo_loc,
            summary_text=summary_text,
            config_json=config,
            parameter_values=parameter_values,
        )

    async def index_config(
        self,
        tenant_code: str,
        service_code: str,
        service_name: str,
        environment: str,
        geo_loc: str,
        config: Dict[str, Any],
        sidecar_config: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Index a single config to Qdrant."""
        doc = self.prepare_document(
            tenant_code=tenant_code,
            service_code=service_code,
            service_name=service_name,
            environment=environment,
            geo_loc=geo_loc,
            config=config,
            sidecar_config=sidecar_config,
        )

        embedding = await OpenAIIntegration.embed_text(doc.summary_text)
        if not embedding:
            logger.error(f"Failed to generate embedding for {doc.id}")
            return False

        point = qdrant_models.PointStruct(
            id=doc.id,
            vector=embedding,
            payload={
                "tenant_code": doc.tenant_code,
                "service_code": doc.service_code,
                "service_name": doc.service_name,
                "environment": doc.environment,
                "geo_loc": doc.geo_loc,
                "summary_text": doc.summary_text,
                "config_json": doc.config_json,
                "parameter_values": doc.parameter_values,
            },
        )

        return await QdrantIntegration.upsert_points(
            collection_name=self._collection_name,
            points=[point],
        )

    async def index_configs_batch(self, configs: List[Dict[str, Any]]) -> int:
        """Index multiple configs in batch (more efficient)."""
        if not configs:
            return 0

        documents = []
        for cfg in configs:
            doc = self.prepare_document(
                tenant_code=cfg["tenant_code"],
                service_code=cfg["service_code"],
                service_name=cfg["service_name"],
                environment=cfg["environment"],
                geo_loc=cfg["geo_loc"],
                config=cfg["config"],
                sidecar_config=cfg.get("sidecar_config"),
            )
            documents.append(doc)

        summary_texts = [doc.summary_text for doc in documents]
        embeddings = await OpenAIIntegration.embed_batch(summary_texts)

        points = []
        for doc, embedding in zip(documents, embeddings):
            if embedding:
                point = qdrant_models.PointStruct(
                    id=doc.id,
                    vector=embedding,
                    payload={
                        "tenant_code": doc.tenant_code,
                        "service_code": doc.service_code,
                        "service_name": doc.service_name,
                        "environment": doc.environment,
                        "geo_loc": doc.geo_loc,
                        "summary_text": doc.summary_text,
                        "config_json": doc.config_json,
                        "parameter_values": doc.parameter_values,
                    },
                )
                points.append(point)

        if not points:
            logger.warning("No valid embeddings generated for batch")
            return 0

        success = await QdrantIntegration.upsert_points(
            collection_name=self._collection_name,
            points=points,
        )
        return len(points) if success else 0

    async def search_similar_configs(
        self,
        query: str,
        tenant_code: str,
        environment: Optional[str] = None,
        geo_loc: Optional[str] = None,
        limit: int = 5,
        score_threshold: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """Search for configs similar to a natural language query."""
        query_embedding = await OpenAIIntegration.embed_text(query)
        if not query_embedding:
            logger.error(f"Failed to generate embedding for query: '{query}'")
            return []

        filter_conditions = [
            qdrant_models.FieldCondition(
                key="tenant_code",
                match=qdrant_models.MatchValue(value=tenant_code),
            )
        ]

        if environment:
            filter_conditions.append(
                qdrant_models.FieldCondition(
                    key="environment",
                    match=qdrant_models.MatchValue(value=environment),
                )
            )

        if geo_loc:
            filter_conditions.append(
                qdrant_models.FieldCondition(
                    key="geo_loc",
                    match=qdrant_models.MatchValue(value=geo_loc),
                )
            )

        client = await QdrantIntegration.get_client_async()
        if not client:
            return []

        try:
            results = await client.query_points(
                collection_name=self._collection_name,
                query=query_embedding,
                query_filter=qdrant_models.Filter(must=filter_conditions),
                limit=limit,
                score_threshold=score_threshold,
            )

            return [
                {
                    "service_code": hit.payload.get("service_code"),
                    "service_name": hit.payload.get("service_name"),
                    "environment": hit.payload.get("environment"),
                    "geo_loc": hit.payload.get("geo_loc"),
                    "score": hit.score,
                    "summary": hit.payload.get("summary_text"),
                    "config": hit.payload.get("config_json"),
                }
                for hit in results.points
            ]
        except Exception as e:
            logger.error(f"Config search failed: {e}")
            return []

    async def get_collection_info(self) -> Optional[Dict[str, Any]]:
        """Get info about the service_configs collection."""
        return await QdrantIntegration.get_collection_info(self._collection_name)
