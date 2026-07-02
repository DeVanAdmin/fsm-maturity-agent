# connectors/shared/dataverse_upsert.py
"""Single shared Dataverse write utility for all five FSM connectors.

Every connector — D365, Salesforce, ServiceNow, IFS, and Custom — writes to the
same target Dataverse org in exactly the same way: an idempotent, alternate-key
PATCH keyed on ``fma_externalsourceid``. Rather than copy that logic into each
connector, both the write-token acquisition (``get_dataverse_token``) and the
upsert itself (``upsert_record``) live here as the one tested code path. Each
connector authenticates to *its own* source system however it must, then routes
all Dataverse writes through this module.
"""

import json

# `requests` is imported lazily so pure-logic unit tests can import connector
# modules (which import this file) without the HTTP stack installed. The
# network functions guard on it and fail loudly if it is missing.
try:
    import requests
except ImportError:  # pragma: no cover - exercised only when requests is absent
    requests = None


def _require_requests():
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required for network operations. "
            "Install dependencies with: pip install -r requirements.txt"
        )


def get_dataverse_token(tenant_id: str, client_id: str, client_secret: str, resource: str) -> str:
    """
    Gets an OAuth 2.0 access token (client credentials flow) for the target
    Dataverse org. `resource` is the Dataverse org base URL; the scope is
    `{resource}/.default`. Every connector calls this to obtain the token it
    uses for writes, regardless of how it authenticated to its own source.
    """
    _require_requests()
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": f"{resource.rstrip('/')}/.default",
    }
    resp = requests.post(token_url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def upsert_record(
    record: dict,
    table: str,
    alternate_key_column: str,
    alternate_key_value: str,
    target_url: str,
    access_token: str,
) -> dict:
    """
    Generic Dataverse upsert via PATCH against an alternate key.
    Used by all connectors. The alternate-key URL pattern
    `PATCH /api/data/v9.2/{table}({column}='{value}')` means the write is
    idempotent: it updates the matching row if present, or creates it if not,
    so re-running a connector never produces duplicates.

    Returns a dict with the HTTP status code, the key value written, and a
    `created` flag (201 = created, 204 = updated).
    """
    _require_requests()
    url = (
        f"{target_url.rstrip('/')}/api/data/v9.2/"
        f"{table}({alternate_key_column}='{alternate_key_value}')"
    )
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
        "If-Match": "*",  # allow update; combined with PATCH this is an upsert
    }
    resp = requests.patch(url, headers=headers, data=json.dumps(record), timeout=60)
    resp.raise_for_status()
    return {
        "status_code": resp.status_code,
        "key": alternate_key_value,
        "created": resp.status_code == 201,
    }
