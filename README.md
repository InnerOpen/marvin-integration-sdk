# marvin-integration-sdk

The contract for building [Marvin](https://claude.ai/code) integrations — credentialed connections to
external services (deploy hooks, RSS, search indexes, notifiers…).

An integration is a **provider**: a manifest (credentials, config schema, actions, emitted events,
the workspace content it needs)
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

An optional `icon` (an emoji — Marvin renders it as text) is shown beside your provider in the UI.

## Declare the content it needs

A provider never touches the database. If your integration needs entry types, collections or
scheduled tasks to work, *declare* them and the core offers the list to the workspace for review —
nothing is created on install, and applying creates only what is missing, so a workspace that has
customised its copy keeps it when you ship a new version.

```python
from marvin_integration_sdk import ContentBlueprint

content = (
    ContentBlueprint(
        kind="entry_type",                      # entry_type | collection | scheduled_task
        slug="thing-log",
        name="Thing log",
        description="One row per thing done.",
        payload={"name": "Thing log", "schema_json": {"fields": [...]}},
    ),
    ContentBlueprint(
        kind="collection",
        slug="things-sent",
        name="Things sent",
        requires=("entry_type:thing-log",),     # within your own bundle; ordered for you on apply
        payload={"is_smart": True, "smart_rules": {"entry_types": ["thing-log"]}},
    ),
)
```

Mark `required=True` only for content an action genuinely reads or writes — everything else is a
suggestion the workspace can take or leave, and Marvin lists the two separately. "Announce published
entries" is an editorial decision, not a requirement.

You own the names of *your* content. For anything belonging to the workspace — which of their entry
types a rule should track — ask through `parameters` instead of guessing a slug.

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
