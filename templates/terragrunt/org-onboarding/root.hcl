terraform_binary = "terraform"

locals {
  versions = read_terragrunt_config("${get_repo_root()}/versions.hcl")
  env_vars = read_terragrunt_config(find_in_parent_folders("env.hcl"))
  ctx      = local.env_vars.locals.context

  # Derived — never hardcoded
  state_bucket    = "${local.ctx.organization}-${local.ctx.env}-${local.ctx.region}-${local.ctx.index}-state-bucket"
    assume_role_arn = "arn:aws:iam::${local.ctx.account_id}:role/atlantis-assume-role"
}

terragrunt_version_constraint = local.versions.locals.terragrunt

remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket       = local.state_bucket
    key          = "${path_relative_to_include()}/terraform.tfstate"
    region       = local.ctx.region
    encrypt      = true
    use_lockfile = true
    s3_bucket_tags = {
      ManagedBy     = "Terraform"
      ProvisionedBy = "Terragrunt"
      Purpose       = "store terraform state files and lock"
    }
  }
}

generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "${local.ctx.region}"
}

terraform {
  required_version = "${local.versions.locals.terraform}"
  required_providers {
    aws = {
      source  = "${local.versions.locals.providers.aws.source}"
      version = "${local.versions.locals.providers.aws.version}"
    }
    postgresql = {
      source  = "cyrilgdn/postgresql"
      version = "~> 1.22.0"
    }
    mysql = {
      source  = "petoju/mysql"
      version = "~> 3.0.0"
    }
  }
}

EOF
}
