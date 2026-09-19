terraform {
  source = "${get_repo_root()}/layers/aws/v1/redis"
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
  organization = include.env.locals.context.organization
  env          = include.env.locals.context.env
  region_code  = include.env.locals.context.region_code
  index        = include.env.locals.context.index
  tags         = include.env.locals.context.tags_all

  # Network configuration
  vpc_id              = ""
  subnets             = []
  allowed_cidr_blocks = []

  # Redis configuration
  identifier             = basename(get_terragrunt_dir())
  port                   = 6379
  engine_version         = "7.1"
  node_type              = "cache.t4g.micro"
  num_cache_clusters     = 1
  create_parameter_group = false
  parameter_group_family = "redis7"
  maintenance_window     = "sun:03:00-sun:04:00"
  apply_immediately      = false
  create_alarms          = false
}
