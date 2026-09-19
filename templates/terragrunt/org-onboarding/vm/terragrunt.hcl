terraform {
  source = "${get_repo_root()}/layers/aws/v1/vm"
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
    vpc_cidr_block = "10.0.0.0/16"
    subnet_groups = {
      private = {
        app = ["subnet-00000000000000000"]
      }
      public = {}
    }
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  context   = include.env.locals.context
  name      = basename(get_terragrunt_dir())
  vpc_id    = dependency.base.outputs.vpc_id
  subnet_id = dependency.base.outputs.subnet_groups.private.app[0]

  instance_type = "t3a.medium"

  ami_id                 = null
  ami_ssm_parameter_name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"

  create_security_group = true
  ingress_cidr_rules = [
    {
      description = "SSH from within VPC"
      protocol    = "tcp"
      from_port   = 22
      to_port     = 22
      cidr_ipv4   = dependency.base.outputs.vpc_cidr_block
    }
  ]
  ingress_source_security_group_rules = []

  root_volume_size       = 20
  root_volume_type       = "gp3"
  root_volume_iops       = 3000
  root_volume_throughput = 125

  create_iam_instance_profile = true
  iam_managed_policy_arns = [
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
  ]
  iam_custom_policy_arns = []
  iam_role_policies      = {}

  user_data = null
}
