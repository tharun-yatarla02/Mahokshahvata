# The app's runtime infrastructure (created in Milestones 4-5, imported here).

# --- Private networking for Cloud SQL (private services access) -------------
resource "google_compute_global_address" "psa_range" {
  name          = "google-managed-services-default"
  description   = "Range for Google-managed services (Cloud SQL private IP)"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = "10.27.48.0"
  prefix_length = 20
  network       = "projects/${local.project}/global/networks/default"
}

resource "google_service_networking_connection" "psa" {
  network                 = "projects/${local.project}/global/networks/default"
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.psa_range.name]
}

# --- Cloud SQL: PostgreSQL 16, private IP only, TLS required -----------------
resource "google_sql_database_instance" "db" {
  name                = "mahokshahvata-db"
  database_version    = "POSTGRES_16"
  region              = local.region
  deletion_protection = true
  settings {
    tier                        = "db-f1-micro"
    edition                     = "ENTERPRISE"
    availability_type           = "ZONAL"
    activation_policy           = "ALWAYS"
    disk_type                   = "PD_SSD"
    disk_size                   = 10
    disk_autoresize             = true
    deletion_protection_enabled = true
    enable_dataplex_integration = true # was on when imported
    ip_configuration {
      ipv4_enabled    = false # no public IP
      private_network = "projects/${local.project}/global/networks/default"
      ssl_mode        = "ENCRYPTED_ONLY"
    }
    backup_configuration {
      enabled    = true
      start_time = "08:00"
    }
  }
  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "app" {
  name     = "mahokshahvata"
  instance = google_sql_database_instance.db.name
}

# The app's database user. Its password is generated outside Terraform and
# lives only in Secret Manager (db-password), so it never enters the state.
resource "google_sql_user" "app" {
  name     = "app"
  instance = google_sql_database_instance.db.name
}

# --- Storage bucket (profile photos, sentiment history): private -------------
resource "google_storage_bucket" "data" {
  name                        = "${local.project}-app-data"
  location                    = "US-CENTRAL1"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
}

# --- Secrets: the containers only, never the values --------------------------
resource "google_secret_manager_secret" "s_db_password" {
  secret_id = "db-password"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret" "s_anthropic_api_key" {
  secret_id = "anthropic-api-key"
  replication {
    auto {}
  }
}
resource "google_secret_manager_secret" "s_db_admin_password" { # the app can't read this one
  secret_id = "db-admin-password"
  replication {
    auto {}
  }
}

# --- The app's identity (GKE nodes run as it) and its least-privilege roles --
resource "google_service_account" "runtime" {
  account_id   = "mahokshahvata-vm"
  display_name = "Mahokshahvata VM (app runtime)"
}
resource "google_secret_manager_secret_iam_member" "runtime_db_password" {
  secret_id = google_secret_manager_secret.s_db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.runtime_sa
}
resource "google_secret_manager_secret_iam_member" "runtime_anthropic_api_key" {
  secret_id = google_secret_manager_secret.s_anthropic_api_key.id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.runtime_sa
}
resource "google_storage_bucket_iam_member" "runtime_objects" {
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectUser"
  member = local.runtime_sa
}
resource "google_artifact_registry_repository_iam_member" "runtime_reader" {
  location   = google_artifact_registry_repository.app.location
  repository = google_artifact_registry_repository.app.name
  role       = "roles/artifactregistry.reader"
  member     = local.runtime_sa
}
resource "google_project_iam_member" "runtime_logging_logwriter" {
  project = local.project
  role    = "roles/logging.logWriter"
  member  = local.runtime_sa
}
resource "google_project_iam_member" "runtime_monitoring_metricwriter" {
  project = local.project
  role    = "roles/monitoring.metricWriter"
  member  = local.runtime_sa
}

# --- Image registry, with the cleanup policy from deploy/registry-cleanup-policy.json
resource "google_artifact_registry_repository" "app" {
  repository_id = "mahokshahvata"
  location      = local.region
  format        = "DOCKER"
  description   = "Mahokshahvata app images (Milestone 3)"
  cleanup_policies {
    id     = "keep-5-newest"
    action = "KEEP"
    most_recent_versions { keep_count = 5 }
  }
  cleanup_policies {
    id     = "keep-v1"
    action = "KEEP"
    condition {
      tag_state    = "TAGGED"
      tag_prefixes = ["v1"]
    }
  }
  cleanup_policies {
    id     = "delete-untagged-after-7-days"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }
  cleanup_policies {
    id     = "delete-old-after-30-days"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "2592000s"
    }
  }
}

# --- GKE: zonal cluster + its node pool, and the load balancer's static IP ---
resource "google_compute_global_address" "lb" {
  name = "mahokshahvata-gke-ip" # 34.8.137.93 -> https://34-8-137-93.sslip.io (k8s/ingress.yaml)
}

resource "google_container_cluster" "app" {
  name                = "mahokshahvata"
  location            = local.zone
  deletion_protection = true
  network             = "projects/${local.project}/global/networks/default"
  subnetwork          = "projects/${local.project}/regions/${local.region}/subnetworks/default"
  networking_mode     = "VPC_NATIVE"
  release_channel { channel = "REGULAR" }
  ip_allocation_policy {
    cluster_secondary_range_name = "gke-mahokshahvata-pods-561a7ded"
  }
  logging_config { enable_components = ["SYSTEM_COMPONENTS", "WORKLOADS"] } # WORKLOADS ships the app's JSON stdout
  addons_config {
    http_load_balancing { disabled = false }
    horizontal_pod_autoscaling { disabled = false }
  }
  # The nodes are the separately managed pool below. The cluster's own
  # node_config describes the long-gone default pool; leave it alone.
  lifecycle {
    ignore_changes = [node_config, node_pool, initial_node_count, monitoring_config]
  }
}

resource "google_container_node_pool" "app" {
  name     = "pool-medium"
  cluster  = google_container_cluster.app.name
  location = local.zone
  autoscaling {
    min_node_count = 2
    max_node_count = 3
  }
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  node_config {
    machine_type    = "e2-medium"
    disk_type       = "pd-standard"
    disk_size_gb    = 30
    service_account = google_service_account.runtime.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"] # IAM roles decide access
    metadata        = { disable-legacy-endpoints = "true" }
  }
  lifecycle {
    ignore_changes = [node_count, initial_node_count, version] # the cluster autoscaler and auto-upgrade own these
  }
}
