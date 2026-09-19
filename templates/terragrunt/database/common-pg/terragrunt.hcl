terraform {
  source = "../../../../../layers/database"
}

include "root" {
  path = find_in_parent_folders()
}

include "env" {
  path           = find_in_parent_folders("env.hcl")
  expose         = true
  merge_strategy = "no_merge"
}

# Dependency on VPC module for network information
dependency "base" {
  config_path = "../../base"
  mock_outputs = {
    vpc_id = "mock-vpc-id"
    vpc_cidr_block = "10.0.0.0/16"
    vpc_subnet_groups = {
      postgres_db_private_subnets = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
    }
  }
}
dependency "regional_bootstrap" {
  config_path = "../../regional_bootstrap"
  mock_outputs = {
    acm_certificate_arns = {
      "*.genorim.xyz" = "arn:aws:acm:::certificate/<id>"
    }
         
  }
}

dependency "slack_ops" {
  config_path = "../../slack-ops"
}

dependencies {
  paths = ["../../base", "../../regional_bootstrap", "../../slack-ops"]
}

inputs = {
  organization         = include.env.locals.organization
  env                  = include.env.locals.env
  region_code          = include.env.locals.region_code
  region               = include.env.locals.region
  country_code         = include.env.locals.country_code
  index                = include.env.locals.index 
  deletion_protection  = include.env.locals.deletion_protection 
  tags                 = include.env.locals.tags
  retention_period     = include.env.locals.retention_period

  # Network configuration
  vpc_id                  = dependency.base.outputs.vpc_id
  subnet_ids              = dependency.base.outputs.vpc_subnet_groups.postgres_db_private_subnets
  create_db_subnet_group  = true
  port                    = 5432

  allowed_cidr_blocks = [
      dependency.base.outputs.vpc_cidr_block,
      include.env.locals.vpn_cidr,
      include.env.locals.atlantis_vpc_cidr
  ]  

  # DB configuration
  identifier              = basename(get_terragrunt_dir())
  engine                  = "aurora-postgresql"
  engine_version          = "15.12"
  instance_class          = "db.t4g.medium"
  auto_minor_version_upgrade = true
  
  # Credentials - using Secrets Manager
  master_username             = "commmon_pg_admin4e2Q"
  manage_master_user_password = true 

  # Observability
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  enable_monitoring                   = true
  monitoring_interval                 = 60
  
  # Parameter group
  create_db_cluster_parameter_group     = true
  db_cluster_parameter_group_family     = "aurora-postgresql15"    
  db_cluster_parameter_group_parameters = [
                                            {
                                              name  = "rds.logical_replication"
                                              value = "1"
                                              apply_method = "pending-reboot"
                                            }
                                          ]
  
  # Backup and maintenance settings
  preferred_backup_window       = "00:24-00:54"
  preferred_maintenance_window  = "sun:03:00-sun:03:30"
  backup_retention_period       = 31
  
  # Alarms Cloudwatch
  create_alarms = true
  alarms_sns_topic_arn = dependency.slack_ops.outputs.sns_topic_arn

  psql_databases = [
    "goms",
    "recon_service",
    "guardian_db",
    "rhythm_db",
    "harbor_db",
    "current_db"
  ]
  psql_users = [
    {
      name     = "goms_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFkL5Kz/0K//F0gbCcYVbemAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM5vZIudzu8/XumgwWAgEQgDDhLGn2HMt8E2W2ovZCj0bGx5yIvRhJSBYWUm9HZTIGNPV/yIa3ngbH52r/ehdFgtg="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "uday_jartarghar_user"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGanYOzwmF8UjbK1yYJfe3ZAAAAfTB7BgkqhkiG9w0BBwagbjBsAgEAMGcGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMHAOXxZxQhEXrs0t9AgEQgDpyR9lvGR3t8bWo7bKmhox6c61PjjhVvsOmkCAPbqUUKQli9EpV2s21xT+tTjmI4s9L85mCmZixI3nh"
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "recon_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEx3Wx/SGLuerpa3NGBnYsiAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMp3Je2WJJo5Z2PS05AgEQgC+qkdx7cL/uCTiA5g+58As6AtD/GcBNCQ5X59FlMHX6kO/kGcrznHFr9qg5nsIFSA=="
      grants   = [
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "ashutosh_gupta_user"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH+xuLWhL10CurESOf1IrtLAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0ZB002OQa15Fm8RXAgEQgDA8ae+LMCHzCVvkMMLwHwykqm8tfZ2tiB5/DODA9ZNeoDbE3ADW+GNDg8oYJjo5+q0="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "debezium_user"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHcCW3qSS98UvUEV8k1PQUTAAAAbjBsBgkqhkiG9w0BBwagXzBdAgEAMFgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM42f4f0vU+G3vISbnAgEQgCvZ9LLZaIUm9F+BUmtgzjXMP1uJo5Utk5mPgnI0zDhVbNBeSAdWfT0g2jwD"
      # Database level permissions
      roles = ["rds_replication"]
      # Schema level permissions
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        # Table level permissions (only SELECT is needed for CDC)
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        },
        # Sequence permissions (required for some CDC operations)
        {
          database    = "goms"
          schema      = "public"
          object_type = "sequence"
          privileges  = ["SELECT", "USAGE"]
        }]
    },
    {
      name     = "saurabh_dohaiya_user"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGw9fO1AziOfjzMRsm6ieWgAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMnXDM7v59eigeg5k/AgEQgC+Hpj9gSQvTKp1d9kUjvCe9kjARtbavoVQE6faTnWUCQ5IRdKq73N2FnY5y31UZRQ=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "venkatesan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFssmdsMwM6PsfqgFmetvm2AAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMUq7bohztnVEM91aeAgEQgC9jWGhPQf+H+wHk9hzkCvB9O/LkbXGHzSwmaon/sVcBLCj9Q9bVIugYVkg0GtcK6A=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        }
      ]
    },
    {
      name     = "sandeep_srinivasan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH3U3S8iEevTH0Uc7xbJfdYAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMOtjPZCCNPEoK9VA7AgEQgC8GWFrUGl8fnUXkRlRqbAJxavfRcxrW1wlahFknDwxNSiPkcYIZJMYvOAqYMQr46g=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        }
      ]
    },
    {
      name     = "varun_nayal_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEu2ssSRgSGbkv0IS0mafuJAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMEXbgFQJjKif1UsncAgEQgC8PyapBKHPqQVc42WuIZirCGcXFghkGaoGv8RsqOZ0Rw7qmIOB4wPJQKqatY6EXbA=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        }
      ]
    },
    {
      name     = "jil_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQESQAx0qQ1dxIc5xhbZusPnAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMf+siHcNEAjiHmlVnAgEQgC+0UNu1iN70YM8KkSJa1o3RijZ+nyIIjQsLnL9XxJ392Xl8A/uIZ8bPyoDTSiA1PQ=="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "adnan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEDd1fMUOudoAEydGVE7zRXAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0yNLsmzHdbOTqRKVAgEQgC+AaibFl04SZoPZxoCjS3irll3o5nJcWAX/7j/WVKnOertwZojCTCXMj2YUM0WVDA=="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "alex_guo_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFhnJU40Um52LSYm54p8BeWAAAAbjBsBgkqhkiG9w0BBwagXzBdAgEAMFgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMWR6UlZeS/oxMuEBEAgEQgCt4JumOzeDyaZRiuT3GiZm3PRJlqjsVvzr8u7n4T3LoOONMjMQIB/AxqJ8r"
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        }
      ]
    },
    {
      name     = "siddharth_singh_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQG6iGVaPbuAUMM61jWfYAGRAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMNI0RMiBfXHjV8N3xAgEQgC9lToiS0w8vVn/0Q7UEJ8PTrQQqEHo8KZOMifqg/ENOVjBXezc1Hb3ZChbDEElz2g=="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "lalit_nankani_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGdp6ZtLOHhN0D8DyyL1xScAAAAezB5BgkqhkiG9w0BBwagbDBqAgEAMGUGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMXgbtbpfahnsS+eJDAgEQgDiB1uj1lol3iPon1NB2KomQkNIT1+egcCoDI/qGC5ZnmD0t88HdxYZlhbShtYYJbTQ6cYh8UAG0Ow=="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "abhishek_pr_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGX6tldEzQWbFyzq5Gq5DpvAAAAejB4BgkqhkiG9w0BBwagazBpAgEAMGQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFefOf8nG2Hd8KlwsAgEQgDeEuKtxxEPteTh6LqtijG9MkpJ8YTWa0dGbi7gR7WBbmWN96sj9qVZ2imZWZTGU5xpfqA1tkYfJ"
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "aviral_asthana_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHfRU++DwAwC59NZjIO4Ok5AAAAajBoBgkqhkiG9w0BBwagWzBZAgEAMFQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMslifv2PC3fKS6I0jAgEQgCeNejTYEjbq+aERGobkqmLs2gZQL3m4EZqI3GLkfIwWxVGudNnvKbc="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT"]
        }
      ]
    },
    {
      name     = "raj_vishwakarma_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGdsONPQJYF69w8QpJXWshUAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMW6AJwhqBLihQLdBKAgEQgDD76bNLMhboA6ULpG/thRu/lXSoCuHWVS3y5uBRB8qhnkjTxbaQ0LhTsPOQGqR2L1U="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "arun_bajpai_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFyNnxsVMUCwtjkDmKhUwuUAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM33U/crJd1Yvuin21AgEQgC8t8z7X5wb+7J45+fa/cmB+5iJ1GkqyigynH1J7K365pkoQxOngOjsUHo41dVuIVA=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CONNECT"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "sudan_s_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFm39Bt6j6FlecJXmJvHO8nAAAAdTBzBgkqhkiG9w0BBwagZjBkAgEAMF8GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM2MJcu7HOyEAFFd3iAgEQgDJbcIyhhO202k19D5Yx/wOExFd9S44+GRqHxDOvDrzgvpsDWvs+prNRKOPVedHNgYMPpQ=="
      grants   = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "guardian_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFK4JIPje+fd1WYz7mWzwaPAAAAdjB0BgkqhkiG9w0BBwagZzBlAgEAMGAGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMj9l89kk98FYC7UUnAgEQgDP+vARNyHBvunyI9PCyXnDCLKt7kjdurgxTIMybUG8KCvGJvHkuI9pPZNZF1POXz3uSL+4="
      grants   = [
        {
          database    = "guardian_db"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "guardian_db"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "guardian_db"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "rhythm_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHKUFxuMCtTMj497ZJofE37AAAAfjB8BgkqhkiG9w0BBwagbzBtAgEAMGgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMDFV6eu2UW99P0TcFAgEQgDvTBjvGNU9Hsr69ov7JuA/on+ZhPfvf3ugt6169Ccg7sNMcuE/LMdhjZdhKEfje5gwnGx72L8IvNO4JWw=="
      grants   = [
        {
          database    = "rhythm_db"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "rhythm_db"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "rhythm_db"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "harbor_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEDSb1eD+zWRsij5kGM/qHoAAAAdzB1BgkqhkiG9w0BBwagaDBmAgEAMGEGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMnZjX/3kjcuVTD+RUAgEQgDR3p5K/34sXapkOJfM9umeRGqGSoBP/s2y06FP9qmzmwOgyNFuP1CMz1CerKVyS19vcjU/6"
      grants   = [
        {
          database    = "harbor_db"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "harbor_db"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "harbor_db"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "current_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEaCbI3KHkVm7rYU5m0zy5OAAAAeDB2BgkqhkiG9w0BBwagaTBnAgEAMGIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMELMToTAiWSLmYBbaAgEQgDVwUGlM4lcYvvLGr3oC9D4KV5PwTiGv9xJfQ6gtA34s1YYjDX0WWfC85BNh/y3YqurULeMUvw=="
      grants   = [
        {
          database    = "current_db"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "current_db"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "current_db"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "prithvi_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEiKAErwa/1qVG58VXpecVIAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM4VtR4Ru6N2SpMrQaAgEQgC/WXkkWLBkNT+bSAKPnWe4BIXB3bt86sHIS2TDIBxLvDnkTzU4LkqWhKj2mAkj0gA=="
      grants = [
        {
          database    = "goms"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "goms"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "database"
          privileges  = ["CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "schema"
          privileges  = ["USAGE","CREATE"]
        },
        {
          database    = "recon_service"
          schema      = "public"
          object_type = "table"
          privileges  = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    }
  ]
}
