terraform {
  source = "${get_repo_root()}/layers/aws/v1/dynamodb"
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
  context = include.env.locals.context
  name    = basename(get_terragrunt_dir())

  # Partition key — required. Add range_key for a composite primary key.
  hash_key      = "pk"
  hash_key_type = "S"
  range_key     = null

  # On-demand billing — no capacity planning needed.
  billing_mode = "PAY_PER_REQUEST"

  # TTL — disabled by default. Set ttl_attribute_name when enabling.
  ttl_enabled        = false
  ttl_attribute_name = "ttl"
}
