"""
LangFuse Service for logging chat interactions and monitoring
"""
import asyncio
import logging
import os
from typing import Dict, Any, Optional
from datetime import datetime

try:
    from langfuse import Langfuse, get_client
    from langfuse.langchain import CallbackHandler
    LANGFUSE_AVAILABLE = True
except ImportError as e:
    logger_temp = logging.getLogger(__name__)
    logger_temp.warning(f"Langfuse import failed: {e}")
    LANGFUSE_AVAILABLE = False
    Langfuse = None
    get_client = None
    CallbackHandler = None

from app.core.config import settings
from app.utils.langfuse_helper import LangfuseHelper

logger = logging.getLogger(__name__)


class LangFuseService:
    """Service for logging chat interactions to LangFuse"""

    def __init__(self):
        self.client = None
        self._initialized = False

    def _initialize_client(self):
        """Lazy initialization of LangFuse client"""
        if self._initialized:
            return

        try:
            if settings.langfuse_secret_key and settings.langfuse_public_key:
                # Set environment variables for LangFuse client
                os.environ['LANGFUSE_SECRET_KEY'] = settings.langfuse_secret_key
                os.environ['LANGFUSE_PUBLIC_KEY'] = settings.langfuse_public_key
                os.environ['LANGFUSE_HOST'] = settings.langfuse_host

                # Initialize client
                self.client = Langfuse()
                logger.info("LangFuse client initialized successfully")
            else:
                logger.warning("LangFuse keys not configured, logging disabled")
                self.client = None
        except Exception as e:
            logger.error(f"Failed to initialize LangFuse client: {str(e)}")
            self.client = None
        finally:
            self._initialized = True

    async def log_chat_interaction(
        self,
        user_query: str,
        ai_response: str,
        metadata: Dict[str, Any]
    ) -> bool:
        """
        Log a chat interaction to LangFuse

        Args:
            user_query: The user's input message
            ai_response: The AI's response
            metadata: Additional context (user_id, chat_info_code, etc.)

        Returns:
            bool: True if logging successful, False otherwise
        """
        try:
            self._initialize_client()

            if not self.client:
                logger.debug("LangFuse client not available, skipping logging")
                return False

            # Run in thread pool to avoid blocking async operations
            await asyncio.get_event_loop().run_in_executor(
                None,
                self._log_sync,
                user_query,
                ai_response,
                metadata
            )

            return True

        except Exception as e:
            logger.error(f"Failed to log chat interaction: {str(e)}")
            return False

    def _log_sync(self, user_query: str, ai_response: str, metadata: Dict[str, Any]):
        """Synchronous logging method to run in thread pool"""
        try:
            # Extract metadata
            user_id = metadata.get("user_mst_code", "anonymous")
            tenant_id = metadata.get("tenants_mst_code", "unknown")
            chat_code = metadata.get("chat_info_code", "")
            infra_vendor = metadata.get("infra_vendor_enum", "")
            environment = metadata.get("environment_enum", "")
            application = metadata.get("applications_mst_code", "")
            resource_group = metadata.get("resource_group_mst_code", "")
            service = metadata.get("services_mst_code", "")
            model_name = metadata.get("model_used", settings.openai_model)
            terraform_code = metadata.get("terraform_code")

            generation_metadata = {
                "user_id": user_id,
                "tenant_id": tenant_id,
                "chat_code": chat_code,
                "infra_vendor": infra_vendor,
                "environment": environment,
                "application": application,
                "resource_group": resource_group,
                "service": service,
                "has_terraform_code": terraform_code is not None,
                "timestamp": datetime.utcnow().isoformat()
            }

            # Structure input as chat format with conversation history
            chat_input = []

            # Add conversation messages if available
            conversation_messages = metadata.get("conversation_messages", [])
            if conversation_messages:
                seen_messages = set()
                summary_content = ""

                for msg in conversation_messages:
                    msg_type = msg.__class__.__name__
                    content = getattr(msg, 'content', '').strip()

                    if not content:
                        continue

                    # Extract conversation summary if present
                    if msg_type == "SystemMessage" and "Summary of earlier conversation" in content:
                        summary_content = content
                        logger.info(f"[LANGFUSE] Found conversation summary: {len(content)} chars")
                        continue

                    # Create unique identifier to avoid duplicates
                    msg_id = f"{msg_type}:{content[:100]}"
                    if msg_id in seen_messages:
                        continue
                    seen_messages.add(msg_id)

                    if msg_type == "SystemMessage":
                        chat_input.append({"role": "system", "content": content})
                    elif msg_type == "HumanMessage":
                        chat_input.append({"role": "user", "content": content})
                    elif msg_type == "AIMessage":
                        chat_input.append({"role": "assistant", "content": content})

                # Add conversation summary at the beginning if found
                if summary_content and chat_input:
                    insert_index = 1 if chat_input and chat_input[0]["role"] == "system" else 0
                    chat_input.insert(insert_index, {
                        "role": "system",
                        "content": f"📋 {summary_content}"
                    })
                    logger.info(f"[LANGFUSE] Added conversation summary to chat_input at index {insert_index}")
            else:
                # Fallback: just add the current query
                infrastructure_context = metadata.get("infrastructure_context", "")
                if infrastructure_context:
                    chat_input.append({"role": "system", "content": f"Infrastructure Context:\n{infrastructure_context}"})
                chat_input.append({"role": "user", "content": user_query})

            # Log to LangFuse
            with self.client.start_as_current_generation(
                name="infrastructure_chat_interaction",
                model=model_name,
                input=chat_input,
                output=ai_response,
                metadata=generation_metadata
            ) as generation:
                # Update trace-level info (user_id, session_id belong here)
                generation.update_trace(
                    user_id=user_id,
                    session_id=chat_code
                )

            # Flush to ensure data is sent
            self.client.flush()
            logger.debug(f"Successfully logged chat interaction for session {chat_code}")

        except Exception as e:
            logger.error(f"Sync logging failed: {str(e)}")
            raise

    async def log_error(
        self,
        error: Exception,
        context: Dict[str, Any]
    ) -> bool:
        """
        Log errors to LangFuse for monitoring

        Args:
            error: The exception that occurred
            context: Additional context about the error

        Returns:
            bool: True if logging successful, False otherwise
        """
        try:
            self._initialize_client()

            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                self._log_error_sync,
                error,
                context
            )

            return True

        except Exception as e:
            logger.error(f"Failed to log error: {str(e)}")
            return False

    def _log_error_sync(self, error: Exception, context: Dict[str, Any]):
        """Synchronous error logging"""
        try:
            user_id = context.get("user_mst_code", "unknown")
            chat_code = context.get("chat_info_code", "unknown")

            # Create a span for the error using the correct API
            with self.client.start_as_current_span(
                name="chat_error"
            ) as span:
                # Update the span with error details
                span.update(
                    level="ERROR",
                    status_message=str(error),
                    user_id=user_id,
                    session_id=chat_code,
                    metadata={
                        "user_id": user_id,
                        "chat_info_code": chat_code,
                        "error_type": type(error).__name__,
                        "error_message": str(error),
                        "context": context,
                        "timestamp": datetime.utcnow().isoformat()
                    }
                )

            self.client.flush()
            logger.debug("Successfully logged error to LangFuse")

        except Exception as e:
            logger.error(f"Sync error logging failed: {str(e)}")
            raise

    # ==================== Service Sage & Vector DB Monitoring ====================

    async def log_service_sage_interaction(
        self,
        user_query: str,
        ai_response: str,
        intent: str,
        chat_info_code: str,
        user_id: str,
        tenant_id: str,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """Log Service Sage chat interaction. Non-blocking via thread pool."""
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_service_sage_generation,
                self.client, user_query, ai_response, intent,
                chat_info_code, user_id, tenant_id, metadata or {}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log Service Sage interaction: {e}")
            return False

    async def log_llm_call(
        self,
        call_type: str,
        input_text: str,
        output_text: str,
        latency_ms: float,
        metadata: Dict[str, Any] = None,
        usage: Dict[str, int] = None,
    ) -> bool:
        """Log LLM call (intent detection, response generation). Non-blocking."""
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_llm_span,
                self.client, call_type, input_text, output_text, latency_ms, metadata or {}, usage
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log LLM call: {e}")
            return False

    async def log_embedding_operation(
        self,
        operation_type: str,
        text_count: int,
        latency_ms: float,
        success: bool,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """Log embedding operation. Non-blocking via thread pool."""
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_embedding_span,
                self.client, operation_type, text_count, latency_ms, success, metadata or {}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log embedding operation: {e}")
            return False

    async def log_vector_search(
        self,
        collection_name: str,
        query_text: str,
        result_count: int,
        top_score: float = None,
        latency_ms: float = 0,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """Log vector search operation. Non-blocking via thread pool."""
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_vector_search_span,
                self.client, collection_name, query_text, result_count,
                top_score, latency_ms, metadata or {}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log vector search: {e}")
            return False

    async def log_parameter_match(
        self,
        query: str,
        match_path: str,
        canonical_name: str = None,
        confidence: str = None,
        score: float = 0,
        latency_ms: float = 0,
        alternatives_count: int = 0,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """Log parameter matching result. Non-blocking via thread pool."""
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_parameter_match_span,
                self.client, query, match_path, canonical_name, confidence,
                score, latency_ms, alternatives_count, metadata or {}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log parameter match: {e}")
            return False

    async def log_workflow_execution(
        self,
        workflow_name: str,
        chat_info_code: str,
        user_id: str,
        tenant_id: str,
        nodes_executed: list,
        latency_ms: float,
        success: bool,
        error_message: str = None,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """
        Log complete workflow execution as a trace with spans.

        Args:
            workflow_name: Name of the workflow (e.g., "service_config_agent")
            chat_info_code: Session ID
            user_id: User identifier
            tenant_id: Tenant identifier
            nodes_executed: List of nodes that were executed
            latency_ms: Total execution time
            success: Whether workflow succeeded
            error_message: Optional error message
            metadata: Additional metadata

        Returns:
            True if logging successful
        """
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                self._log_workflow_sync,
                workflow_name, chat_info_code, user_id, tenant_id,
                nodes_executed, latency_ms, success, error_message, metadata or {}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log workflow execution: {e}")
            return False

    def _log_workflow_sync(
        self,
        workflow_name: str,
        chat_info_code: str,
        user_id: str,
        tenant_id: str,
        nodes_executed: list,
        latency_ms: float,
        success: bool,
        error_message: str,
        metadata: Dict[str, Any]
    ):
        """Synchronous workflow logging."""
        try:
            # Create trace for the workflow
            self.client.trace(
                name=workflow_name,
                user_id=user_id,
                session_id=chat_info_code,
                metadata={
                    "tenant_id": tenant_id,
                    "nodes_executed": nodes_executed,
                    "node_count": len(nodes_executed),
                    "success": success,
                    "latency_ms": round(latency_ms, 2),
                    **metadata,
                }
            )

            # Add span for overall workflow execution
            with self.client.start_as_current_span(
                name=f"{workflow_name}_execution"
            ) as span:
                span.update(
                    level="ERROR" if not success else "DEFAULT",
                    status_message=error_message if error_message else "Success",
                    metadata={
                        "nodes": nodes_executed,
                        "duration_ms": round(latency_ms, 2),
                    }
                )

            self.client.flush()
            logger.debug(f"Logged workflow '{workflow_name}' execution")
        except Exception as e:
            logger.error(f"Sync workflow logging failed: {e}")

    async def log_node_execution(
        self,
        node_name: str,
        latency_ms: float,
        success: bool,
        input_summary: str = None,
        output_summary: str = None,
        metadata: Dict[str, Any] = None
    ) -> bool:
        """
        Log individual node execution within a workflow.

        Args:
            node_name: Name of the node
            latency_ms: Node execution time
            success: Whether node succeeded
            input_summary: Brief summary of input
            output_summary: Brief summary of output
            metadata: Additional metadata

        Returns:
            True if logging successful
        """
        try:
            self._initialize_client()
            if not self.client:
                return False

            await asyncio.get_event_loop().run_in_executor(
                None,
                LangfuseHelper.log_llm_span,
                self.client,
                f"node:{node_name}",
                input_summary or "",
                output_summary or "",
                latency_ms,
                {"success": success, **(metadata or {})}
            )
            return True
        except Exception as e:
            logger.error(f"Failed to log node execution: {e}")
            return False

    def get_callback_handler(
        self,
        trace_name: str = None,
        user_id: str = None,
        session_id: str = None,
        metadata: Dict[str, Any] = None
    ) -> Optional[CallbackHandler]:
        """
        Get Langfuse callback handler for LangChain/LangGraph integration (v3.x).
        
        This is the recommended way to integrate Langfuse with LangChain.
        The callback handler automatically tracks:
        - All LLM calls (with token usage)
        - Traces for entire workflow executions
        - Spans for individual nodes
        - Generations for LLM responses
        
        Args:
            trace_name: Name for the trace (e.g., "infra_chat_workflow") - optional
            user_id: User identifier for trace grouping - pass via metadata instead
            session_id: Session/conversation identifier - pass via metadata instead
            metadata: Additional metadata for the trace (use langfuse_user_id, langfuse_session_id keys)
            
        Returns:
            CallbackHandler instance or None if Langfuse not configured
            
        Example:
            handler = langfuse_service.get_callback_handler(
                trace_name="infra_chat",
                metadata={
                    "langfuse_user_id": "user123",
                    "langfuse_session_id": "conv456",
                    "langfuse_tags": ["production", "infra"],
                    "tenant_id": "acme"
                }
            )
            
            # Pass to LangGraph
            result = await graph.ainvoke(
                state,
                config={
                    "callbacks": [handler],
                    "metadata": {
                        "langfuse_user_id": "user123",
                        "langfuse_session_id": "conv456"
                    }
                }
            )
            
            # Or pass to individual LLM calls
            response = llm.invoke(messages, config={"callbacks": [handler]})
        """
        try:
            if not LANGFUSE_AVAILABLE or CallbackHandler is None:
                logger.debug("Langfuse callback handler not available")
                return None
            
            if not settings.langfuse_secret_key or not settings.langfuse_public_key:
                logger.debug("Langfuse keys not configured, callback handler disabled")
                return None
            
            # Initialize client (sets up environment variables)
            self._initialize_client()
            
            # v3.x: CallbackHandler takes no arguments - it uses singleton get_client()
            # Metadata is passed via the config dict in invoke() calls
            handler = CallbackHandler()
            
            logger.debug(f"Created Langfuse callback handler for trace: {trace_name}")
            return handler
            
        except Exception as e:
            logger.error(f"Failed to create Langfuse callback handler: {str(e)}")
            return None

    def shutdown(self):
        """Cleanup resources"""
        try:
            if self.client:
                self.client.flush()
                logger.info("LangFuse service shutdown complete")
        except Exception as e:
            logger.error(f"Error during LangFuse shutdown: {str(e)}")


# Singleton instance
langfuse_service = LangFuseService()