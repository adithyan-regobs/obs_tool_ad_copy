terraform {
  source = "${get_repo_root()}/layers/aws/v1/rds_aurora"

  before_hook "before_destroy" {
    commands = ["destroy"]
    execute  = ["echo", "Ensuring Aurora DB resources are properly backed up before destruction"]
  }
}

include "root" {
  path = find_in_parent_folders("root.hcl")
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

# dependency "base" {
#   config_path = "../../base/main"

#   mock_outputs = {
#     vpc_id         = "vpc-00000000000000000"
#     vpc_cidr_block = "10.0.0.0/16"
#     subnet_groups = {
#       private = {
#         app = ["subnet-00000000000000001", "subnet-00000000000000002"]
#       }
#     }
#   }
#   mock_outputs_allowed_terraform_commands = ["validate", "plan"]
# }

inputs = {
  context = include.env.locals.context
  name    = basename(get_terragrunt_dir())

  # Network
  vpc_id                 = ""
  subnet_ids             = []
  create_db_subnet_group = true
  allowed_cidr_blocks    = []

  # Engine
  engine         = "aurora-postgresql"
  engine_version = "16.4"

  # Database
  database_name  = null
  psql_databases = []

  # Credentials
  master_username                     = "aurora_admin"
  manage_master_user_password         = true
  iam_database_authentication_enabled = true

  # Serverless v2
  instance_count = 1
  min_capacity   = 0.5
  max_capacity   = 2

  # CloudWatch logs
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # Operational
  deletion_protection = false

  # Cluster parameter group
  create_db_cluster_parameter_group     = true
  db_cluster_parameter_group_family     = "aurora-postgresql16"
  db_cluster_parameter_group_parameters = [
    {
      name         = "rds.logical_replication"
      value        = "1"
      apply_method = "pending-reboot"
    }
  ]
}
