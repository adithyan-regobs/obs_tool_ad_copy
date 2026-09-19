"""
Database Users Schemas

Pydantic models for database users endpoint responses.
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class DatabaseUserGrant(BaseModel):
    """Grant/permission details for a database user (supports both MySQL and PostgreSQL)"""
    database: str = Field(..., description="Database name the grant applies to")
    table: Optional[str] = Field(None, description="Table name (MySQL only, use '*' for all tables)")
    schema_name: Optional[str] = Field(None, alias="schema", description="Schema name (PostgreSQL only)")
    object_type: Optional[str] = Field(None, description="Object type (PostgreSQL only: database, schema, table)")
    privileges: List[str] = Field(default=[], description="List of privileges (e.g., SELECT, INSERT, UPDATE)")

    class Config:
        populate_by_name = True


class DatabaseUserDetail(BaseModel):
    """Detailed information about a database user"""
    username: str = Field(..., description="Database username")
    password: str = Field(..., description="Database password")
    database_type: str = Field(..., description="Database type: mysql or postgresql")
    source_directory: str = Field(..., description="Directory name where user was found")
    grants: List[DatabaseUserGrant] = Field(default=[], description="List of database grants/permissions")


class DatabaseUserSummary(BaseModel):
    """Summary of a database user (username, password, and database type)"""
    username: str = Field(..., description="Database username")
    password: str = Field(..., description="Database password")
    database_type: str = Field(..., description="Database type: mysql or postgresql")
    server: str = Field(..., description="Server/subdirectory name (e.g., common-mysql, common-pg)")


class GetDatabaseUsersRequest(BaseModel):
    """Request for fetching database users from terragrunt files"""
    product_name: str = Field(..., description="Product name (e.g., 'genorim')", min_length=1)
    environment: str = Field(..., description="Environment name (e.g., dev, staging, prod)", min_length=1)
    github_repository: str = Field(..., description="GitHub repository in format 'owner/repo'", min_length=1)
    branch_name: str = Field(..., description="Target branch name", min_length=1)
    region: Optional[str] = Field(None, description="AWS region (auto-detected from environment if not provided)")
    tenant: Optional[str] = Field(None, description="Tenant identifier (e.g., 'vance', 'aspora') for tenant-specific path logic")
    geo_loc: Optional[str] = Field(None, description="Geographic location code (e.g., 'mumbai', 'london') for region mapping")


class GetDatabaseUsersResponse(BaseModel):
    """Response containing database users from terragrunt files"""
    success: bool = Field(..., description="Whether the operation was successful")
    users: List[DatabaseUserSummary] = Field(default=[], description="Simple list of usernames with database type")
    full_details: List[DatabaseUserDetail] = Field(default=[], description="Full user details with passwords and grants")
    servers: List[str] = Field(default=[], description="List of database server directory names (e.g., common-mysql, common-pg)")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (directories_scanned, files_parsed, etc.)"
    )
    message: str = Field(..., description="Result message")


# ============== Servers List Schemas ==============

class ServerInfo(BaseModel):
    """Server information with database type"""
    name: str = Field(..., description="Server directory name (e.g., 'common-mysql', 'common-pg')")
    type: Optional[str] = Field(None, description="Database type: 'mysql' or 'postgresql' (None if detection failed)")


class GetServersListRequest(BaseModel):
    """Request for fetching database server directories"""
    product_name: str = Field(..., description="Product name (e.g., 'core')", min_length=1)
    environment: str = Field(..., description="Environment name (e.g., dev, staging, prod)", min_length=1)
    github_repository: str = Field(..., description="GitHub repository in format 'owner/repo'", min_length=1)
    branch_name: str = Field(..., description="Target branch name", min_length=1)
    region: Optional[str] = Field(None, description="AWS region (auto-detected from environment if not provided)")
    tenant: Optional[str] = Field(None, description="Tenant identifier (e.g., 'vance', 'aspora') for tenant-specific path logic")
    geo_loc: Optional[str] = Field(None, description="Geographic location code (e.g., 'mumbai', 'london') for region mapping")


class GetServersListResponse(BaseModel):
    """Response containing list of database server directories with type info"""
    success: bool = Field(..., description="Whether the operation was successful")
    servers: List[ServerInfo] = Field(default=[], description="List of database servers with type information")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (base_path, server_count, etc.)"
    )
    message: str = Field(..., description="Result message")


# ============== Database List from Terragrunt Schemas ==============

class GetDatabaseListFromTerragruntRequest(BaseModel):
    """Request for fetching database names from terragrunt.hcl"""
    product_name: str = Field(..., description="Product name (e.g., 'core')", min_length=1)
    environment: str = Field(..., description="Environment name (e.g., dev, staging, prod)", min_length=1)
    github_repository: str = Field(..., description="GitHub repository in format 'owner/repo'", min_length=1)
    branch_name: str = Field(..., description="Target branch name", min_length=1)
    server_name: str = Field(..., description="Server/subdirectory name (e.g., 'common-mysql', 'common-pg')", min_length=1)
    region: Optional[str] = Field(None, description="AWS region (auto-detected from environment if not provided)")
    tenant: Optional[str] = Field(None, description="Tenant identifier (e.g., 'vance', 'aspora') for tenant-specific path logic")
    geo_loc: Optional[str] = Field(None, description="Geographic location code (e.g., 'mumbai', 'london') for region mapping")


class GetDatabaseListFromTerragruntResponse(BaseModel):
    """Response containing list of databases from terragrunt.hcl"""
    success: bool = Field(..., description="Whether the operation was successful")
    databases: List[str] = Field(default=[], description="List of database names")
    database_type: Optional[str] = Field(None, description="Database type: mysql or postgresql")
    server_name: str = Field(..., description="Server/subdirectory name")
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (file_path, etc.)"
    )
    message: str = Field(..., description="Result message")
