terraform {
  source = "${get_repo_root()}/layers/aws/v1/application"
}

include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

inputs = {
  context                 = include.env.locals.context
  identifier              = "default"
  namespace               = "${tenant_namespace}"
  service_account         = "tenant-default"
  service_account_pattern = "*"
  eks_cluster_name        = "${eks_cluster_name}"
  create_irsa_role        = true
  custom_policy_json      = ""
  managed_policy_arns     = []
}
