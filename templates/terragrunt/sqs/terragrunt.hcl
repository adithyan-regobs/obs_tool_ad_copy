terraform {
  source = "../../../../../layers//queue"
}
include "root" {
  path = find_in_parent_folders()
}
include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}
dependency "slack_ops" {
  config_path = "./../../slack-ops"
  mock_outputs = {
    sns_topic_arn = "arn:aws:sns:ap-south-1:111111122222:mock-alarms-topic"
  }
}
dependencies {
  paths = ["./../../slack-ops"]
}
inputs = {
  organization         = include.env.locals.organization
  env                  = include.env.locals.env
  country_code         = include.env.locals.country_code
  region_code          = include.env.locals.region_code
  region               = include.env.locals.region
  index                = include.env.locals.index
  tags                 = include.env.locals.tags
  create_dlq           = true
  fifo_queue           = true
  identifier           = basename(get_terragrunt_dir())
  # Alarms
  create_alarm         = false
  alarms_sns_topic_arn = dependency.slack_ops.outputs.sns_topic_arn
}
