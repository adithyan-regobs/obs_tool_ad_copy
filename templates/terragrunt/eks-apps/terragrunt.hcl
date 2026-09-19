terraform {
  source = "../../../../../../../layers/eks-workload"
}

include "root" {
  path = find_in_parent_folders()
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

dependency "argocd" {
  config_path = "../../../../argocd-applications"
  mock_outputs = {
    project_names_by_kind = {
      service = "argocd-project-core-${include.env.locals.env}-applications-service"
      system  = "argocd-project-core-${include.env.locals.env}-applications-system"
    }
  }
}

dependency "eks_cluster" {
  config_path = "{{EKS_DEPENDENCY_PATH}}"
  mock_outputs = {
    cluster_name = "mock-eks-cluster"
  }
}

dependencies {
  paths = ["../../../../argocd-applications", "{{EKS_DEPENDENCY_PATH}}"]
}

inputs = {
  organization      = include.env.locals.organization
  env               = include.env.locals.env
  region            = include.env.locals.region
  region_code       = include.env.locals.region_code
  index             = include.env.locals.index
  identifier        = basename(get_terragrunt_dir())
  eks_cluster_name  = dependency.eks_cluster.outputs.cluster_name
  tags              = include.env.locals.tags
  manifest_repo_url = include.env.locals.k8s_manifests_repo_url

  create_ecr     = {{CREATE_ECR}}
  create_secrets = {{CREATE_SECRETS}}
  create_ssm     = {{CREATE_SSM}}
  create_argo    = {{CREATE_ARGO}}
  auth_mode      = "{{AUTH_MODE}}"

  custom_policy_json = {{CUSTOM_POLICY_JSON}}

  argo = {
    project_names = dependency.argocd.outputs.project_names_by_kind
  }
}
