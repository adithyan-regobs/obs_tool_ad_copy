locals {

  context = {
    organization = "${organization}"
    env          = "${env}"
    cloud        = "aws"
    account_id   = "${account_id}"
    region       = "${region}"
    region_code  = "${region_code}"
    country_code = "${country_code}"
    index        = "${index}"
    tags_all     = local.tags_all
  }
  tags_all = {
    Terraform    = "true"
    Environment  = "${env}"
    Organization = "${organization}"
    RegionCode   = "${region_code}"
    Index        = "${index}"
    Cloud        = "aws"
  }
  retention_period = 365

}
