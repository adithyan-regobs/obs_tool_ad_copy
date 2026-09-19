terraform {
  source = "../../../../../layers//ecs"
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
    vpc_id         = "mock-vpc-id"
    vpc_cidr_block = "10.0.0.0/16"
    vpc_subnet_groups = {
      backend_cluster_private_subnets = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
      backend_api_private_subnets     = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
    }
  }
}

dependency "regional_bootstrap" {
  config_path = "../../regional_bootstrap"
  mock_outputs = {
    kms_key_id = "arn:kms:ap-south-1:111111122222:key/12345678-1234-1234-1234-1234567890ab"
    acm_certificate_arns = {
      internal = "arn:aws:acm:ap-south-1:111111122222:certificate/mock-cert"
    }
  }
}

dependency "common_infra" {
  config_path = "../common-infra"
  mock_outputs = {
    cluster_arn            = "arn:aws:ecs:ap-south-1:111111122222:cluster/mock-cluster"
    cluster_name           = "mock-cluster"
    alb_security_group_id  = "sg-mock"
    capacity_provider_name = "mock-capacity-provider"
  }
}

dependency "slack_ops" {
  config_path = "../../slack-ops"
  mock_outputs = {
    sns_topic_arn                 = "arn:aws:sns:ap-south-1:111111122222:mock-alarms-topic"
    devops_p0_alarm_sns_topic_arn = "arn:aws:sns:ap-south-1:111111122222:mock-devops-p0"
    devops_p1_alarm_sns_topic_arn = "arn:aws:sns:ap-south-1:111111122222:mock-devops-p1"
    devs_p0_alarm_sns_topic_arn   = "arn:aws:sns:ap-south-1:111111122222:mock-devs-p0"
    devs_p1_alarm_sns_topic_arn   = "arn:aws:sns:ap-south-1:111111122222:mock-devs-p1"
  }
}

dependency "datadog_api_key" {
  config_path = "../../secrets/datadog-configs"
  mock_outputs = {
    secret_manager_arn = "arn:aws:secretsmanager:ap-south-1:111111122222:secret:datadog_api_key-123456"
  }
}

dependencies {
  paths = ["../../base", "../../regional_bootstrap", "../common-infra", "../../slack-ops", "../../secrets/datadog-configs"]
}

inputs = {
  # General
  organization        = include.env.locals.organization
  env                 = include.env.locals.env
  country_code        = include.env.locals.country_code
  region_code         = include.env.locals.region_code
  region              = include.env.locals.region
  index               = include.env.locals.index
  deletion_protection = include.env.locals.deletion_protection
  tags                = include.env.locals.tags
  identifier          = basename(get_terragrunt_dir())

  # Network
  vpc_id              = dependency.base.outputs.vpc_id
  vpc_cidr_block      = dependency.base.outputs.vpc_cidr_block
  cluster_subnet_ids  = dependency.base.outputs.vpc_subnet_groups.backend_cluster_private_subnets
  api_subnet_ids      = dependency.base.outputs.vpc_subnet_groups.backend_api_private_subnets
  alb_ingress_cidrs   = [dependency.base.outputs.vpc_cidr_block, include.env.locals.vpn_cidr]
  kms_key_id          = dependency.regional_bootstrap.outputs.kms_key_id
  acm_certificate_arn = dependency.regional_bootstrap.outputs.acm_certificate_arns.internal

  # Feature Toggles
  create_cluster           = false
  create_autoscaling_group = false
  create_alb               = true
  create_service           = true
  alb_attachment           = true
  internal_alb             = true
  enable_ulimits           = true

  # ALB Config (New ALB) - Optional domain configuration
  alb_identifier = "service-name"
  // create_alb_domain = true
  // custom_alb_domain = "service-name-${include.env.locals.env}-${include.env.locals.index}-api.internal"

  # Cluster Reference
  cluster_arn            = dependency.common_infra.outputs.cluster_arn
  cluster_name           = dependency.common_infra.outputs.cluster_name
  capacity_provider_name = dependency.common_infra.outputs.capacity_provider_name
  alb_sg_id              = dependency.common_infra.outputs.alb_security_group_id

  # Container Config
  container_port = 5000
  cpu            = 2048
  memory         = 4096

  # ALB Routing
  service_path           = "/*"
  listener_rule_priority = 100

  # Container Configs (service specific - uncomment and update paths)
  // service_container_configs = jsondecode(file("../../envs/{service}/non-secure/{service}-configs.json"))
  // service_container_secrets = jsondecode(file("../../envs/{service}/secure/{service}-secrets.json"))

  # Health Check
  health_check_path                 = "/"

  # Autoscaling
  enable_autoscaling        = true
  desired_count             = 1
  min_task_count            = 1
  max_task_count            = 1
  http_scaling_enabled      = false
  http_scaling_target_value = 3000

  # Datadog Sidecar
  enable_datadog_sidecar = false
  datadog_secret_arn     = dependency.datadog_api_key.outputs.secret_manager_arn
  datadog_log_source     = "java"
  datadog_sidecar_cpu    = 256
  datadog_sidecar_memory = 512
  datadog_logs_enabled   = false

  # OTel Sidecar
  enable_otel_sidecar = false
  otel_sidecar_cpu    = 512
  otel_sidecar_memory = 1024

  # Alarms
  create_alarms                              = true
  alarms_sns_topic_arn                       = dependency.slack_ops.outputs.sns_topic_arn
  devops_p0_alarm_sns_topic_arn              = dependency.slack_ops.outputs.devops_p0_alarm_sns_topic_arn
  devops_p1_alarm_sns_topic_arn              = dependency.slack_ops.outputs.devops_p1_alarm_sns_topic_arn
  devs_p0_alarm_sns_topic_arn                = dependency.slack_ops.outputs.devs_p0_alarm_sns_topic_arn
  devs_p1_alarm_sns_topic_arn                = dependency.slack_ops.outputs.devs_p1_alarm_sns_topic_arn
}
