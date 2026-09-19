terraform {
  source = "../../../../layers/gateway"
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
  config_path = "../base"
  mock_outputs = {
    vpc_id = "mock-vpc-id"
    vpc_cidr_block = "10.0.0.0/16"
    vpc_subnet_groups = {
      kong_cluster_private_subnets = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
      kong_db_private_subnets      = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
      kong_api_public_subnets      = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
      kong_admin_private_subnets   = ["mock-subnet-1", "mock-subnet-2", "mock-subnet-3"]
    } 
    s3_access_logs_bucket_id = "s3-id"      
  }
}

dependency "regional_bootstrap" {
  config_path = "../regional_bootstrap"
  mock_outputs = {
    s3_access_logs_bucket_id = "s3-id" 
    acm_certificate_arns = {
      "*.genorim.xyz" = "arn:aws:acm:::certificate/<id>"
    }     
  }
}

dependencies {
  paths = ["../base", "../regional_bootstrap"]
}

inputs = {
  organization         = include.env.locals.organization
  env                  = include.env.locals.env
  country_code         = include.env.locals.country_code
  region_code          = include.env.locals.region_code
  region               = include.env.locals.region
  index                = include.env.locals.index 
  deletion_protection  = include.env.locals.deletion_protection 
  retention_period     = include.env.locals.retention_period
  block_public         = include.env.locals.block_public
  allowed_ips          = include.env.locals.allowed_ips  
  tags                 = include.env.locals.tags    

  # Network configuration
  vpc_id                        = dependency.base.outputs.vpc_id
  kong_cluster_subnets          = dependency.base.outputs.vpc_subnet_groups.kong_cluster_private_subnets
  kong_public_api_subnets       = dependency.base.outputs.vpc_subnet_groups.kong_api_public_subnets
  kong_private_api_subnets      = dependency.base.outputs.vpc_subnet_groups.kong_admin_private_subnets
  kong_db_subnets               = dependency.base.outputs.vpc_subnet_groups.kong_db_private_subnets
  vpc_cidr_block                = dependency.base.outputs.vpc_cidr_block
  s3_access_logs_bucket_id      = dependency.regional_bootstrap.outputs.s3_access_logs_bucket_id
  admin_alb_ingress_cidrs       = [include.env.locals.vpn_cidr, include.env.locals.atlantis_vpc_cidr, dependency.base.outputs.vpc_cidr_block]
  # DB configuration
  db_instance_class             = "db.t4g.small"
  db_username                   = "kong_pg_admin_2025_4e2Q"
  preferred_backup_window       = "00:24-00:54"
  preferred_maintenance_window  = "sun:03:00-sun:03:30"
  
  // Cluster configs
  container_cpu                    = 512
  container_memory                 = 1024
  desired_task_count               = 2
  max_task_count                   = 4
  min_task_count                   = 2
  //IP lists
  whitelist_ipv4                   = [
                                      # # OPENVPN
                                      "65.0.8.155/32", 
                                      "3.109.132.64/32",
                                      # OLD VANCE PROD ACCOUNT NAT IPs, Remove after testing
                                      "18.134.235.0/32",
                                      "35.179.46.109/32",
                                      "13.41.255.185/32",
                                      "13.235.171.71/32",
                                      "65.0.254.72/32",
                                      # VANCE AND VANCE_BOLD_PITCH
                                      "65.0.8.155/32",
                                      "3.109.132.64/32",
                                      # DIGIT9
                                      "213.42.166.195/32",
                                      "86.96.250.195/32",
                                      "86.96.250.210/32",
                                      # GCC_REMIT and GCC_VALIDATION_REMIT (same IPs)
                                      "80.227.221.90/32",
                                      "151.253.75.59/32",
                                      "146.177.63.88/32",
                                      # SACHIN_ARMX
                                      "52.149.149.92/32",
                                      "115.99.201.32/32",
                                      "115.99.97.184/32",
                                      # WALAPAY
                                      "186.22.17.6/32",
                                      "3.134.238.10/32",
                                      "3.129.111.220/32",
                                      "52.15.118.168/32",
                                      # NILOS
                                      "3.69.120.63/32",
                                      "18.198.171.64/32",
                                      # DIGIT9_B2B AND DIGIT9
                                      "213.42.166.195/32",
                                      "86.96.250.195/32",
                                      "86.96.250.210/32",
                                      # WORKER_APPZ
                                      "43.204.16.203/32",
                                      "13.235.39.244/32",
                                      # VANCE CORE LONDON
                                      "18.132.153.123/32",
                                      "3.11.200.183/32",
                                      "13.134.84.208/32",
                                      # VANCE CORE MUMBAI
                                      "15.207.193.178/32",
                                      "3.111.143.119/32",
                                      "3.6.123.44/32",
                                      # VANCE CORE HYD(FOR DR)
                                      "40.192.63.51/32",
                                      "98.130.106.191/32",
                                      "98.130.107.120/32",
                                      #YOLAT
                                      "54.200.194.42/32",
                                     ]
  whitelist_ipv6                   = []
  blacklist_ipv4                   = []
  blacklist_ipv6                   = []
  // Rate limiting
  gateway_ip_rate_limit            = 1000
  ratelimit_action                 = "count"
  
  // Path allowlist feature
  enable_falcon_webhooks_path_allowlist = true
  enable_partner_dashboard_path_allowlist = true
  enable_omega_path_allowlist = true
  enable_all_paths_block = true

  // Domains
  proxy_domain = [
    "falcon.api.plutusremit.com",
    "eko.web-hooks.com"
  ]
  
  internal_acm_certificate_arn = dependency.regional_bootstrap.outputs.acm_certificate_arns["*.internal.genorim.xyz"] 
  
  # Certificate ARNs for each domain - make sure these match the domains above
  external_acm_certificate_arns = [
    dependency.regional_bootstrap.outputs.acm_certificate_arns["falcon.api.plutusremit.com"],
    dependency.regional_bootstrap.outputs.acm_certificate_arns["eko.web-hooks.com"] 
  ]
  // For manual proxy from old infra
  attach_temp_falcon_prod_tg       = true

  // Dashboard configuration
  enable_dashboard = true

  // Define services with their associated routes
  kong_configs = {
    "partner-dashboard-api" = {
      service = {
        host            = "falcon-prod-london-01-backend-alb.internal.genorim.xyz"
        protocol        = "http"
        port            = 80
        path            = "/"
        retries         = 5
        connect_timeout = 5000
        write_timeout   = 30000
        read_timeout    = 30000
      },
      route_config = {
        preserve_host  = true
        protocols      = ["http", "https"]
        strip_path     = false
      },
      routes = {
        "POST"    = ["~/partner-dashboard/api/v1/payout-dashboard/generate-excel-sheet$", "~/partner-dashboard/api/v1/dashboard/sign-up$", "~/partner-dashboard/api/v1/dashboard/sign-in$", "~/partner-dashboard/api/v1/payout-dashboard/mark-as-processed/(?<payoutSheetId>[^/]+)$", "~/partner-dashboard/api/v1/payout-dashboard/search$", "~/partner-dashboard/api/v1/payout-dashboard/download-payout-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/create-payout-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/download-excel-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/bulk-process$", "~/partner-dashboard/api/v1/payout-dashboard/update-status$"],
        "OPTIONS" = ["~/partner-dashboard/api/v1/payout-dashboard/generate-excel-sheet$", "~/partner-dashboard/api/v1/dashboard/sign-up$", "~/partner-dashboard/api/v1/dashboard/sign-in$", "~/partner-dashboard/api/v1/payout-dashboard/mark-as-processed/(?<payoutSheetId>[^/]+)$", "~/partner-dashboard/api/v1/payout-dashboard/search$", "~/partner-dashboard/api/v1/payout-dashboard/download-payout-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/create-payout-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/download-excel-sheet$", "~/partner-dashboard/api/v1/payout-dashboard/bulk-process$", "~/partner-dashboard/api/v1/payout-dashboard/update-status$", "~/partner-dashboard/api/v1/payout-dashboard/payout-sheet$", "~/partner-dashboard/api/v1/dashboard/me$"],
        "GET"     = ["~/partner-dashboard/api/v1/payout-dashboard/payout-sheet$", "~/partner-dashboard/api/v1/dashboard/me$", "~/api/v1/getUsers", "~/api/v1/user$"]
      }
    },
    "falcon-service-api" = {
      service = {
        host            = "falcon-prod-london-01-backend-alb.internal.genorim.xyz"
        protocol        = "http"
        port            = 80
        path            = "/"
      },
      routes = {
        "GET" = [
          "~/falcon/admin/api/v1/alphadesk-configs$",
          "~/falcon/admin/api/v1/alphadesk-configs/clients$",
          "~/falcon/admin/api/v1/alphadesk-configs/transaction-dashboard$",
          "~/falcon/admin/api/v1/analytics$",
          "~/falcon/admin/api/v1/clients/(?<name>[^/]+)$",
          "~/falcon/admin/api/v1/clients/balances$",
          "~/falcon/admin/api/v1/clients/balances/(?<name>[^/]+)$",
          "~/falcon/admin/api/v1/clients/balances/history/(?<name>[^/]+)$",
          "~/falcon/admin/api/v1/clients/webhooks/(?<client>[^/]+)$",
          "~/falcon/admin/api/v1/failure-limiter$",
          "~/falcon/admin/api/v1/reversal-limiter$",
          "~/falcon/admin/api/v1/rfi$",
          "~/falcon/admin/api/v1/rfi/(?<requestid>[^/]+)$",
          "~/falcon/admin/api/v1/rfi/documents$",
          "~/falcon/admin/api/v1/transactions/(?<transactionid>[^/]+)$",
          "~/falcon/admin/api/v1/vendor/(?<name>[^/]+)$",
          "~/falcon/admin/api/v1/vendor/configs$",
          "~/falcon/admin/api/v2/transaction$",
          "~/falcon/admin/feature-flag/(?<featureflag>[^/]+)$",
          "~/falcon/admin/fulfillments/(?<clienttxnid>[^/]+)$",
          "~/falcon/admin/payout/by-external-id$",
          "~/falcon/admin/test$",
          "~/falcon/admin/transaction$",
          "~/falcon/admin/transaction/lulu-transaction-id/(?<lulu_transaction_id>[^/]+)$",
          "~/falcon/api/v1/beneficiary/fraud$",
          "~/falcon/api/v1/clients/balances$",
          "~/falcon/api/v1/clients/rate$",
          "~/falcon/api/v1/rfi/documents$",
          "~/falcon/api/v1/vendors/(?<vendorname>[^/]+)/balances$",
          "~/falcon/api/v2/transactions$",
          "~/falcon/api/v2/transactions/client-id$",
          "~/falcon/balance$",
          "~/falcon/error/handle$",
          "~/falcon/health$",
          "~/falcon/ping$",
          "~/falcon/redis/(?<cachename>[^/]+)/(?<key>[^/]+)$",
          "~/falcon/redis/migration/(?<type>[^/]+)/(?<id>[^/]+)$",
          "~/falcon/transactions$",
          "~/falcon/transactions/(?<transactionid>[^/]+)$",
          "~/falcon/transactions/search-by-external-id/(?<transactionid>[^/]+)$",
          "~/falcon/api/v1/vendor/(?<vendorName>[^/]+)/reserve$",
          "~/falcon/swagger-ui$",
          "~/falcon/actuator$",
          "~/falcon/v3/api-docs$",
          "~/falcon/admin/api/v1/clients/(?<clientId>[^/]+)$", "~/falcon/api/v1/test-endpoint$", "~/falcon/api/v1/test-endpoint-2$", "~/api/v1/user", "~/api/users"
        ]
        "POST" = [
          "~/falcon/admin/api/v1/analytics/validation-history/search$",
          "~/falcon/admin/api/v1/clients$",
          "~/falcon/admin/api/v1/clients/balances/(?<name>[^/]+)$",
          "~/falcon/admin/api/v1/clients/search$",
          "~/falcon/admin/api/v1/clients/webhooks/(?<client>[^/]+)/test$",
          "~/falcon/admin/api/v1/clients/webhooks/(?<transactionid>[^/]+)$",
          "~/falcon/admin/api/v1/failure-limiter/re-enable$",
          "~/falcon/admin/api/v1/payout/force-fail$",
          "~/falcon/admin/api/v1/payout/force-process$",
          "~/falcon/admin/api/v1/payout/force-status-update$",
          "~/falcon/admin/api/v1/payout/force-status-update-bulk$",
          "~/falcon/admin/api/v1/payout/manual-force-update$",
          "~/falcon/admin/api/v1/payout/mark-complete-not-received$",
          "~/falcon/admin/api/v1/payout/partner/status/bulk$",
          "~/falcon/admin/api/v1/reversal-limiter$",
          "~/falcon/admin/api/v1/rfi/initiate-rfi$",
          "~/falcon/admin/api/v1/rfi/rejected-by-partner$",
          "~/falcon/admin/api/v1/rfi/search$",
          "~/falcon/admin/api/v1/rfi/submit$",
          "~/falcon/admin/api/v1/rfi/submitted-to-partner$",
          "~/falcon/admin/api/v1/transaction/(?<clientTxnId>[^/]+)$",
          "~/falcon/admin/api/v1/transaction/force-fail-txn/(?<transactionid>[^/]+)$",
          "~/falcon/admin/api/v1/transaction/force-fail-ybl-txn/(?<transactionid>[^/]+)$",
          "~/falcon/admin/api/v1/transaction/on-hold-handling$",
          "~/falcon/admin/api/v1/transactions$",
          "~/falcon/admin/api/v1/transactions/bulk-fetch-details$",
          "~/falcon/admin/api/v1/vendor$",
          "~/falcon/admin/api/v1/vendor/search$",
          "~/falcon/admin/clients$",
          "~/falcon/admin/api/v1/clients/copy-payout-config$",
          "~/falcon/admin/service/falcon/transactions/v1/completed$",
          "~/falcon/admin/service/falcon/transactions/v2/completed$",
          "~/falcon/admin/transaction/force-status-update$",
          "~/falcon/admin/transaction/manual-force-update$",
          "~/falcon/admin/transaction/update-force-routing-partner$",
          "~/falcon/api/analytics$",
          "~/falcon/api/v1/beneficiary/fraud$",
          "~/falcon/api/v1/rfi/reject$",
          "~/falcon/api/v1/rfi/submit$",
          "~/falcon/api/v1/vendors$",
          "~/falcon/api/v1/webhooks/send$",
          "~/falcon/api/v1/webhooks/subscribe$",
          "~/falcon/api/v1/webhooks/test$",
          "~/falcon/api/v2/transactions$",
          "~/falcon/api/v2/transactions/fetch/bulk$",
          "~/falcon/error/handle$",
          "~/falcon/recipients$",
          "~/falcon/transactions$",
          "~/falcon/transactions/(?<transactionid>[^/]+)/confirm$",
          "~/falcon/webhook/status-update$",
          "~/falcon/webhooks/eko/transaction-status/call-back$",
          "~/falcon/webhooks/gemzpay_bulk/(?<partner>[^/]+)$",
          "~/falcon/webhooks/hyperalpha$",
          "~/falcon/webhooks/indicpay/(?<partner>[^/]+)$",
          "~/falcon/webhooks/tangope/(?<partner>[^/]+)$",
          "~/falcon/webhooks/webtechpay/webinfo$",
          "~/falcon/webhooks/wowpe/(?<partner>[^/]+)$",
          "~/falcon/webhooks/xettle/(?<partner>[^/]+)$",
          "~/falcon/webhooks/genpay/(?<partners>[^/]+)$",
          "~/falcon/webhooks/vance-rda$",
          "~/falcon/webhooks/sovapay/(?<partners>[^/]+)$",
          "~/falcon/webhooks/(?<partnerGroups>[^/]+)/(?<partners>[^/]+)$",
          "~/falcon/admin/api/v1/clients/webhooks/(?<client>[^/]+)$",
          "~/falcon/api/v1/vendor/(?<name>[^/]+)/deactivate$",
          "~/falcon/api/v1/vendor/(?<vendorName>[^/]+)/balances$",
          "~/falcon/api/v1/beneficiary/fraud/check$",
          "~/falcon/redis/migration/transaction/(?<id>[^/]+)$"
        ]
        "PUT" = [
          "~/falcon/admin/api/v1/clients$",
          "~/falcon/admin/api/v1/clients/payout-partners$",
          "~/falcon/admin/api/v1/clients/webhooks/(?<client>[^/]+)$",
          "~/falcon/admin/api/v1/payout/force-status-sync$",
          "~/falcon/admin/api/v1/vendor/configs$",
          "~/falcon/admin/api/v1/vendor/update/exchange-rate$",
          "~/falcon/admin/feature-flag$",
          "~/falcon/api/v1/vendors/(?<name>[^/]+)/deactivate$",
          "~/falcon/api/v1/vendors/(?<vendorname>[^/]+)/reserve$",
          "~/falcon/api/v2/transactions/(?<transactionid>[^/]+)/confirm$",
          "~/falcon/api/v2/transactions/(?<transactionid>[^/]+)/reject$",
          "~/falcon/error/handle$"
        ]
        "PATCH" = [
          "~/falcon/admin/api/v1/reversal-limiter/re-enable$",
          "~/falcon/error/handle$"
        ]
        "DELETE" = [
          "~/falcon/admin/feature-flag/(?<featureflag>[^/]+)$",
          "~/falcon/error/handle$",
          "~/falcon/redis/(?<cachename>[^/]+)/(?<key>[^/]+)$"
        ]
        "OPTIONS" = [
          "~/falcon/error/handle$"
        ]
        "HEAD" = [
          "~/falcon/error/handle$"
        ]
      }
    },
    "eventbus-service" = {
      service = {
        host            = "falcon-prod-london-01-backend-alb.internal.genorim.xyz"
        protocol        = "http"
        port            = 80
        path            = "/"
        retries         = 5
        connect_timeout = 5000
        write_timeout   = 30000
        read_timeout    = 30000
      },
      routes = {
        "POST"    = ["~/eventbus/api/v1/events/publish$", "~/eventbus/api/v1/events/(?<eventName>[^/]+)$"],
      }
    },
    "omega-api" = {
      service = {
        host            = "falcon-prod-london-01-backend-alb.internal.genorim.xyz"
        protocol        = "http"
        port            = 80
        path            = "/"
        retries         = 5
        connect_timeout = 5000
        write_timeout   = 30000
        read_timeout    = 30000
      },
      routes = {
        "GET"    = ["~/omega/api/health$","~/omega/api/health/$","~/omega/api/configs$","~/omega/api/configs/$","~/omega/api/webhook$","~/omega/api/webhook/$","~/omega/auth/users$","~/omega/auth/users/$","~/omega/api/client-settlement-configs/config$","~/omega/api/client-settlement-configs/config/$","~/omega/auth/logout$","~/omega/auth/logout/$","~/omega/auth/admin/users/client/(?<clientName>[^/]+)$","~/omega/auth/admin/users/client/(?<clientName>[^/]+)/$","~/omega/auth/admin/users/(?<email>[^/]+)$","~/omega/auth/admin/users/(?<email>[^/]+)/$","~/omega/auth/users/switch-client$","~/omega/auth/users/switch-client/$","~/omega/auth/users/me$","~/omega/auth/users/me/$","~/omega/auth/users/clients$","~/omega/auth/users/clients/$", "~/omega/api/metrics/exchange-rates$", "~/omega/api/metrics/exchange-rates/$", "~/omega/api/settlements/download/(?<jobId>[^/]+)$", "~/omega/api/settlements/download/(?<jobId>[^/]+)/$"]
        "PUT"    = ["~/omega/api/webhook/$"]
        "PATCH"  = ["~/omega/auth/admin/users/(?<userId>[^/]+)/client/$"]
        "POST"   = ["~/omega/auth/users/$", "~/omega/api/webhook/$", "~/omega/api/settlements/statement/$", "~/omega/api/transactions/search/$", "~/omega/api/balances/search/$", "~/omega/api/metrics/dashboard/$", "~/omega/api/client-settlement-configs/config/$", "~/omega/api/auto-settlements/cron/$", "~/omega/auth/google/$", "~/omega/auth/login/$", "~/omega/auth/login/refresh/$", "~/omega/auth/login/verify/$", "~/omega/auth/admin/users/register/$"]
        "OPTIONS" = ["~/omega/api/health$","~/omega/api/health/$","~/omega/api/configs$","~/omega/api/configs/$","~/omega/api/webhook$","~/omega/api/webhook/$","~/omega/auth/users$","~/omega/auth/users/$","~/omega/api/client-settlement-configs/config$","~/omega/api/client-settlement-configs/config/$","~/omega/auth/logout$","~/omega/auth/logout/$","~/omega/auth/admin/users/client/(?<clientName>[^/]+)$","~/omega/auth/admin/users/client/(?<clientName>[^/]+)/$","~/omega/auth/admin/users/(?<email>[^/]+)$","~/omega/auth/admin/users/(?<email>[^/]+)/$","~/omega/auth/users/switch-client$","~/omega/auth/users/switch-client/$","~/omega/auth/users/me$","~/omega/auth/users/me/$","~/omega/auth/users/clients$","~/omega/auth/users/clients/$","~/omega/auth/admin/users/(?<userId>[^/]+)/client/$", "~/omega/auth/users/$", "~/omega/api/settlements/statement/$", "~/omega/api/transactions/search/$", "~/omega/api/balances/search/$", "~/omega/api/metrics/dashboard/$", "~/omega/api/client-settlement-configs/config/$", "~/omega/api/auto-settlements/cron/$", "~/omega/auth/google/$", "~/omega/auth/login/$", "~/omega/auth/login/refresh/$", "~/omega/auth/login/verify/$", "~/omega/auth/admin/users/register/$", "~/omega/api/metrics/exchange-rates$", "~/omega/api/metrics/exchange-rates/$", "~/omega/api/settlements/download/(?<jobId>[^/]+)$", "~/omega/api/settlements/download/(?<jobId>[^/]+)/$"]
      }
    }
  }
}
