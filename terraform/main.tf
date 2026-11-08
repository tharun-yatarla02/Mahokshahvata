# The app's Google Cloud infrastructure. Everything here already existed when
# Milestone 6 started (deploy/milestone4.sh, deploy/gke.sh created it), so
# imports.tf adopts it into state; monitoring.tf and ci.tf are new.
# What is NOT here, and why: ../docs/deploy/MILESTONE6.md ("Terraform coverage").
#
#   cd terraform
#   export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
#   terraform init && terraform plan        # expect "No changes."
terraform {
  required_version = ">= 1.6"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 7.0" }
  }
  # Created once with gcloud (versioned, private). State holds no secret values:
  # secrets are managed here, their versions are not.
  backend "gcs" {
    bucket = "project-262a2026-879b-4b49-88e-tfstate"
    prefix = "app"
  }
}

provider "google" {
  project = local.project
  region  = local.region
  zone    = local.zone
}

locals {
  project    = "project-262a2026-879b-4b49-88e"
  region     = "us-central1"
  zone       = "us-central1-a"
  runtime_sa = "serviceAccount:mahokshahvata-vm@${local.project}.iam.gserviceaccount.com"
  github     = "tharun-yatarla02/Mahokshahvata"
}
