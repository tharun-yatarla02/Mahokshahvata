# Observability: the 5xx log-based metric, the alert on it, and the dashboard.
# The app writes one JSON log line per request (api/main.py request_log,
# cloud/log.py); GKE ships stdout to Cloud Logging as jsonPayload.

resource "google_logging_metric" "http_5xx" {
  name        = "app_http_5xx"
  description = "HTTP 5xx responses from the app: its JSON request log lines with status >= 500."
  filter      = <<-EOT
    resource.type="k8s_container"
    resource.labels.cluster_name="mahokshahvata"
    resource.labels.container_name="app"
    jsonPayload.status>=500
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "Team email"
  type         = "email"
  labels       = { email_address = "sidsidsid328@gmail.com" }
}

resource "google_monitoring_alert_policy" "http_5xx" {
  display_name = "App is returning HTTP 5xx"
  combiner     = "OR"
  conditions {
    display_name = "Any 5xx in the last minute"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.http_5xx.name}\" AND resource.type=\"k8s_container\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_SUM"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger { count = 1 }
    }
  }
  notification_channels = [google_monitoring_notification_channel.email.id]
  alert_strategy { auto_close = "1800s" }
  documentation {
    mime_type = "text/markdown"
    content   = "The app logged at least one HTTP 5xx in the last minute. Logs: resource.type=\"k8s_container\" jsonPayload.status>=500. Runbook: docs/deploy/MILESTONE6.md."
  }
}

resource "google_monitoring_dashboard" "app" {
  dashboard_json = jsonencode({
    displayName = "Mahokshahvata app"
    mosaicLayout = {
      columns = 12
      tiles = [
        { xPos = 0, yPos = 0, width = 6, height = 4, widget = {
          title = "HTTP 5xx errors (log-based metric, per minute)"
          xyChart = { dataSets = [{ plotType = "STACKED_BARS", timeSeriesQuery = { timeSeriesFilter = {
            filter      = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.http_5xx.name}\" resource.type=\"k8s_container\""
            aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_SUM", crossSeriesReducer = "REDUCE_SUM" }
          } } }] }
        } },
        { xPos = 6, yPos = 0, width = 6, height = 4, widget = {
          title = "Load balancer requests by response class"
          xyChart = { dataSets = [{ plotType = "LINE", timeSeriesQuery = { timeSeriesFilter = {
            filter      = "metric.type=\"loadbalancing.googleapis.com/https/request_count\" resource.type=\"https_lb_rule\""
            aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["metric.label.response_code_class"] }
          } } }] }
        } },
        { xPos = 0, yPos = 4, width = 6, height = 4, widget = {
          title = "CPU per pod (cores)"
          xyChart = { dataSets = [{ plotType = "LINE", timeSeriesQuery = { timeSeriesFilter = {
            filter      = "metric.type=\"kubernetes.io/container/cpu/core_usage_time\" resource.type=\"k8s_container\" resource.label.cluster_name=\"mahokshahvata\" resource.label.container_name=\"app\""
            aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_RATE", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.label.pod_name"] }
          } } }] }
        } },
        { xPos = 6, yPos = 4, width = 6, height = 4, widget = {
          title = "Memory per pod (bytes, limit 1 GiB)"
          xyChart = { dataSets = [{ plotType = "LINE", timeSeriesQuery = { timeSeriesFilter = {
            filter      = "metric.type=\"kubernetes.io/container/memory/used_bytes\" resource.type=\"k8s_container\" resource.label.cluster_name=\"mahokshahvata\" resource.label.container_name=\"app\" metric.label.memory_type=\"non-evictable\""
            aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_MEAN", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.label.pod_name"] }
          } } }] }
        } },
        { xPos = 0, yPos = 8, width = 6, height = 4, widget = {
          title = "Container restarts"
          xyChart = { dataSets = [{ plotType = "STACKED_BARS", timeSeriesQuery = { timeSeriesFilter = {
            filter      = "metric.type=\"kubernetes.io/container/restart_count\" resource.type=\"k8s_container\" resource.label.cluster_name=\"mahokshahvata\" resource.label.container_name=\"app\""
            aggregation = { alignmentPeriod = "60s", perSeriesAligner = "ALIGN_DELTA", crossSeriesReducer = "REDUCE_SUM", groupByFields = ["resource.label.pod_name"] }
          } } }] }
        } },
        { xPos = 6, yPos = 8, width = 6, height = 4, widget = {
          title      = "Alert: app is returning HTTP 5xx"
          alertChart = { name = google_monitoring_alert_policy.http_5xx.name }
        } },
      ]
    }
  })
}
