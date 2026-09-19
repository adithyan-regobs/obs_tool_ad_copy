terraform {
  source = "../../../../../../layers/ecs"
}

include "root" {
  path = find_in_parent_folders()
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

dependency "base" {
  config_path = "../../base"
  mock_outputs = {
    vpc_id = "mock-vpc-id"
    vpc_cidr_block = "10.0.0.0/16"
    vpc_subnet_groups = {
      backend_cluster_private_subnets = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
      backend_api_private_subnets     = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
    } 
  }
}

dependency "common_infra" {
  config_path = "../common-infra"
  mock_outputs = {
    alb_arn = ""
    alb_security_group_id = "sg-89485495849835"
    cluster_arn = ""
    cluster_name = "mock-cluster"
    http_listener_arn = ""
  }
}

dependencies {
  paths = ["../../base", "../common-infra"]
}

inputs = {
  organization          = include.env.locals.organization
  env                   = include.env.locals.env
  country_code          = include.env.locals.country_code
  region_code           = include.env.locals.region_code
  region                = include.env.locals.region
  index                 = include.env.locals.index 
  deletion_protection   = include.env.locals.deletion_protection    
  tags                  = include.env.locals.tags
  identifier            = basename(get_terragrunt_dir())
  
  vpc_id               = dependency.base.outputs.vpc_id
  vpc_cidr_block       = dependency.base.outputs.vpc_cidr_block
  cluster_subnet_ids   = dependency.base.outputs.vpc_subnet_groups.backend_cluster_private_subnets
  alb_ingress_cidrs    = ["0.0.0.0/0"]  
  # kms_key_id           = dependency.regional_bootstrap.outputs.kms_key_id


  // Feature toggles - only infrastructure components
  create_cluster            = false
  create_autoscaling_group  = false
  create_alb                = false
  create_service            = true
  
  // Required for service - reference to existing cluster
  cluster_arn             = dependency.common_infra.outputs.cluster_arn
  cluster_name            = dependency.common_infra.outputs.cluster_name
  existing_listener_arn   = dependency.common_infra.outputs.http_listener_arn
  alb_sg_id               = dependency.common_infra.outputs.alb_security_group_id
  
  // Service configuration
  container_port          = 8080
  cpu                     = 1024
  memory                  = 2048
  service_path            = "/casa/*"
  listener_rule_priority  = 1805


  // Health check parameters
 health_check_path                  = "/casa-service/actuator/health"


  create_alarms                      = false

  // DATADOG
  encrypted_datadog_api_key                   = include.env.locals.encrypted_datadog_api_key
  encrypted_datadog_app_key                   = include.env.locals.encrypted_datadog_app_key

  // Data dog monitor list.
  monitors = [
    ${monitors_list}
  ]
}
