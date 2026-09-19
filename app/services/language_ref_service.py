"""
Language Reference Service
Business logic for language reference operations
"""
import logging
from typing import Optional, List, Dict
from collections import defaultdict
from sqlalchemy.ext.asyncio import AsyncSession
from app.repository.language_ref_repository import LanguageRefRepository
from app.schemas.language_ref_schemas import (
    LanguageRefResponse,
    LanguageVersionsResponse,
    LanguageRefGroupedResponse,
    LanguageVersionsGroupedResponse
)

logger = logging.getLogger(__name__)


class LanguageRefService:
    """Service for language reference operations"""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.language_ref_repository = LanguageRefRepository(session)

    async def get_all_language_versions(
        self,
        platform: Optional[str] = None
    ) -> LanguageVersionsResponse:
        """
        Get all active language versions.

        Args:
            platform: Optional filter by CI/CD platform (e.g., 'github_actions', 'gitlab_ci')

        Returns:
            LanguageVersionsResponse with list of all language versions
        """
        logger.info(f"Getting all language versions (platform filter: {platform})")

        # Get filtered results
        if platform:
            languages = await self.language_ref_repository.get_filtered(
                platform=platform,
                is_active=True
            )
        else:
            languages = await self.language_ref_repository.get_all_active()

        logger.info(f"Found {len(languages)} language versions")

        # Convert to response schema
        language_responses = [
            LanguageRefResponse.model_validate(lang)
            for lang in languages
        ]

        return LanguageVersionsResponse(
            total=len(language_responses),
            languages=language_responses
        )

    async def get_language_versions_grouped(
        self,
        platform: Optional[str] = None
    ) -> LanguageVersionsGroupedResponse:
        """
        Get language versions grouped by base language name.

        Args:
            platform: Optional filter by CI/CD platform (e.g., 'github_actions', 'gitlab_ci')

        Returns:
            LanguageVersionsGroupedResponse with languages grouped by name
        """
        logger.info(f"Getting grouped language versions (platform filter: {platform})")

        # Get all languages
        if platform:
            languages = await self.language_ref_repository.get_filtered(
                platform=platform,
                is_active=True
            )
        else:
            languages = await self.language_ref_repository.get_all_active()

        # Group by base language name (extract from full name)
        grouped: Dict[str, List[LanguageRefResponse]] = defaultdict(list)

        for lang in languages:
            # Extract base language name (e.g., "Python" from "Python 3.12")
            # Special handling for Java: distinguish between Gradle and Maven based on code
            if lang.code and lang.code.startswith("JAVA-MAVEN"):
                base_name = "Java Maven"
            elif lang.code and lang.code.startswith("JAVA"):
                base_name = "Java Gradle"
            else:
                base_name = lang.name.split()[0] if lang.name else "Unknown"
            grouped[base_name].append(LanguageRefResponse.model_validate(lang))

        # Convert to grouped response format
        grouped_responses = [
            LanguageRefGroupedResponse(
                language_name=lang_name,
                versions=versions
            )
            for lang_name, versions in sorted(grouped.items())
        ]

        total_versions = sum(len(group.versions) for group in grouped_responses)

        logger.info(f"Found {len(grouped_responses)} languages with {total_versions} total versions")

        return LanguageVersionsGroupedResponse(
            total_languages=len(grouped_responses),
            total_versions=total_versions,
            languages=grouped_responses
        )

    async def get_language_by_code(self, code: str) -> Optional[LanguageRefResponse]:
        """
        Get a specific language version by code.

        Args:
            code: Language reference code

        Returns:
            LanguageRefResponse if found, None otherwise
        """
        logger.info(f"Getting language version by code: {code}")

        language = await self.language_ref_repository.get_by_code(code)

        if not language:
            logger.warning(f"Language version not found: {code}")
            return None

        return LanguageRefResponse.model_validate(language)

    async def get_languages_for_platform(
        self,
        platform: str
    ) -> LanguageVersionsResponse:
        """
        Get all language versions that support a specific CI/CD platform.

        Args:
            platform: CI/CD platform name (e.g., 'github_actions', 'gitlab_ci')

        Returns:
            LanguageVersionsResponse with filtered languages
        """
        logger.info(f"Getting languages for platform: {platform}")

        languages = await self.language_ref_repository.get_by_platform(platform)

        logger.info(f"Found {len(languages)} languages for {platform}")

        language_responses = [
            LanguageRefResponse.model_validate(lang)
            for lang in languages
        ]

        return LanguageVersionsResponse(
            total=len(language_responses),
            languages=language_responses
        )
