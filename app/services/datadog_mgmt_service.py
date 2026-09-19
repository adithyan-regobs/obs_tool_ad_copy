from typing import Dict, Any, Optional
import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.repository.datadog_alert_query_ref_repository import DatadogAlertQueryRefRepository
from app.repository.infrastructuretype_ref_repository import InfrastructureTypeRefRepository
from app.repository.alerttype_ref_repository import AlertTypeRefRepository
from app.schemas.datadog_schemas import CreateDatadogAlertQuery, UpdateDatadogAlertQuery
from app.domain.validators.datadog_mgmt_rules import DatadogValidationError
from app.core.enum import SignalKindEnum

logger = logging.getLogger(__name__)


class DatadogMgmtService:
    """
    Service layer for Datadog Management operations.
    Handles business logic for Datadog alert query templates.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        self.datadog_query_repository = DatadogAlertQueryRefRepository(session)
        self.infrastructure_type_repository = InfrastructureTypeRefRepository(session)
        self.alert_type_repository = AlertTypeRefRepository(session)

    async def create_datadog_alert_query(
        self,
        data: CreateDatadogAlertQuery
    ) -> Dict[str, Any]:
        """
        Create a new Datadog alert query template.

        Business Logic:
        - Validates that code is unique
        - Validates that infrastructure type exists
        - Validates that alert type exists
        - Creates the query template

        Args:
            data: CreateDatadogAlertQuery schema with all required fields

        Returns:
            Dict with created query details

        Raises:
            DatadogValidationError: If validation fails or code already exists
            ValueError: If infrastructure type or alert type not found

        Example:
            >>> result = await service.create_datadog_alert_query(data)
        """
        logger.info(
            f"Creating Datadog alert query '{data.code}'",
            extra={
                "query_code": data.code,
                "infrastructuretype": data.infrastructuretype_ref_code,
                "alerttype": data.alerttype_ref_code,
                "signal_kind": data.signal_kind.value
            }
        )

        # Step 1: Check if code already exists
        logger.debug(f"Checking if query code '{data.code}' already exists")
        existing_query = await self.datadog_query_repository.get_by_code(data.code)
        if existing_query:
            logger.warning(f"Query code '{data.code}' already exists")
            raise DatadogValidationError([f"Datadog alert query with code '{data.code}' already exists"])

        # Step 2: Validate infrastructure type exists
        logger.debug(f"Validating infrastructure type: {data.infrastructuretype_ref_code}")
        infrastructure_type = await self.infrastructure_type_repository.get_by_code(
            data.infrastructuretype_ref_code
        )
        if not infrastructure_type:
            logger.error(f"Infrastructure type not found: {data.infrastructuretype_ref_code}")
            raise ValueError(f"Infrastructure type '{data.infrastructuretype_ref_code}' not found")

        # Step 3: Validate alert type exists
        logger.debug(f"Validating alert type: {data.alerttype_ref_code}")
        alert_type = await self.alert_type_repository.get_by_code(
            data.alerttype_ref_code
        )
        if not alert_type:
            logger.error(f"Alert type not found: {data.alerttype_ref_code}")
            raise ValueError(f"Alert type '{data.alerttype_ref_code}' not found")

        # Step 4: Check if query already exists for this combination
        logger.debug(f"Checking for existing combination: {data.infrastructuretype_ref_code}/{data.alerttype_ref_code}/{data.signal_kind.value}")
        existing_combination = await self.datadog_query_repository.get_by_infra_alert_signal(
            infrastructuretype_ref_code=data.infrastructuretype_ref_code,
            alerttype_ref_code=data.alerttype_ref_code,
            signal_kind=data.signal_kind
        )
        if existing_combination:
            logger.warning(f"Query template combination already exists: {data.infrastructuretype_ref_code}/{data.alerttype_ref_code}/{data.signal_kind.value}")
            raise DatadogValidationError([
                f"Query template already exists for infrastructure type '{data.infrastructuretype_ref_code}', "
                f"alert type '{data.alerttype_ref_code}', and signal kind '{data.signal_kind.value}'"
            ])

        # Step 5: Create the query template
        query_data = {
            "code": data.code,
            "name": data.name,
            "description": data.description,
            "infrastructuretype_ref_code": data.infrastructuretype_ref_code,
            "alerttype_ref_code": data.alerttype_ref_code,
            "signal_kind": data.signal_kind,
            "query_template": data.query_template,
            "is_active": data.is_active,
            "is_deleted": False
        }

        created_query = await self.datadog_query_repository.create(**query_data)
        await self.session.commit()

        logger.info(
            f"Successfully created Datadog alert query '{data.code}' (ID: {created_query.id})",
            extra={"query_id": created_query.id, "query_code": created_query.code}
        )

        # Step 6: Return response
        return {
            "status": "success",
            "message": "Datadog alert query template created successfully",
            "query": {
                "id": created_query.id,
                "code": created_query.code,
                "name": created_query.name,
                "description": created_query.description,
                "infrastructuretype_ref_code": created_query.infrastructuretype_ref_code,
                "alerttype_ref_code": created_query.alerttype_ref_code,
                "signal_kind": created_query.signal_kind.value,
                "query_template": created_query.query_template,
                "is_active": created_query.is_active,
                "created_at": created_query.created_at
            }
        }

    async def get_all_datadog_alert_queries(
        self,
        infrastructuretype_ref_code: Optional[str] = None,
        alerttype_ref_code: Optional[str] = None,
        signal_kind: Optional[str] = None,
        is_active: Optional[bool] = None,
        skip: int = 0,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Get all Datadog alert query templates with optional filtering.

        Business Logic:
        - Validates pagination parameters
        - Applies multiple optional filters
        - Returns paginated results with total count

        Args:
            infrastructuretype_ref_code: Optional filter by infrastructure type
            alerttype_ref_code: Optional filter by alert type
            signal_kind: Optional filter by signal kind (string: 'metric', 'log', 'trace', 'event')
            is_active: Optional filter by active status
            skip: Pagination offset
            limit: Page size

        Returns:
            Dict with:
                - total: Total count of matching records
                - skip: Pagination offset used
                - limit: Page size used
                - queries: List of query template dictionaries

        Example:
            >>> result = await service.get_all_datadog_alert_queries(
            ...     infrastructuretype_ref_code='ec2',
            ...     skip=0,
            ...     limit=50
            ... )
        """
        logger.info(
            "Fetching Datadog alert queries",
            extra={
                "infrastructuretype": infrastructuretype_ref_code or 'All',
                "alerttype": alerttype_ref_code or 'All',
                "signal_kind": signal_kind or 'All'
            }
        )
        logger.debug(f"Query filters: is_active={is_active}, skip={skip}, limit={limit}")

        signal_kind_enum = None
        if signal_kind:
            try:
                signal_kind_enum = SignalKindEnum[signal_kind]
            except KeyError:
                logger.warning(f"Invalid signal_kind provided: {signal_kind}")
                raise DatadogValidationError([f"Invalid signal_kind '{signal_kind}'. Must be one of: metric, log, trace, event"])

        # Get queries from repository
        queries = await self.datadog_query_repository.get_all_queries(
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            alerttype_ref_code=alerttype_ref_code,
            signal_kind=signal_kind_enum,
            is_active=is_active,
            skip=skip,
            limit=limit
        )

        # Get total count with same filters
        total = await self.datadog_query_repository.count_queries(
            infrastructuretype_ref_code=infrastructuretype_ref_code,
            alerttype_ref_code=alerttype_ref_code,
            signal_kind=signal_kind_enum,
            is_active=is_active
        )

        # Convert model instances to dictionaries for response
        query_list = []
        for query in queries:
            query_list.append({
                "id": query.id,
                "code": query.code,
                "name": query.name,
                "description": query.description,
                "infrastructuretype_ref_code": query.infrastructuretype_ref_code,
                "alerttype_ref_code": query.alerttype_ref_code,
                "signal_kind": query.signal_kind.value,  # Convert enum to string
                "query_template": query.query_template,
                "is_active": query.is_active,
                "created_at": query.created_at,
                "updated_at": query.updated_at
            })

        logger.info(
            f"Successfully retrieved {total} Datadog alert queries",
            extra={"total": total, "returned": len(query_list)}
        )

        return {
            "total": total,
            "skip": skip,
            "limit": limit,
            "queries": query_list
        }

    async def update_datadog_alert_query(
        self,
        query_code: str,
        data: UpdateDatadogAlertQuery
    ) -> Dict[str, Any]:
        """
        Update an existing Datadog alert query template.

        Business Logic:
        - Validates that query with given code exists
        - Only updates fields that are provided (partial update)
        - Returns updated query details

        Args:
            query_code: Unique code of the query to update
            data: UpdateDatadogAlertQuery schema with optional fields

        Returns:
            Dict with updated query details

        Raises:
            ValueError: If query not found
            DatadogValidationError: If no fields provided for update

        Example:
            >>> result = await service.update_datadog_alert_query(
            ...     query_code='ec2_cpu_metric',
            ...     data=UpdateDatadogAlertQuery(name='New Name', is_active=False)
            ... )
        """
        logger.info(
            f"Updating Datadog alert query '{query_code}'",
            extra={"query_code": query_code}
        )

        # Step 1: Check if query exists
        logger.debug(f"Checking if query '{query_code}' exists")
        existing_query = await self.datadog_query_repository.get_by_code(query_code)
        if not existing_query:
            logger.error(f"Query not found: {query_code}")
            raise ValueError(f"Datadog alert query with code '{query_code}' not found")

        # Step 2: Check if query is deleted
        logger.debug(f"Checking if query '{query_code}' is deleted")
        if existing_query.is_deleted:
            logger.warning(f"Attempted to update deleted query: {query_code}")
            raise ValueError(f"Datadog alert query with code '{query_code}' has been deleted")

        # Step 3: Build updates dictionary (only include provided fields)
        updates = {}
        if data.name is not None:
            updates['name'] = data.name
        if data.description is not None:
            updates['description'] = data.description
        if data.query_template is not None:
            updates['query_template'] = data.query_template
        if data.is_active is not None:
            updates['is_active'] = data.is_active

        logger.debug(f"Update fields for '{query_code}': {list(updates.keys())}")

        # Step 4: Validate that at least one field is being updated
        if not updates:
            logger.warning(f"No fields provided for update: {query_code}")
            raise DatadogValidationError(["No fields provided for update"])

        # Step 5: Update the query template
        updated_query = await self.datadog_query_repository.update_query(
            query=existing_query,
            name=data.name,
            code=data.code,
            infrastructuretype_ref_code=data.infrastructuretype_ref_code,
            alerttype_ref_code=data.alerttype_ref_code,
            signal_kind=data.signal_kind,
            description=data.description,
            query_template=data.query_template,
            is_active=data.is_active
        )
        await self.session.commit()

        logger.info(
            f"Successfully updated Datadog alert query '{query_code}'",
            extra={"query_id": updated_query.id, "query_code": updated_query.code}
        )

        # Step 6: Return response
        return {
            "status": "success",
            "message": "Datadog alert query template updated successfully",
            "query": {
                "id": updated_query.id,
                "code": updated_query.code,
                "name": updated_query.name,
                "description": updated_query.description,
                "infrastructuretype_ref_code": updated_query.infrastructuretype_ref_code,
                "alerttype_ref_code": updated_query.alerttype_ref_code,
                "signal_kind": updated_query.signal_kind.value,
                "query_template": updated_query.query_template,
                "is_active": updated_query.is_active,
                "created_at": updated_query.created_at,
                "updated_at": updated_query.updated_at
            }
        }