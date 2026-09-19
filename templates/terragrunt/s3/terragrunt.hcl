terraform {
  source = "../../../../../layers/bucket"
}
include "root" {
  path = find_in_parent_folders()
}
include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

dependency "regional_bootstrap" {
  config_path = "../../regional_bootstrap"
  mock_outputs = {
    s3_access_logs_bucket_id = "s3-id"
  }
}
inputs = {
  organization          = include.env.locals.organization
  env                   = include.env.locals.env
  country_code          = include.env.locals.country_code
  region_code           = include.env.locals.region_code
  region                = include.env.locals.region
  index                 = include.env.locals.index
  s3_access_logs_bucket_id = dependency.regional_bootstrap.outputs.s3_access_logs_bucket_id
  identifier            = basename(get_terragrunt_dir())
  tags                  = include.env.locals.tags
  versioning            = false
}
