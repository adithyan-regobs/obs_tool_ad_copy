terraform {
  source = "${get_repo_root()}/layers/aws/v1/bucket"
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
  organization           = include.env.locals.context.organization
  env                    = include.env.locals.context.env
  region                 = include.env.locals.context.region
  region_code            = include.env.locals.context.region_code
  country_code           = include.env.locals.context.country_code
  index                  = include.env.locals.context.index
  identifier             = basename(get_terragrunt_dir())
  tags                   = include.env.locals.context.tags_all
  versioning             = false
  access_logging_enabled = false
}
