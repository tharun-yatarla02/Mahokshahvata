"""
secrets.py

Loads the app's passwords and API keys from Google Secret Manager into the
process environment at startup, before any module reads them. Nothing is
written to disk; the values live only in this process's memory.

Enabled by SECRETS_PROJECT (the GCP project holding the secrets). The VM
reads them as its own service account, which has Secret Accessor on exactly
these secrets, so there is no key file. Without SECRETS_PROJECT (laptops,
CI) nothing happens and the usual environment / .env applies.

A secret that doesn't exist is skipped: the data-provider keys are optional.
Secret Manager reports a secret the caller has no access to as permission
denied (it won't confirm that the name exists), so for optional keys that is
treated the same as missing. DB_PASSWORD is required and always raises.
"""

import logging
import os

log = logging.getLogger(__name__)

# Environment variable -> secret name in Secret Manager.
SECRETS = {
    "DB_PASSWORD": "db-password",
    "ANTHROPIC_API_KEY": "anthropic-api-key",
    "QUIVER_API_KEY": "quiver-api-key",
    "MASSIVE_API_KEY": "massive-api-key",
    "BENZINGA_API_KEY": "benzinga-api-key",
}
REQUIRED = {"DB_PASSWORD"}


def load_secrets(project=None, client=None):
    """Fills in each variable from its secret's latest version. Returns the
    names of the variables loaded (never their values)."""
    project = project or os.environ.get("SECRETS_PROJECT")
    if not project:
        return []
    from google.api_core.exceptions import NotFound, PermissionDenied
    if client is None:
        from google.cloud import secretmanager
        client = secretmanager.SecretManagerServiceClient()

    loaded = []
    for variable, secret in SECRETS.items():
        name = f"projects/{project}/secrets/{secret}/versions/latest"
        try:
            value = client.access_secret_version(name=name).payload.data.decode("utf-8").strip()
        except (NotFound, PermissionDenied) as e:
            if variable in REQUIRED:
                raise
            log.info("Secret %s not available (%s); skipping", secret, type(e).__name__)
            continue  # optional key that this deployment doesn't use
        if value:
            os.environ[variable] = value
            loaded.append(variable)
    log.info("Loaded %d secret(s) from Secret Manager: %s", len(loaded), ", ".join(loaded) or "none")
    return loaded
