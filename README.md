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

An AI provider (`marvin.ai_providers`) declares one the same way, on its `AIProvider` class, beside its
`provider_type`; Marvin shows it in AI settings and Admin → Plugins. A package with two providers ships one file
each (`logo = "openai.svg"`, `logo = "azure.svg"`).

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

## What a post may hold: `post_limits` (0.19.0)

A provider that posts (a `social.post` action) states its network's numbers once, so Marvin can check a caption before it
goes out — what the feed shows before "… more", a link where it won't work, too many hashtags — the same checks for every
network, with each network's own limits:

```python
class InstagramProvider(IntegrationProvider):
    post_limits = {"max": 2200, "unit": "characters", "visible": 125, "hashtags_max": 30, "links": "not_clickable"}
```

`max` is required; `unit` is `characters` (default) or `graphemes`; `visible` is how much shows before "… more" (leave it
out when the whole post shows); `hashtags_max` only when the network has a limit; `links` is `clickable` (default),
`not_clickable` or `card`. `post_limits_of(provider)` reads it with the defaults filled in, and `check_post_limits` says
what's wrong with one. An unusable declaration is ignored, never an error. Marvin does the checking; the plugin only states
the numbers.

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
        kind="entry_type",                      # entry_type | collection | scheduled_task | playbook
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

## Teach Marvin about it: recipes and a guide (0.9.0)

Two optional declarations make Marvin's agents and its Workflow Library know your integration the way they know
Marvin's own features.

**Recipes** are ready-made workflows. Marvin lists them in its Workflow Library, tagged "from the <name> plugin
<version>", and offers each to a workspace once your integration is connected there. Nothing is created: the person
fills in the setup variables and saves it, switched off.

```python
from marvin_integration_sdk import WorkflowRecipe

recipes = (
    WorkflowRecipe(
        id="announce-entry-type",                         # shown as "<slug>.announce-entry-type"
        title="Announce published entries",
        outcome="Each published entry of one type is posted to the channel.",
        definition={                                      # Marvin's workflow format
            "trigger": {"type": "event", "event": "entry_published"},
            "conditions": [{"field": "entry.entry_type", "op": "eq", "value": "{{entry_type}}"}],
            "actions": [{"kind": "integration", "integration": "{{connection}}", "action": "send_message",
                         "args": {"text": "New: ${entry.title}"}}],   # ${…} is resolved when it runs
        },
        variables={                                       # every {{placeholder}}: type (= picker), description, example
            "entry_type": {"type": "entry_type_slug", "description": "Which entries to announce.", "example": "post"},
            "connection": {"type": "integration_slug", "description": "This integration's connection.", "example": "slack"},
        },
    ),
)
```

`check_recipe` holds a recipe to its shape (kebab-case id, every placeholder declared and every declared one used,
known variable types, an example for each, integration steps calling only your own actions); Marvin also validates
the definition, filled with the examples, against its workflow model — a recipe that fails is skipped, never shown.

**A guide** is a markdown file in your package — how to set it up, what to paste where, what it can't do:

```python
guide = "guide.md"   # package data beside the provider module, like `logo`; at most 16 KB; starts with "# Title"
```

Marvin's agents read it when building workflows in a workspace where your integration is connected, and its manual
search (`search_docs`) finds it. Write it for a person; the agents read the same text.

**Playbooks (0.15.0).** An integration can ship Playbooks, the written procedures Marvin's agents load: what makes a good
newsletter issue, how to read a failed build. Put a skill folder in your package data (`SKILL.md` with `name` and
`description`, plus optional `references/*.md`) and declare it:

```python
from marvin_integration_sdk import playbook_blueprint

content = (playbook_blueprint(__package__, "playbooks/acme-weekly-report"),)
```

Marvin lists it on the card under Optional. An admin adds it, and it arrives as a **draft** with the import review for
someone to read and publish; updates and edit protection work like a workflow's. The rules (`check_playbook` and the
conformance test check them; Marvin checks again):

- The name starts with your provider's slug and a hyphen (`acme-weekly-report`; underscores in the slug count as hyphens, so
  `cloudflare_pages` gives `cloudflare-pages-…`). A name already taken in the workspace is refused, never renamed.
