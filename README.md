# marvin-integration-sdk

The contract for building [Marvin](https://claude.ai/code) integrations — credentialed connections to
external services (deploy hooks, RSS, search indexes, notifiers…).

An integration is a **provider**: a manifest (credentials, config schema, actions, emitted events)
plus handlers. Providers are pure with respect to Marvin — they receive `config`, the resolved
`secret`, a `logger`, and an `http` helper, and they **return** results/events. They never touch the
database or the event bus; the core owns all persistence and dispatch. That keeps this SDK tiny (zero
heavy dependencies) and your integration decoupled from Marvin's internals.

## Write a provider

```python
from marvin_integration_sdk import (
    CATEGORY_DESTINATION, CredentialField, ProviderAction,
    IntegrationProvider, register_provider,
)

@register_provider
class MyProvider(IntegrationProvider):
    slug = "my_service"
    name = "My Service"
    category = CATEGORY_DESTINATION
    credentials = (CredentialField(key="token", label="API Token"),)
    actions = (ProviderAction(key="ping", label="Ping"),)

    def check(self, ctx):
        r = ctx.http.get("https://api.example.com/health",
                         headers={"Authorization": f"Bearer {ctx.secret}"})
        return ("ok", None) if r.ok else ("error", f"HTTP {r.status_code}")

    def run_action(self, key, args, ctx):
        r = ctx.http.post("https://api.example.com/ping", json=args,
                          headers={"Authorization": f"Bearer {ctx.secret}"})
        return {"status": r.status_code}
```

## Ship it

Expose the provider via an entry point so an installed Marvin discovers it automatically:

```toml
# pyproject.toml
[project.dependencies]
marvin-integration-sdk = ">=0.1,<1"

[project.entry-points."marvin.integrations"]
my_service = "my_package:MyProvider"
```

Then, on a Marvin host: `uv add my-package` → restart → your integration appears in the catalog.
No core changes, no frontend changes.
