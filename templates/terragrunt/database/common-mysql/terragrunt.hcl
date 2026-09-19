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
      mysql_db_private_subnets = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
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
  subnet_ids              = dependency.base.outputs.vpc_subnet_groups.mysql_db_private_subnets
  create_db_subnet_group  = true
  port                    = 3306

  allowed_cidr_blocks = [
      dependency.base.outputs.vpc_cidr_block,
      include.env.locals.vpn_cidr,
      include.env.locals.atlantis_vpc_cidr
    ]  

  create_alarms = true
  alarms_sns_topic_arn = dependency.slack_ops.outputs.sns_topic_arn
  # DB configuration
  identifier              = basename(get_terragrunt_dir())
  engine                  = "aurora-mysql"
  engine_version          = "8.0"
  instance_class          = "db.r5.large"
  instances               = { one = { instance_class = "db.r5.large" } , two = { instance_class = "db.r5.large" }}
  auto_minor_version_upgrade = true
  
  # Credentials - using Secrets Manager
  master_username             = "commmon_mysql_admin1wQj"
  manage_master_user_password = true 

  # Observability
  enabled_cloudwatch_logs_exports     = ["error"]
  enable_monitoring                   = true
  monitoring_interval                 = 60
  backup_retention_period             = 31
  enable_rds_proxy                    = false
  
  # Parameter group
  create_db_cluster_parameter_group     = true
  db_cluster_parameter_group_family     = "aurora-mysql8.0"    
  db_cluster_parameter_group_parameters = [
                                            {
                                              name         = "binlog_format"
                                              value        = "ROW"
                                              apply_method = "pending-reboot"
                                            },
                                            {
                                              name         = "binlog_row_metadata"
                                              value        = "FULL" 
                                              apply_method = "pending-reboot"
                                            },
                                            {
                                              name         = "binlog_row_image"
                                              value        = "FULL" 
                                              apply_method = "pending-reboot"
                                            }
                                          ]
  
  # Backup and maintenance settings
  preferred_backup_window   = "00:24-00:54"
  preferred_maintenance_window = "sun:03:00-sun:03:30"

  //mysql databases
  mysql_databases = [
    "ybl_fulfillment",
    "appserver_noref",
    "lulufulfillment",
    "goblin_service",
    "user_service_v2",
    "accounts",
    "rewards_service",  
    "bbps_service",
    "settlements_db",
    "notification_db",
    "ponzim_db",
    "backoffice_db",
    "pulse_db"
  ]  

  //mysql users and permissions
  mysql_users = [
    {
      name     = "fx_service"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQE8b7POQj/VM3hxTjn5oMKEAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFRqfTCx9tbc++NcyAgEQgDBoFDOpt3x9B32UDMdCVj8Ry8hfVjJK5K3RhGbNEDxKMTEneUqp3TyxELR/G8c5YxE="
      host     = "%"
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "beneficiary_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGylM1ad1191jBOaEeC8zj7AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMK2lRWAI7iu+2JwFpAgEQgDD6YfUNddEKsLbeiv3x4fRdZBeUGhux1xm17RZELaB4cxJQjnaY0UeXHEgTuU1yRgw="
      grants = [
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "verification_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFI0cxUVJHSvHz6zPmiIMCdAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMGruaNFpLHnyM8e/OAgEQgDAEnqQPbO9SX9IZ4PaYG5KC85GUf5tT/RgkTsVQsx3zXm5Q6DH+xBk2K8kvIHeScws="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "workflow_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHuRSIx0kK54GpCnAOvgbYhAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMT2awR73WTyuj/hV5AgEQgDCinHwZu0g9/ZNVc9LBoojUOUGNyT6iR2d/rNxB2SzCFcbou8A9Y7omFI7R6Otl+cg="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "appserver_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQG9RcUm5GuKcKJs2scs7u26AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMZ1+vonhcOlaNPR4oAgEQgDDQy1eHTJFyr8yEBW20h78Gq002T/hnhFK1Cgz84QXVi3wYjr666X4Wv9aLIBAyF0Y="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "cron_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHSyhb192EPJnyaZiP8QHMZAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFb92pyM0X/Ik7OlnAgEQgDB0EMS8W2e/XjgUEM/FYYPQpE8Re/0E/7Ref36nEFrU3SWSsE1LPQuseJbtWmLQ3Vo="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "rewards_api_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGX/5HWpxOZ6JDQGiqZ4QZRAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMbNWbr+CYIrkpMpbnAgEQgDA941TrXAwTL4itNAqgvbn7nnkcP4K+9nJthj38mcLwONBY5X/hmMvp2CtdtBvlQqs="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }        
      ]
    },
    {
      name     = "rewards_worker_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQE29ObDZudPwlz8N6C5jYB3AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMjEHan8Y1IhF/KOlxAgEQgDAzW9BFu9nl4fQXuHONG2YQ94/uCxsONLtcgjoa46EM1g2QKc4DymvECAc1cmDoY+I="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }     
      ]
    },
    {
      name     = "lulu_fulfilment_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFW8aloLAaiINpq0k0BoQ0SAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM+hZUUPYHAk8sybP+AgEQgDCg+TLxq0sWp7KxerYD5A5e4JlqZfMTO7uGJERaQUrynOOGgi2F6buMNUunGFcnmLU="
      grants = [
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }        
      ]
    },
    {
      name     = "ybl_fulfilment_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQF8I8CuaXN6W3BN59HH1vB6AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFlc8+WGd7hzfFvd4AgEQgDCUQnuE1WA0SGQt/0sBcFXAfTYvGuzxHRRyJ+6LRPcAq+LlGJuUGqeuBT8Pn57wQ28="
      grants = [
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "user_vault_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHy2jL9cOsqzSi1shV7bFMlAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMSFP0+PdjXYxaKSdnAgEQgDAdPdvsxX4JWqibCxdkWj1aXjoHapCgRfBk3IHCHSM7QVyQKouiNSY59JJdssO6IJM="
      grants = [
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "goblin_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQF8iyUdAWygAQb8P+axmn/4AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM7TsfjZQAs6IH97QKAgEQgDCIMnmrbey2iWsUJzFQKkRD8oYand+dXUr/GoAYydpUzMQJgbbLAtEC5aAHbybAWvE="
      grants = [
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "notification_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH8hhgEvi4Gg2zDDSMIX2UfAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMF562HoFYIjLwilZRAgEQgDDMMAH8VvW/Xt8pIAax4SrGQuaTyhU9ym/lglcWuLQOdbg29oBm0EjydNxuzVmN848="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "alphadesk_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEZ+fKi3BJI7xCHiw0GLJurAAAAcDBuBgkqhkiG9w0BBwagYTBfAgEAMFoGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMuSRm+kVr9Ll5s8b5AgEQgC1+eLGOGIU5Z83ja0WATTDOqMUb3MNizPYuogbLFWiUwFXIcV+G9ZyQ7zC9qaM="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "liquibase_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFD0Pz+HhOXnT/Xf+aiQYhHAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMQB9anVAO+XcgQJdeAgEQgC8XD10+vFppfVew5SgnsS/v4EeayWvtVNjvc9w7hoVb0KioLG3nsLIvuvnqdKBjNQ=="
      grants = [
        {
          database   = "lulufulfillment"
          table      = "liquibase_test"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "INDEX", "ALTER"]
        }
      ]
    },
    {
      name     = "ashutosh_gupta_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH+xuLWhL10CurESOf1IrtLAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0ZB002OQa15Fm8RXAgEQgDA8ae+LMCHzCVvkMMLwHwykqm8tfZ2tiB5/DODA9ZNeoDbE3ADW+GNDg8oYJjo5+q0="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "bbps_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFkGz+vL6D59KyZIWgFKV24AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMTfl4Vowi73+gZ7QLAgEQgDB06TyTR1wbDykk3Pfn+GA0rEJFucza/HIpIGw2mriLS/ITHoo2AHunixhL+7sr/P4="
      grants = [
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE"]
        }
      ]
    },
    {
      name     = "settlements_api_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEBSe4xOhtSCZfTHs8+mJToAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMBBf7cZM3pPKA4mFMAgEQgC9l489ir0RkUytwuNWDyZj65iTizhtcLyxyv6rF6u5tvITSCRryxONbpacrjY7biw=="
      grants = [
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE"]
        }
      ]
    },
    {
      name     = "settlements_worker_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGLN4M5RiLV4ptrhUqLYntMAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMt3j4WNeKQPDPlBRDAgEQgDB0NsapOpJtAO98TTCEFeoxrUoXEbfglE78p6rvZ4Tnz0VQoNNSUyBmYewB8Hbbc/4="
      grants = [
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE"]
        }
      ]
    },
    {
      name     = "ponzim_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH56tKrjzdFLFek5XGxw4M7AAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMmLkVRS3ofJBcut8lAgEQgC9M0AO7yZ0sRUZ6YjKnWs00ajVFMMLOHev5txZgeVppqu/KNw79H/UEUbEvHmJQpg=="
      grants = [
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    }, 
    {
      name     = "raj_vishwakarma_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGdsONPQJYF69w8QpJXWshUAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMW6AJwhqBLihQLdBKAgEQgDD76bNLMhboA6ULpG/thRu/lXSoCuHWVS3y5uBRB8qhnkjTxbaQ0LhTsPOQGqR2L1U="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "ayush_singh_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGQAdgZ7bBDsDGQTWnJybNJAAAAdjB0BgkqhkiG9w0BBwagZzBlAgEAMGAGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMp5IUhdy3/9y784PyAgEQgDPx0LUV21B9Z2v50nPe5u5KVuMQxZmSzreioNnAHS70vQYYIxvCT7RHLGNwdSWKOy4NdqI="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "ALTER"]
        }
      ]
    },
    {
      name     = "adnan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEDd1fMUOudoAEydGVE7zRXAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0yNLsmzHdbOTqRKVAgEQgC+AaibFl04SZoPZxoCjS3irll3o5nJcWAX/7j/WVKnOertwZojCTCXMj2YUM0WVDA=="
      grants = [
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        }
      ]
    },
    {
      name     = "vaibhav_sidana_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGojdDtlkWrP4YgX4EJWSORAAAAbjBsBgkqhkiG9w0BBwagXzBdAgEAMFgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMNEQ2ZWiT4fRGARZMAgEQgCsWwdqqR6Iyz1L17UN7NrT6Hmm+h+UIW9U4RTXcdOAGL+vWEyvpGdEV6IFe"
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE", "CREATE", "ALTER", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"

          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
          
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "ALTER", "UPDATE", "DELETE"]

        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "uday_jartarghar_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGBwKYxk5rYCxfVluu85IxgAAAAfjB8BgkqhkiG9w0BBwagbzBtAgEAMGgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMjH89Ww+dAX3dxp/tAgEQgDsMf5KzuVtGcae1DU9mMPjDCSTwxu/8bQCIsKzy9tbYJN5Tqvn9550X83Uq0J/eoYRlBFzte1hH2Gpksw=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "deep_diwakar_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFIxEitpSEp+qS7xieQCnegAAAAfjB8BgkqhkiG9w0BBwagbzBtAgEAMGgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMxa7L7q9Faw4JHDPWAgEQgDty/qJ2ffg719YlBN8Oxyoh+XHFHjSj1g0QqRfAOjbcLH2AZvQgS65tx8vMxG43q6DIYkOSDavGOAzd/A=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "prathmesh_bhalekar_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQG5iV37DEdfAKGceItspQwYAAAAhjCBgwYJKoZIhvcNAQcGoHYwdAIBADBvBgkqhkiG9w0BBwEwHgYJYIZIAWUDBAEuMBEEDL8vKO5sYv5UxG8PXQIBEIBCbuViBKMWNdA68KikeLJ0+sOkrGkqg5HHPtABAmJJAymbryOXISy9T7yEGKkCimVcwRXaC60Sj2FjVSRvDuxPJXoT"
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        }
      ]
    },
    {
      name     = "rishi_dubey_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQHK1DBvEsLAHZSvuos49eB8AAAAiDCBhQYJKoZIhvcNAQcGoHgwdgIBADBxBgkqhkiG9w0BBwEwHgYJYIZIAWUDBAEuMBEEDIvK9gMaMgtERYwkjwIBEIBEywNJOtUt9UjoPCzscfTV88lwjCM73Gx9bYHg1VsVLN58Sz42PpyDwULajM6zlZ6Hwk6yRVLE60ro3OgO3KdLsebCiOI="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "lalit_nankani_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGdp6ZtLOHhN0D8DyyL1xScAAAAezB5BgkqhkiG9w0BBwagbDBqAgEAMGUGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMXgbtbpfahnsS+eJDAgEQgDiB1uj1lol3iPon1NB2KomQkNIT1+egcCoDI/qGC5ZnmD0t88HdxYZlhbShtYYJbTQ6cYh8UAG0Ow=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        }
      ]
    },
    {
      name     = "praveen_nadumani_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEaVqwcKfRKfJ4JfotrtMhxAAAAejB4BgkqhkiG9w0BBwagazBpAgEAMGQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM7JYonMq7fVBOhVPYAgEQgDfqCdZ72CFIWr/6a5ksY8GgMc7NHdsnHJsC1jR9rSDElpFSgtpfVrUc20s7usMPsB2ULvVI+KTA"
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        },
        {
          database   = "pulse_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE", "DROP"]
        }
      ]
    },
    {
      name     = "saksham_tiwari_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFG/AQbSwDeQTxf7Ua9Rcg5AAAAiDCBhQYJKoZIhvcNAQcGoHgwdgIBADBxBgkqhkiG9w0BBwEwHgYJYIZIAWUDBAEuMBEEDKRC7qlLEoDW8jodxQIBEIBENeVC6/an9GpAhRcq88pXXowYXV0xqZ0Xwh+h8tmJhGVMzkA4s6SjHpMa0xs2x4VYWMvhHFIKxItN4PYSaz4WExUJ0GE="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "aviral_asthana_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEaVqwcKfRKfJ4JfotrtMhxAAAAejB4BgkqhkiG9w0BBwagazBpAgEAMGQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM7JYonMq7fVBOhVPYAgEQgDfqCdZ72CFIWr/6a5ksY8GgMc7NHdsnHJsC1jR9rSDElpFSgtpfVrUc20s7usMPsB2ULvVI+KTA"
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "UPDATE", "CREATE"]
        }
      ]
    },
    {
      name     = "saurabh_dohaiya_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGw9fO1AziOfjzMRsm6ieWgAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMnXDM7v59eigeg5k/AgEQgC+Hpj9gSQvTKp1d9kUjvCe9kjARtbavoVQE6faTnWUCQ5IRdKq73N2FnY5y31UZRQ=="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }        
      ]
    },
    {
      name     = "debezium_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQF4KzJiOVmsFxUtDXKxG1mlAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMuJxwXZbn8uHUlxYNAgEQgC/XelLOScX1qJOQYDS7lYTOviP9I+7tJs6LIdXp3yMKhpqu+9Ie9e6MMFOnAYt7FA=="
      grants = [
        # Global privileges
        {
          database   = "*"
          table      = "*"
          privileges = ["REPLICATION SLAVE", "REPLICATION CLIENT", "LOCK TABLES", "RELOAD"]
        },
        # Database-specific privileges
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "SHOW VIEW"]
        }
      ]    
    },
    {
      name     = "rishabh_tripathi"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFbBg1GHJT6UiGz4WOMZkBIAAAAajBoBgkqhkiG9w0BBwagWzBZAgEAMFQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMuvyIwkfJRJ9x5miRAgEQgCfiZ0oPcY/eo78vycYAG1kuSWPa4SAmrhPC8b9On6MtQYntFmhMhJI="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE"]
        }
      ]
    },
    {
      name     = "kamila_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGHLe0MDp/Bqe9bZ6GWWLyMAAAAdjB0BgkqhkiG9w0BBwagZzBlAgEAMGAGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0S6A7aFYM2O+o7fyAgEQgDNEuw9xBUNeo/70ffI9TSosQ2ave+E2pPCIPG+x6accy2rKKn0GTEXlBMMWmsCuZGzX1oY="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "jil_patel_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGlWGlwIoF6EKJqrRkdsCEIAAAAbjBsBgkqhkiG9w0BBwagXzBdAgEAMFgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMQ3rSwB8hDuRmEnsdAgEQgCsdpfM4ZPWmc6A3U9WL4GnQljOEg2k4Zpx1GxwqKvvjKynGPtYvTUhm2RH4"
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX"]
        }
      ]
    },
  {
      name     = "suyash_karkare_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFe1vRC3hIFl2EOPYIvjXvWAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMfZ3UOz3ixwr8QxntAgEQgC90vJl0/ok50vGScPUjH9x0FX9wzAjHhMqU7IGSRDJFFIIkCdYfNPt2194kZ5peKg=="
      grants = [
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "anubhav_jindal_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH1l6B9MqLMp0ipXHA0+2rUAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM0NkPHNuiddjxPfSAAgEQgC8BEKA6GO6lvIiGE+MbLLH7OYqP7aFJLwMp7tnVQSK8N5XcwXZbS8SzIGns+A/nMg=="
      grants = [
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "urvil_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGs5skf5G0IMLSccMkVwqqUAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM9mc/hJOf6L5D3zm7AgEQgC+Zw8Jy7PLgMyvwM0++D8aPlSxTCOlREHCD6bDENeR/wTIO87A0m18zIHga4d2b4Q=="
      grants = [
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "alex_guo_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFhnJU40Um52LSYm54p8BeWAAAAbjBsBgkqhkiG9w0BBwagXzBdAgEAMFgGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMWR6UlZeS/oxMuEBEAgEQgCt4JumOzeDyaZRiuT3GiZm3PRJlqjsVvzr8u7n4T3LoOONMjMQIB/AxqJ8r"
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "CREATE", "ALTER", "INDEX", "DELETE"]
        }
      ]
    },
    {
      name     = "varun_nayal_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEu2ssSRgSGbkv0IS0mafuJAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMEXbgFQJjKif1UsncAgEQgC8PyapBKHPqQVc42WuIZirCGcXFghkGaoGv8RsqOZ0Rw7qmIOB4wPJQKqatY6EXbA=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        }
      ]
    },
    {
      name     = "venkatesan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFssmdsMwM6PsfqgFmetvm2AAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMUq7bohztnVEM91aeAgEQgC9jWGhPQf+H+wHk9hzkCvB9O/LkbXGHzSwmaon/sVcBLCj9Q9bVIugYVkg0GtcK6A=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "sandeep_srinivasan_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQH3U3S8iEevTH0Uc7xbJfdYAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMOtjPZCCNPEoK9VA7AgEQgC8GWFrUGl8fnUXkRlRqbAJxavfRcxrW1wlahFknDwxNSiPkcYIZJMYvOAqYMQr46g=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "siddharth_singh_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQG6iGVaPbuAUMM61jWfYAGRAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMNI0RMiBfXHjV8N3xAgEQgC9lToiS0w8vVn/0Q7UEJ8PTrQQqEHo8KZOMifqg/ENOVjBXezc1Hb3ZChbDEElz2g=="
      grants = [
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT","INSERT","UPDATE","CREATE","ALTER","INDEX"]
        },
      ]  
    },
    {
      name     = "backoffice_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFwxfUP2KKvEWvxZvvFGzxfAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMF1FSmGnaxI/1kRToAgEQgC9Zr32Fh25vCnjazZ9MTSXCo3yPKxfVuDrtiQZ3p82p0Nr5xqJM18NaFh1TjVEEnw=="
      grants = [
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "pulse_service"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQF+XjDepphryoDpawhgvtAWAAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM4mcW8xxrCuqXKx3nAgEQgDAaKAieZU+O2HmgCr71xsC+IVkXdpj4tLFKfthPGjHA9vUmk4th6Mti49DKHopYbBs="
      grants = [
        {
          database   = "pulse_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "abhishek_pr_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQGX6tldEzQWbFyzq5Gq5DpvAAAAejB4BgkqhkiG9w0BBwagazBpAgEAMGQGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMFefOf8nG2Hd8KlwsAgEQgDeEuKtxxEPteTh6LqtijG9MkpJ8YTWa0dGbi7gR7WBbmWN96sj9qVZ2imZWZTGU5xpfqA1tkYfJ"
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "arun_bajpai_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFyNnxsVMUCwtjkDmKhUwuUAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM33U/crJd1Yvuin21AgEQgC8t8z7X5wb+7J45+fa/cmB+5iJ1GkqyigynH1J7K365pkoQxOngOjsUHo41dVuIVA=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "rahul_srivastava_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQE9w2Ur+KDep7ydvkMr+CJbAAAAeDB2BgkqhkiG9w0BBwagaTBnAgEAMGIGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMNjpAD6R5Gd12T2w1AgEQgDXfulLs/Hpq6Y2eErft4bZOJnNIBw9sJIf4/q3Cvnj80nCXY9aZgU7Lnc3UDvQRfLopk0IdrQ=="
      grants = [
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "ALTER"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "ALTER", "INSERT", "UPDATE"]
        }
      ]
    },
    {
      name     = "sudan_s_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQFm39Bt6j6FlecJXmJvHO8nAAAAdTBzBgkqhkiG9w0BBwagZjBkAgEAMF8GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM2MJcu7HOyEAFFd3iAgEQgDJbcIyhhO202k19D5Yx/wOExFd9S44+GRqHxDOvDrzgvpsDWvs+prNRKOPVedHNgYMPpQ=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE"]
        }
      ]
    },
    {
      name     = "molt_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQE29ObDZudPwlz8N6C5jYB3AAAAczBxBgkqhkiG9w0BBwagZDBiAgEAMF0GCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQMjEHan8Y1IhF/KOlxAgEQgDAzW9BFu9nl4fQXuHONG2YQ94/uCxsONLtcgjoa46EM1g2QKc4DymvECAc1cmDoY+I="
      grants = [
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT"]
        }
      ]
    },
    {
      name     = "prithvi_user"
      host     = "%"
      password = "AQICAHiI9DJjx36hw9bjPu5j+BmOFkBZyx7amkPsjHjVsSMhqQEiKAErwa/1qVG58VXpecVIAAAAcjBwBgkqhkiG9w0BBwagYzBhAgEAMFwGCSqGSIb3DQEHATAeBglghkgBZQMEAS4wEQQM4VtR4Ru6N2SpMrQaAgEQgC/WXkkWLBkNT+bSAKPnWe4BIXB3bt86sHIS2TDIBxLvDnkTzU4LkqWhKj2mAkj0gA=="
      grants = [
        {
          database   = "ybl_fulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "appserver_noref"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "lulufulfillment"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "goblin_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "user_service_v2"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "accounts"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "rewards_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "bbps_service"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "settlements_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "notification_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "ponzim_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "backoffice_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        },
        {
          database   = "pulse_db"
          table      = "*"
          privileges = ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "INDEX", "REFERENCES", "CREATE TEMPORARY TABLES", "EXECUTE", "CREATE VIEW", "SHOW VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "EVENT", "TRIGGER"]
        }
      ]
    }
  ]
}