- Text only (`.md .txt .json .csv .yaml`), no `scripts/` or script files, relative paths without `..`. `SKILL.md` at most
  64 KB; at most 20 other files of 64 KB each, 256 KB in all.
- `allowed-tools`, if you set it, names Marvin tools (`list_events`), never your integration's actions. It narrows what an
  agent may use; it can't widen it. An agent reaches your integration through your workflows.
- Write for any workspace, not yours. If the Playbook drafts text, say the agent writes it itself and saves it with
  `create_draft` / `update_draft`, not `compose_entry` / `revise_entry`.
- An older Marvin skips a Playbook declaration with a warning and loads the rest of your content.

**System Playbooks (0.16.0).** A Playbook can instead be offered in *every* workspace without anyone adding it: read-only,
labelled "System · <your plugin>", updated with your release. A workspace can switch it off or copy it to customise; an operator
can switch it off for the whole server. Use it for procedures nobody should have to install (reading a failed Check connection),
not for suggestions. Build it with `scope="system"` and list it apart from `content` (an older Marvin reads only `content`, so it
never offers one as an Add item):

```python
class AcmeProvider(IntegrationProvider):
    system_playbooks = (playbook_blueprint(__package__, "playbooks/acme-read-a-failed-build", scope="system"),)

PLUGIN = StoragePlugin(slug="bucket", name="Bucket storage", provider=BucketProvider,
                       playbooks=(playbook_blueprint(__package__, "playbooks/bucket-setup", scope="system"),))
```

`StoragePlugin` and `AIProviderPlugin` take the same list as `playbooks` (default empty). `check_playbook(bp, provider, scope="system")`
refuses a system Playbook in `content`, a workspace one in `system_playbooks` / `playbooks`, and (in the conformance tests) a name in
both. Names start with the provider's or plugin's slug and a hyphen. Rules for a system Playbook; 7 and 8 are refused, 6 and 7's
body half are warnings (`playbook_warnings`):

1. A provider's `guide.md` stays the reference for building workflows. A skill covers doing a job and points to the guide
   (`read_doc plugins/<provider>.md`); it never restates actions or recipes.
