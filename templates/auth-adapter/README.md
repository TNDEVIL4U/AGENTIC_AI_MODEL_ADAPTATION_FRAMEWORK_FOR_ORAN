# Auth and secrets adapter template

A starting point for an identity system or a secret store the framework does not ship an
adapter for (docs/adapters/auth.md).

- `adapter.py`: an `AuthPort` adapter. It checks opaque bearer tokens against a JSON file of
  their SHA-256 digests. Replace `_lookup()` with a call to your identity system, such as an
  OAuth2 introspection endpoint, LDAP or a vendor SDK.
- `secrets_adapter.py`: a `SecretsPort` adapter. It reads values from one JSON document.
  Replace `_load()` with your secret manager.

Run the conformance suites against your adapters before registering them:

```python
import pytest
from oran_adapt.conformance.auth import (
    AUTH_CHECKS, SECRETS_CHECKS, AuthContext, SecretsContext,
)
from oran_adapt.core.enums import Role

@pytest.mark.parametrize("check", sorted(AUTH_CHECKS))
def test_auth_conformance(check, tmp_path):
    tokens = {role: f"{role.value.lower()}-token-0123456789" for role in Role}
    path = tmp_path / "tokens.json"
    path.write_text(json.dumps({digest(t): f"{r.value}:{r.value.lower()}"
                                for r, t in tokens.items()}))
    ctx = AuthContext(
        credentials=lambda role: {"Authorization": f"Bearer {tokens[role]}"},
        forgeries={"unknown token": {"Authorization": "Bearer not-a-real-token-123"}},
    )
    AUTH_CHECKS[check](TokenFileAuth(str(path)), ctx)

@pytest.mark.parametrize("check", sorted(SECRETS_CHECKS))
def test_secrets_conformance(check, tmp_path):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"dataset_http_token": "s3cr3t-value"}))
    SECRETS_CHECKS[check](JsonFileSecrets(str(path)),
                          SecretsContext(known={"dataset_http_token": "s3cr3t-value"}))
```

`tests/unit/test_phase9_security.py` runs exactly this against the template, so the template
stays conformant.
