terraform {
  source = "${get_repo_root()}/layers/aws/v1/eks"
}

include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

dependency "base" {
  config_path = "../../base/main"

  mock_outputs = {
    vpc_id         = "vpc-00000000000000000"
    vpc_cidr_block = "10.0.0.0/20"
    subnet_groups = {
      private = {
        app = ["subnet-00000000000000000", "subnet-00000000000000001"]
      }
      public = {
        edge = ["subnet-00000000000000002", "subnet-00000000000000003"]
      }
    }
    deploy_role_name = "mock-deploy-role"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  context          = include.env.locals.context
  retention_period = include.env.locals.retention_period
  name             = basename(get_terragrunt_dir())
  vpc_id           = dependency.base.outputs.vpc_id
  vpc_cidr         = dependency.base.outputs.vpc_cidr_block
  subnet_ids       = dependency.base.outputs.subnet_groups.private.app

  endpoint_public_access = true

  cluster_api_ingress_cidrs = [
    dependency.base.outputs.vpc_cidr_block,
    "0.0.0.0/0"
  ]

  access_configs = {
    deploy = {
      principal_type    = "role"
      principal_name    = dependency.base.outputs.deploy_role_name
      policy_arns       = ["AmazonEKSClusterAdminPolicy"]
      access_scope_type = "cluster"
    }
  }
}
