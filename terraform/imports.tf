# Adopting resources created before Terraform. Import blocks are no-ops once
# the resource is in state, so they stay as a record of what was adopted.
locals {
  p  = "project-262a2026-879b-4b49-88e"
  sa = "serviceAccount:mahokshahvata-vm@project-262a2026-879b-4b49-88e.iam.gserviceaccount.com"
}

import {
  to = google_compute_global_address.psa_range
  id = "projects/${local.p}/global/addresses/google-managed-services-default"
}
import {
  to = google_service_networking_connection.psa
  id = "projects/${local.p}/global/networks/default:servicenetworking.googleapis.com"
}
import {
  to = google_sql_database_instance.db
  id = "projects/${local.p}/instances/mahokshahvata-db"
}
import {
  to = google_sql_database.app
  id = "projects/${local.p}/instances/mahokshahvata-db/databases/mahokshahvata"
}
import {
  to = google_sql_user.app
  id = "${local.p}/mahokshahvata-db/app"
}
import {
  to = google_storage_bucket.data
  id = "${local.p}-app-data"
}
import {
  to = google_storage_bucket_iam_member.runtime_objects
  id = "b/${local.p}-app-data roles/storage.objectUser ${local.sa}"
}
import {
  to = google_secret_manager_secret.s_db_password
  id = "projects/${local.p}/secrets/db-password"
}
import {
  to = google_secret_manager_secret.s_anthropic_api_key
  id = "projects/${local.p}/secrets/anthropic-api-key"
}
import {
  to = google_secret_manager_secret.s_db_admin_password
  id = "projects/${local.p}/secrets/db-admin-password"
}
import {
  to = google_secret_manager_secret_iam_member.runtime_db_password
  id = "projects/${local.p}/secrets/db-password roles/secretmanager.secretAccessor ${local.sa}"
}
import {
  to = google_secret_manager_secret_iam_member.runtime_anthropic_api_key
  id = "projects/${local.p}/secrets/anthropic-api-key roles/secretmanager.secretAccessor ${local.sa}"
}
import {
  to = google_service_account.runtime
  id = "projects/${local.p}/serviceAccounts/mahokshahvata-vm@${local.p}.iam.gserviceaccount.com"
}
import {
  to = google_project_iam_member.runtime_logging_logwriter
  id = "${local.p} roles/logging.logWriter ${local.sa}"
}
import {
  to = google_project_iam_member.runtime_monitoring_metricwriter
  id = "${local.p} roles/monitoring.metricWriter ${local.sa}"
}
import {
  to = google_artifact_registry_repository.app
  id = "projects/${local.p}/locations/us-central1/repositories/mahokshahvata"
}
import {
  to = google_artifact_registry_repository_iam_member.runtime_reader
  id = "projects/${local.p}/locations/us-central1/repositories/mahokshahvata roles/artifactregistry.reader ${local.sa}"
}
import {
  to = google_compute_global_address.lb
  id = "projects/${local.p}/global/addresses/mahokshahvata-gke-ip"
}
import {
  to = google_container_cluster.app
  id = "projects/${local.p}/locations/us-central1-a/clusters/mahokshahvata"
}
import {
  to = google_container_node_pool.app
  id = "projects/${local.p}/locations/us-central1-a/clusters/mahokshahvata/nodePools/pool-medium"
}
