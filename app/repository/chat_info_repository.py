"""
Chat Info Repository for managing chat sessions
"""
import hashlib
from typing import Optional, List
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.db.models.chat_info_model import ChatInfoModel
from app.db.models.chat_message_model import ChatMessageModel
from app.db.models.case_type_ref_model import CaseTypeRefModel
from app.db.models.applications_mst_model import ApplicationsMstModel
from app.db.models.services_mst_model import ServicesMstModel
from app.db.models.geo_loc_mst_model import GeoLocMstModel
from app.repository.base_repository import BaseRepository
from app.core.enum import EnvironmentEnum, InfraVendorEnum


class ChatInfoRepository(BaseRepository[ChatInfoModel]):
    """Repository for chat_info table operations"""

    def __init__(self, session: AsyncSession):
        super().__init__(ChatInfoModel, session)

    def _generate_chat_code(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        infra_vendor_enum: InfraVendorEnum,
        applications_mst_code: Optional[str] = None,
        resource_group_mst_code: Optional[str] = None,
        services_mst_code: Optional[str] = None,
        environment_enum: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        case_type_ref_code: Optional[str] = None,
    ) -> str:
        """
        Generate deterministic chat_info_code based on context combination.
        Same context = same code = same chat session.

        Context includes: geo_loc + case_type + product + env + vendor + service + user + tenant
        """
        context_string = (
            f"{tenants_mst_code}|"
            f"{user_mst_code}|"
            f"{geo_loc_mst_code or 'none'}|"
            f"{case_type_ref_code or 'none'}|"
            f"{applications_mst_code or 'none'}|"
            f"{environment_enum.value if environment_enum else 'none'}|"
            f"{infra_vendor_enum.value}|"
            f"{services_mst_code or 'none'}"
        )
        hash_digest = hashlib.sha256(context_string.encode()).hexdigest()
        return f"CHAT_{hash_digest[:16].upper()}"

    async def find_or_create(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        infra_vendor_enum: InfraVendorEnum,
        applications_mst_code: Optional[str] = None,
        resource_group_mst_code: Optional[str] = None,
        services_mst_code: Optional[str] = None,
        environment_enum: Optional[EnvironmentEnum] = None,
        geo_loc_mst_code: Optional[str] = None,
        case_type_ref_code: Optional[str] = None,
    ) -> tuple[ChatInfoModel, bool]:
        """
        Find existing chat_info by context combination or create new one.

        Context: geo_loc + case_type + product + env + vendor + service + user + tenant

        Returns:
            tuple[ChatInfoModel, bool]: (chat_info, is_new)
                - chat_info: The found or created chat_info record
                - is_new: True if newly created, False if found existing
        """
        # Generate deterministic code
        chat_code = self._generate_chat_code(
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            infra_vendor_enum=infra_vendor_enum,
            applications_mst_code=applications_mst_code,
            resource_group_mst_code=resource_group_mst_code,
            services_mst_code=services_mst_code,
            environment_enum=environment_enum,
            geo_loc_mst_code=geo_loc_mst_code,
            case_type_ref_code=case_type_ref_code,
        )

        # Try to find existing chat_info
        stmt = select(ChatInfoModel).where(ChatInfoModel.code == chat_code)
        result = await self.session.execute(stmt)
        existing_chat = result.scalar_one_or_none()

        if existing_chat:
            return existing_chat, False

        # Generate a descriptive name for the chat session
        # Format: {case_type} - {product} ({env}, {geo_loc}) - {service}
        name_parts = []
        if case_type_ref_code:
            name_parts.append(case_type_ref_code.replace("_", " ").title())
        if applications_mst_code:
            name_parts.append(applications_mst_code)

        env_geo_parts = []
        if environment_enum:
            env_geo_parts.append(environment_enum.value)
        if geo_loc_mst_code:
            env_geo_parts.append(geo_loc_mst_code)

        if env_geo_parts:
            name_parts.append(f"({', '.join(env_geo_parts)})")

        if services_mst_code:
            name_parts.append(f"- {services_mst_code}")

        chat_name = " ".join(name_parts) if name_parts else f"Chat - {infra_vendor_enum.value.upper()}"

        # Create new chat_info
        new_chat = ChatInfoModel(
            code=chat_code,
            name=chat_name,
            description=f"Infrastructure chat session for {tenants_mst_code}",
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            infra_vendor_enum=infra_vendor_enum,
            applications_mst_code=applications_mst_code,
            resource_group_mst_code=resource_group_mst_code,
            services_mst_code=services_mst_code,
            environment_enum=environment_enum,
            geo_loc_mst_code=geo_loc_mst_code,
            case_type_ref_code=case_type_ref_code,
        )

        self.session.add(new_chat)
        await self.session.flush()
        await self.session.refresh(new_chat)

        return new_chat, True

    # =========================================================================
    # Chat History Methods
    # =========================================================================

    async def get_user_chat_history(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        limit: int = 50,
    ) -> List[dict]:
        """
        Get all chat sessions for a user, ordered by most recent message activity.

        Returns a list of chat history items with message counts, last activity,
        and human-readable names from joined tables.
        """
        # Subquery to get last message timestamp and count per chat
        # HAVING clause ensures only chats with messages are included
        message_stats = (
            select(
                ChatMessageModel.chat_info_code,
                func.max(ChatMessageModel.created_at).label("last_message_at"),
                func.count(ChatMessageModel.id).label("message_count"),
            )
            .group_by(ChatMessageModel.chat_info_code)
            .having(func.count(ChatMessageModel.id) > 0)
            .subquery()
        )

        # Main query joining chat_info with message stats and related tables for names
        stmt = (
            select(
                ChatInfoModel,
                message_stats.c.last_message_at,
                message_stats.c.message_count,
                CaseTypeRefModel.name.label("case_type_name"),
                ApplicationsMstModel.name.label("application_name"),
                ServicesMstModel.name.label("service_name"),
                GeoLocMstModel.name.label("geo_loc_name"),
            )
            .join(
                message_stats,
                ChatInfoModel.code == message_stats.c.chat_info_code,
            )
            .outerjoin(
                CaseTypeRefModel,
                ChatInfoModel.case_type_ref_code == CaseTypeRefModel.code,
            )
            .outerjoin(
                ApplicationsMstModel,
                ChatInfoModel.applications_mst_code == ApplicationsMstModel.code,
            )
            .outerjoin(
                ServicesMstModel,
                ChatInfoModel.services_mst_code == ServicesMstModel.code,
            )
            .outerjoin(
                GeoLocMstModel,
                ChatInfoModel.geo_loc_mst_code == GeoLocMstModel.code,
            )
            .where(
                ChatInfoModel.tenants_mst_code == tenants_mst_code,
                ChatInfoModel.user_mst_code == user_mst_code,
                ChatInfoModel.is_deleted == False,
                ~ChatInfoModel.code.like("SVCCHAT_%"),  # Exclude Service Config chats
            )
            .order_by(desc(message_stats.c.last_message_at))
            .limit(limit)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        history_items = []
        for row in rows:
            chat_info = row[0]
            last_message_at = row[1]
            message_count = row[2] or 0
            case_type_name = row[3]
            application_name = row[4]
            service_name = row[5]
            geo_loc_name = row[6]

            # Only include chats that have at least one message
            # Explicit check for message_count being truthy prevents NULL/0 from passing
            if message_count and message_count > 0:
                # Build display name from joined names
                # Format: {case_type} {app} ({env}, {geo}) - {service}
                display_parts = []
                if case_type_name:
                    display_parts.append(case_type_name)
                if application_name:
                    display_parts.append(application_name)

                env_geo_parts = []
                if chat_info.environment_enum:
                    env_geo_parts.append(chat_info.environment_enum.value)
                if geo_loc_name:
                    env_geo_parts.append(geo_loc_name)
                if env_geo_parts:
                    display_parts.append(f"({', '.join(env_geo_parts)})")

                if service_name:
                    display_parts.append(f"- {service_name}")

                display_name = " ".join(display_parts) if display_parts else f"Chat - {chat_info.infra_vendor_enum.value.upper()}"

                history_items.append({
                    "chat_info_code": chat_info.code,
                    "name": display_name,
                    "geo_loc_mst_code": chat_info.geo_loc_mst_code,
                    "geo_loc_name": geo_loc_name,
                    "case_type_ref_code": chat_info.case_type_ref_code,
                    "case_type_name": case_type_name,
                    "applications_mst_code": chat_info.applications_mst_code,
                    "application_name": application_name,
                    "environment_enum": chat_info.environment_enum,
                    "infra_vendor_enum": chat_info.infra_vendor_enum,
                    "services_mst_code": chat_info.services_mst_code,
                    "service_name": service_name,
                    "resource_group_mst_code": chat_info.resource_group_mst_code,
                    "last_message_at": last_message_at,
                    "message_count": message_count,
                })

        return history_items

    async def get_by_code(self, code: str) -> Optional[ChatInfoModel]:
        """Get chat_info by code"""
        stmt = select(ChatInfoModel).where(ChatInfoModel.code == code)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    # =========================================================================
    # Service Config Chat Methods
    # Uses different context hash: svc_config | tenant | user | geo_loc | env | service
    # =========================================================================

    def _generate_service_config_chat_code(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        geo_loc_mst_code: str,
        environment_enum: EnvironmentEnum,
        services_mst_code: Optional[str] = None,
    ) -> str:
        """
        Generate deterministic chat code for service config context.

        Uses different context than langchat:
        - Langchat: tenant | user | vendor | app | resource_group | service | env
        - Service config: svc_config | tenant | user | geo_loc | env | service

        The 'svc_config' prefix ensures no collision with langchat hashes.
        """
        context_string = (
            f"svc_config|"
            f"{tenants_mst_code}|"
            f"{user_mst_code}|"
            f"{geo_loc_mst_code}|"
            f"{environment_enum.value}|"
            f"{services_mst_code or 'none'}"
        )
        hash_digest = hashlib.sha256(context_string.encode()).hexdigest()
        return f"SVCCHAT_{hash_digest[:16].upper()}"

    async def find_or_create_service_config_chat(
        self,
        tenants_mst_code: str,
        user_mst_code: str,
        geo_loc_mst_code: str,
        environment_enum: EnvironmentEnum,
        services_mst_code: Optional[str] = None,
    ) -> tuple[ChatInfoModel, bool]:
        """
        Find or create chat session for service config context.

        Args:
            tenants_mst_code: Tenant code (from JWT)
            user_mst_code: User code (from JWT)
            geo_loc_mst_code: Geographic location code (required)
            environment_enum: Environment (required)
            services_mst_code: Service code (optional, set after selection)

        Returns:
            tuple[ChatInfoModel, bool]: (chat_info, is_new)
        """
        chat_code = self._generate_service_config_chat_code(
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            geo_loc_mst_code=geo_loc_mst_code,
            environment_enum=environment_enum,
            services_mst_code=services_mst_code,
        )

        # Try to find existing
        stmt = select(ChatInfoModel).where(ChatInfoModel.code == chat_code)
        result = await self.session.execute(stmt)
        existing_chat = result.scalar_one_or_none()

        if existing_chat:
            return existing_chat, False

        # Build descriptive name
        name_parts = ["Service Config", geo_loc_mst_code.upper(), environment_enum.value.upper()]
        if services_mst_code:
            name_parts.append(services_mst_code)
        chat_name = " - ".join(name_parts)

        # Create new chat_info with geo_loc
        new_chat = ChatInfoModel(
            code=chat_code,
            name=chat_name,
            description=f"Service config assistance for {tenants_mst_code}",
            tenants_mst_code=tenants_mst_code,
            user_mst_code=user_mst_code,
            geo_loc_mst_code=geo_loc_mst_code,
            environment_enum=environment_enum,
            services_mst_code=services_mst_code,
            infra_vendor_enum=InfraVendorEnum.aws,  # Default for ECS services
        )

        self.session.add(new_chat)
        await self.session.flush()
        await self.session.refresh(new_chat)

        return new_chat, True
