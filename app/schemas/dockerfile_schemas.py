"""
Pydantic schemas for Dockerfile API
Provides models for fetching Dockerfiles from repositories and templates.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class DockerfileFromRepoResponse(BaseModel):
    """Response schema for Dockerfile from repository"""
    content: str = Field(..., description="Dockerfile content")
    repository: str = Field(..., description="Repository name")
    branch: str = Field(..., description="Branch name")
    path: str = Field(..., description="Dockerfile path")
    sha: Optional[str] = Field(None, description="Git SHA of the file")
    url: Optional[str] = Field(None, description="URL to the file on GitHub")


class DockerfileTemplateResponse(BaseModel):
    """Response schema for Dockerfile template"""
    content: str = Field(..., description="Dockerfile template content")
    language: str = Field(..., description="Language name (e.g., 'java', 'go')")
    version: Optional[str] = Field(None, description="Language version")
    template_name: str = Field(..., description="Template file name")
    template_path: str = Field(..., description="Template file path")


class DockerfileGenerateRequest(BaseModel):
    """Request schema for generating Dockerfile with replaced placeholders"""
    language_ref_code: str = Field(..., description="Language reference code")
    service_name: str = Field(..., description="Service name for Datadog/documentation")

    # Java-specific parameters
    enable_datadog: bool = Field(default=False, description="Enable Datadog APM (Java only)")
    xms: Optional[int] = Field(None, description="Java heap min in MB (Java only)")
    xmx: Optional[int] = Field(None, description="Java heap max in MB (Java only)")
    advanced_options: Optional[List[Dict[str, Any]]] = Field(None, description="Datadog advanced options (Java only)")

    # Go-specific parameters
    port: Optional[str] = Field(None, description="Application port (Go only, default: 8080)")
    go_config_path: Optional[str] = Field(None, description="Config file path (Go only)")
    use_aws_secrets: bool = Field(default=False, description="Enable AWS Secrets Manager (Go only)")

    # Generic build arguments (all languages)
    build_args: Optional[List[Dict[str, str]]] = Field(
        None,
        description="Custom Docker build arguments [{name: 'ARG_NAME', value: 'value'}]. Added as ARG declarations in Dockerfile."
    )


class DockerfileGenerateResponse(BaseModel):
    """Response schema for generated Dockerfile"""
    content: str = Field(..., description="Generated Dockerfile content with placeholders replaced")
    language: str = Field(..., description="Language name (e.g., 'java', 'go')")
    version: Optional[str] = Field(None, description="Language version")
    service_name: str = Field(..., description="Service name used in generation")
    parameters_used: Dict[str, Any] = Field(..., description="Parameters used for generation")


class DockerfileModifyRequest(BaseModel):
    """Request schema for modifying an existing Dockerfile with build args and memory settings"""
    repository: str = Field(..., description="Repository in format 'owner/repo'")
    branch: str = Field(..., description="Branch name")
    dockerfile_path: Optional[str] = Field(None, description="Path to Dockerfile in repository")
    service_config_code: Optional[str] = Field(None, description="Service config code to check for cached Dockerfile")

    # Java-specific parameters
    enable_datadog: bool = Field(default=False, description="Enable Datadog APM (Java only)")
    xms: Optional[int] = Field(None, description="Java heap min in MB")
    xmx: Optional[int] = Field(None, description="Java heap max in MB")
    advanced_options: Optional[List[Dict[str, Any]]] = Field(None, description="Datadog advanced options (Java only)")
    service_name: Optional[str] = Field(None, description="Service name for Datadog")

    # Generic build arguments (all languages)
    build_args: Optional[List[Dict[str, str]]] = Field(
        None,
        description="Custom Docker build arguments [{name: 'ARG_NAME', value: 'value'}]. Injected as ARG declarations."
    )


class DockerfileModifyResponse(BaseModel):
    """Response schema for modified Dockerfile"""
    content: str = Field(..., description="Modified Dockerfile content")
    repository: str = Field(..., description="Repository name")
    branch: str = Field(..., description="Branch name")
    path: str = Field(..., description="Dockerfile path")
    sha: Optional[str] = Field(None, description="Git SHA of the original file")
    modifications_applied: Dict[str, Any] = Field(
        ..., description="Summary of modifications applied (build_args_added, memory_updated, datadog_enabled)"
    )
