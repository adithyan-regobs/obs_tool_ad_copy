terraform {
  source = "../../../../../layers/dynamo"
}
include "root" {
  path = find_in_parent_folders()
}
include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
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
  partition_key         = "event_name"
  attributes            = [
                            {
                              name = "event_name"
                              type = "S"
                            }
                          ]
}