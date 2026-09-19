terraform {
  source = "${get_repo_root()}/layers/aws/v1/base"
}

prevent_destroy = true

include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

inputs = {
  context          = include.env.locals.context
  retention_period = include.env.locals.retention_period
  name             = basename(get_terragrunt_dir())

  number_of_azs    = 2
  vpc_size_profile = "startup"

  subnet_groups = []

  create_deploy_role = true

  allowed_github_orgs = ${allowed_github_orgs}
}
