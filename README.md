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

## Logo

Ship the service's official mark and Marvin shows it in place of the emoji. `logo` is a path relative
to the package your provider class lives in, `.svg` or `.png`:

```python
class MyProvider(IntegrationProvider):
    icon = "🛰️"          # still the fallback
    logo = "logo.svg"    # src/my_package/logo.svg
```

`load_logo(provider)` returns `(bytes, content_type)` or `None`, and `info()["has_logo"]` says whether
the file was found. The file must be **package data** in your wheel. Hatchling (what the existing
providers use) includes every non-ignored file under the listed package, so this is enough as long as
the file is committed and not `.gitignore`d:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/my_package"]   # src/my_package/logo.svg ships with it
```

With setuptools, declare it explicitly:

```toml
[tool.setuptools.package-data]
my_package = ["logo.svg"]
```

Marvin validates the logo before serving it and falls back to `icon` when it refuses one: at most
64 KB; a PNG must start with the PNG signature; an SVG may not contain a `DOCTYPE`/`ENTITY`, `<script>`,
`<foreignObject>`, `on*=` event attributes, `href`/`xlink:href` other than `#fragment`, `javascript:`, or
`url(…)`/`@import` pointing anywhere but `#fragment`. Use a plain, self-contained mark — optimise it
(e.g. with SVGO) and inline any gradients.

## Options for an action input

An input whose valid values come from the service (a channel, a list, a workflow) can say where to get
them. Add `x-marvin-options` to the property in `input_schema`, naming a **read** action on the same
provider:

```python
ProviderAction(
    key="post",
    label="Post message",
    input_schema={
        "type": "object",
        "properties": {
            "channel": {
                "type": "string",
                "x-marvin-options": {"action": "list_channels", "value": "id", "label": "name",
                                     "args": {"archived": False}},   # args: optional, static
            },
        },
    },
)
```

Marvin's step editor and Run action form run `list_channels` through the connection and render a
searchable picker, keeping free text as the fallback (when the action fails, or for a value not in the
list). The referenced action returns either a list of objects or an object with an `items` list; each
object's `value` field becomes the stored value and its `label` field (falling back to the value) what
people see. Only actions named by some `x-marvin-options` hint can be run this way, at most 500 options
are shown, and the action must be side-effect free — it runs whenever someone opens the picker.

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

## Error handling

Raise `IntegrationError` with a `code` you choose, and declare how each code is handled. The core
applies the policy: your action never sleeps, retries or alerts anyone itself.

```python
from marvin_integration_sdk import Handle, IntegrationError, Retry

class MyProvider(IntegrationProvider):
    error_policy = {
        "rate_limited": Handle(retry=Retry(backoff=(120, 600, 3600)), then=Handle(review=True)),
        "auth_expired": Handle(notify=True, retry=Retry(backoff=(), on_recovery=True)),
        "*": Handle(review=True),                       # anything without its own entry
    }
    actions = (
        ProviderAction(key="post", label="Post",
                       error_policy={"duplicate": Handle(succeed=True)}),   # per-action, wins
    )

    def run_action(self, key, args, ctx):
        done = (ctx.resume or {}).get("posted", 0)      # a retry continues where it stopped
        r = ctx.http.post(..., headers={"Idempotency-Key": ctx.idempotency_key("post", args["id"])})
        if r.status_code == 429:
            raise IntegrationError("Rate limited", code="rate_limited",
                                   partial={"posted": done}, retry_after=float(r.headers.get("retry-after", 60)))
```

Lookup order is `action[code]` › `provider[code]` › `action["*"]` › `provider["*"]`; with no match the
action fails as usual. `review`, `notify` and `succeed` happen straight away, `retry` schedules more
attempts (past the end of `backoff` the last delay repeats), and `then` applies once they run out.
`ctx.idempotency_key(...)` is the same on every attempt of one retry chain, so a request the remote
already processed isn't applied twice. Malformed policies are rejected at `@register_provider`, and
Marvin shows each one in plain words (`Handle.describe()`), e.g. "retry 3× (2m, 10m, 1h), then send to review".

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
