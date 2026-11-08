# What the GitHub Actions deploy job (../.github/workflows/ci.yml) runs as.
# No key file: GitHub's OIDC token is exchanged for a short-lived Google token
# (Workload Identity Federation), and only pushes to master of our repository
# are accepted.

resource "google_project_service" "sts" {
  service            = "sts.googleapis.com"
  disable_on_destroy = false
}

resource "google_service_account" "deployer" {
  account_id   = "github-deployer"
  display_name = "GitHub Actions: build, push and roll out the app"
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "mahokshahvata"
  display_name                       = "Mahokshahvata repo, master only"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${local.github}' && assertion.ref == 'refs/heads/master'"
  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

resource "google_service_account_iam_member" "deployer_from_github" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${local.github}"
}

# Project level, not on the repository: our IAM admin role can't set
# repository-level policies. The project has one repository, so same reach.
resource "google_project_iam_member" "deployer_push" {
  project = local.project
  role    = "roles/artifactregistry.writer"
  member  = google_service_account.deployer.member
}

# set image, rollout status, rollout undo on the cluster. No IAM, SQL or secrets.
resource "google_project_iam_member" "deployer_gke" {
  project = local.project
  role    = "roles/container.developer"
  member  = google_service_account.deployer.member
}

output "workload_identity_provider" { value = google_iam_workload_identity_pool_provider.github.name }
output "deployer_email" { value = google_service_account.deployer.email }
