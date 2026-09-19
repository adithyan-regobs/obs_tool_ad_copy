{
      identifier = "${identifier}"
      name       = "${monitor_name}"
      type       = "${monitor_type}"
      query      = "${monitor_query}"
      message    = "${monitor_message}"
      monitor_thresholds = {
        critical = ${threshold_critical}
      }
      evaluation_delay = ${evaluation_delay}
      notify_audit     = ${notify_audit}
      include_tags     = ${include_tags}
      tags = [
        "resource_kind:${resource_kind}",
        "severity:${severity}",
        "source:${source}",
        "tenant:${tenant}",
        "environment:${environment}"
      ]
    }
