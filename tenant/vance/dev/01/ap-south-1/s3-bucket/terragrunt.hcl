terraform {
  source = "../../../../layers/aws/s3_bucket"
}

include "root" {
  path                = find_in_parent_folders()
}

include "env" {
  path                =   find_in_parent_folders("env.hcl")
  expose              =   true
  merge_strategy      =   "no_merge"
}

inputs = {
  create_bucket        = true 
  organization         = include.env.locals.organization
  env                  = include.env.locals.env
  region               = include.env.locals.region
  index                = include.env.locals.index 
  identifier           = "project-storage"
  # Tags
  tags = {
    service     = "project-storage-bucket"
  }
}