2. The manual explains what a feature is. A skill is a procedure (steps, checks, what a good result looks like) and links the page.
3. Workflow authoring stays with `workflow_authoring_guide` and the Builder: no "how to set up a workflow" skill (Marvin refuses
   one longer than 20 lines that doesn't just call the guide and `draft_workflow`).
4. Instructions say who, skills say how. A system skill never sets voice or tone ("if this workspace has a voice Playbook in your
   list, load it") and never says who the agent is.
5. Manual-only by default: no `disable-model-invocation` key means manual-only (people and workflows name it). Set it to `false`
   only for a Playbook that helps in nearly any conversation.
6. `allowed-tools` is rarely right. It is allowed (narrowing is a safety feature) but warned about, and the admin page shows it.
7. Write it yourself: a skill that drafts text saves with `create_draft` / `update_draft`. `compose_entry` / `revise_entry` in
   `allowed-tools` is refused; in the body it is warned about.
8. Slugs only: a UUID-looking token is refused, since ids differ in every workspace.

**Conformance kit.** Subclass `IntegrationContract` in your tests to check all of this (and that `info()`
serialises, keys are unique and each Playbook, workspace or system, is well formed) without Marvin:

```python
from marvin_integration_sdk.testing import IntegrationContract

class TestMyContract(IntegrationContract):
    @pytest.fixture
    def provider(self):
        return MyProvider()
```

A storage or AI plugin that ships system Playbooks subclasses `PluginPlaybooksContract` (same module) with a `plugin` fixture.

## Hand-offs: follow the work you start (0.13.0)

An action that only *starts* work somewhere else (an n8n workflow, a build, a render) can declare `hands_off`.
Marvin then keeps one record of each hand-off, with its status, the link to the run and what came back, under
Settings > Automation > Results, and flags the ones that failed or never replied. The workflow author adds no step
for it: Marvin records the hand-off **in flight** when the step succeeds and **failed** when the call itself fails.

```python
from marvin_integration_sdk import HandsOff, ProviderAction

ProviderAction(
    key="trigger_workflow",
    label="Trigger workflow",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}, "entry_id": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"execution_id": {"type": "string"}, "execution_url": {"type": "string"}}},
    hands_off=HandsOff(
        remote_id="execution_id",    # key in the action's result: the run's id over there
        remote_url="execution_url",  # key in the action's result: a link to the run
        target="path",               # key in the action's args: what was asked (shown on the record)
        statuses={"success": "succeeded", "error": "failed", "waiting": "in_flight"},
    ),
)
```

All four fields are optional. The service reports back (a webhook it calls, a poll you add) and a workflow settles
the record with the core `automation_result` step, passing the connection and the status in **your service's words**.
Marvin turns them into its own three (`in_flight`, `succeeded`, `failed`) by calling `provider.result_status(word)`,
which by default reads `HandsOff.statuses` and accepts Marvin's three as they are; override it for words that need
more than a lookup. A word it does not know leaves the record in flight with the word in its message, so a person can read it.
The recipe or blueprint that receives the reply therefore stays free of your service's vocabulary. `hands_off` is
checked when the provider registers (a status that is not one of Marvin's three is refused), and the conformance
kit asserts the declaration is clean.

### Sometimes a hand-off, sometimes not: `record_if` (0.14.0)

An action that only *sometimes* starts work elsewhere (a newsletter email that is sent, or only saved as a draft) names an
output key that says so. Marvin records the hand-off only when that key is truthy in the action's result:

```python
ProviderAction(
    key="create_issue_email",
    label="Create issue email",
    output_schema={"type": "object", "properties": {"email_id": {"type": "string"}, "sent": {"type": "boolean"}}},
    hands_off=HandsOff(remote_id="email_id", record_if="sent"),  # a draft returns sent=False: nothing is recorded
)
```

`record_if` is empty by default (every successful call is a hand-off). The key must be a property of the action's
`output_schema`, which therefore has to declare it (checked at registration and by the conformance kit, so a typo cannot
silently record nothing). A call that fails has no result to read, so it records nothing either: its failure reaches people
through the error policy, as for any action. Marvin also records a hands-off action it runs itself, outside a workflow step
(a site rebuild's deploy action, wired to an event), the same way; there is no step to declare anything in.

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

## Storage plugins

The same SDK carries the storage contract (`marvin_integration_sdk.storage`): where Marvin keeps
uploaded assets, and where its backup engine writes. A storage plugin is installed site-wide by the
platform operator, like an integration, and offers either or both of:

- an asset **provider** (`StorageProvider`): put/get/delete/exists, `get_public_url`, `get_metadata`,
  `iter_keys(prefix)` (the backup engine mirrors assets from any provider with it) and an optional
  `checksum(key, algorithm)`. Selected with `STORAGE_PROVIDER=<slug>`.
- a backup **target** (`BackupTarget`): `put_file(key, path, metadata)`, `get(key, dest) -> metadata`,
  `list(prefix) -> {key: TargetObject}`, `delete(keys)` and `head(key)`. A `TargetObject` carries the
  size and a digest with its `hashlib` algorithm, so unchanged assets aren't copied twice.

Each class declares the environment variables it reads (`Setting`, secrets marked so Marvin masks
them) and builds itself in `from_config(config)`:

```python
from marvin_integration_sdk.storage import BackupTarget, Setting, StoragePlugin, StorageProvider

class BucketProvider(StorageProvider):
    slug = "bucket"
    settings = (Setting("BUCKET_NAME", required=True), Setting("BUCKET_KEY", secret=True, required=True))

    @classmethod
    def from_config(cls, config):
        return cls(config["BUCKET_NAME"], config["BUCKET_KEY"])
    ...

PLUGIN = StoragePlugin(slug="bucket", name="Bucket storage", provider=BucketProvider, target=BucketTarget)
```

```toml
[project.entry-points."marvin.storage_providers"]
bucket = "my_package:PLUGIN"
```

A provider can also hold one workspace's assets in that workspace's own bucket ("bring your own storage",
0.11.0). It declares `workspace_settings` (the settings a workspace supplies, a subset of `settings` by `env`
name), `endpoint_setting` (Marvin checks that endpoint against its policy before connecting) and
`public_url_setting`. Marvin builds the provider from the workspace's values alone, never its environment, so
every credential in `workspace_settings` must be `required`; `workspace_settings_problems(cls)` lists what is
wrong with a declaration, and the conformance kit asserts it is empty. Leave `workspace_settings` empty to not
offer workspace buckets. A `BackupTarget` can declare `workspace_settings` and
`endpoint_setting` the same way (0.12.0), for a workspace's own private backup bucket.

Marvin core ships the built-in `local` provider and `local` target; it refuses to start when
`STORAGE_PROVIDER` names a slug no installed plugin provides. Run the conformance kit against your
classes (Marvin runs it against its built-ins), and use the in-memory reference implementations in
`marvin_integration_sdk.storage.memory` as fakes:

```python
from marvin_integration_sdk.storage.testing import BackupTargetContract, StorageProviderContract

class TestBucketProvider(StorageProviderContract):
    @pytest.fixture
    def provider(self, bucket):
        return BucketProvider(bucket, "key")
```

## AI provider plugins

A model vendor (OpenAI, Anthropic, Ollama…) is a plugin too: `marvin_integration_sdk.ai` carries the
contract. It is installed site-wide by the platform operator; every workspace can then choose it in its
AI settings, with the platform's credentials or its own. One package per vendor.

A provider subclasses `AIProvider` and implements `complete`, `complete_structured`, `list_models` and
`test_connection` (plus `complete_with_tools`, `embed`, `pull_model` when its capability flags say so),
speaking Marvin's vendor-neutral `Message` / `ToolCall` / `ToolDefinition` / `ImagePart` /
`CompletionResult`. It declares:

- `credentials`: `Credential`s by key. `api_key` comes from a secret, `base_url` from settings or the
  workspace's provider row, any other key (Azure's `api_version`) is an option. In platform mode Marvin
  reads `<SLUG>_<KEY>` (`OPENAI_API_KEY`), and it masks secrets wherever it shows them.
- capability flags: `supports_vision`, `supports_structured_output`, `supports_embeddings`,
  `supports_tool_calls`, `supports_model_pull`.
- `prices` (`ModelPrice` per model id, USD per million tokens) or `self_hosted = True`; a dated
  snapshot (`gpt-4o-2024-08-06`) takes its base id's price. Prices live with the provider, so a new
  model's price is a plugin release, not a Marvin one. A `ModelPrice` may also carry
  `cached_input_per_1m` and `cache_write_per_1m` (prompt-cache rates; an unset one is the input rate),
  and a `CompletionResult` reports `cached_prompt_tokens` / `cache_write_tokens` — the parts of
  `prompt_tokens` the provider's prompt cache served or stored — so Marvin prices a cached prompt at
  what it actually costs. A provider that caches should ask for it (Anthropic's `cache_control`) and
  report it; one that doesn't leaves both at 0.
- `default_model`, `suggested_models` (the AI settings' model picker) and `default_embedding_model`.

```python
from marvin_integration_sdk.ai import API_KEY, BASE_URL, AIProvider, AIProviderPlugin, ModelPrice

class AcmeProvider(AIProvider):
    provider_type = "acme"
    display_name = "Acme AI"
    credentials = (API_KEY, BASE_URL)
    supports_tool_calls = True
    prices = {"acme-large": ModelPrice(input_per_1m=2.0, output_per_1m=8.0)}
    default_model = "acme-large"

    @classmethod
    def from_credentials(cls, values):
        return cls(api_key=values["api_key"], base_url=values["base_url"])
    ...

plugin = AIProviderPlugin(slug="acme", name="Acme AI", provider=AcmeProvider)
```

```toml
[project.entry-points."marvin.ai_providers"]
acme = "acme_marvin:plugin"
```

A plugin whose slug matches a provider built into Marvin core replaces it (that is how a vendor leaves
core without a flag day); two plugins can't share a slug. Run the conformance kit over a fake of your
vendor's server: implement `FakeTransport` (queue neutral replies, translate them to your wire format,
record the request) and subclass `AIProviderContract`. `marvin_integration_sdk.ai.fake` holds the
reference `FakeAIProvider` + `ScriptedTransport`, which pass the kit and serve as the fake provider in
Marvin's own tests.

```python
from marvin_integration_sdk.ai.testing import AIProviderContract

class TestAcmeProvider(AIProviderContract):
    @pytest.fixture
    def transport(self):
        return AcmeFakeServer()          # a FakeTransport

    @pytest.fixture
    def provider(self, transport):
        return AcmeProvider(api_key="test-key", http_client=transport.client())
```
