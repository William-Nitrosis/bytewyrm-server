# ByteWyrm

**Tiny backend tools for student game projects.**

Current development version: **0.10.0**

Public API: `https://api.bytewyrm.dev`

ByteWyrm is intentionally small and constrained. A **Project** groups a student game, its API keys, limits and tools. The first tool is **Store**: a tiny schema-validated datastore for integers, floats, booleans and short text.

Planned future tools can live alongside Store without changing the Project/key model, for example Config, Counters, Saves and other small game-backend utilities. Leaderboards are intentionally treated as a Store use case rather than a separate persistence system.

## Product model

The public/product vocabulary is now:

```text
ByteWyrm
└── Project
    ├── API keys
    ├── Project limits
    └── Tools
        └── Store
            ├── Schema
            └── Records
```

The SQLite implementation still uses historical internal table/column names such as `containers` and `container_id`. Those are deliberately left alone because they are implementation details and renaming them would create a pointless database migration.

### New identifiers

New Projects use:

```text
prj_...
```

New API keys use:

```text
bwk_...
```

Existing development `ctr_...` Project IDs and `ssk_...` API keys remain valid. Authentication is based on the stored key hash, not the prefix.

## Upgrade to v0.10

**Do not delete `server.db`.** ByteWyrm automatically upgrades the existing database in place.

v0.10 adds simple schema-aware Store queries: sorting plus one `equals`, `greater_than`, or `less_than` filter. It also adds typed query indexes and correct cursor pagination for sorted results. Existing Project data and Store behaviour do not change.

Replace the application files and rebuild:

```bash
docker compose down
docker compose up -d --build
```

Persistent data remains at:

```text
/srv/data/bytewyrm/database/server.db
```

Check the stack:

```bash
docker compose ps
```

Typical services:

```text
bytewyrm-api
bytewyrm-admin
bytewyrm-tunnel
```

## Public service

The Cloudflare Tunnel publishes only the student-facing API:

```text
Internet
   ↓
https://api.bytewyrm.dev
   ↓
Cloudflare Tunnel
   ↓
bytewyrm-api
```

The admin dashboard remains LAN-only and is not on the tunnel's Docker network.

### Public API

Core:

```text
GET /                 service information
GET /health           health check
GET /whoami           authenticated Project/key information
```

Store:

```text
GET  /store/schema
GET  /store/records?limit=100&before_id=<record_id>
GET  /store/records?sort_by=score&reverse=true&limit=10
GET  /store/records?where=completed&equals=true
GET  /store/records?where=score&greater_than=1000
POST /store/records
```

All authenticated public endpoints use a Project API key:

```text
Authorization: Bearer bwk_...
```

Example Store write:

```http
POST /store/records
Authorization: Bearer bwk_...
Content-Type: application/json
```

```json
{
  "player": "Drake",
  "score": 12500
}
```

The API key determines the Project. The client never supplies a Project ID when reading or writing Store data.

### Simple Store queries

Store reads intentionally support only a small query vocabulary:

```text
sort_by=<field>
reverse=true|false
where=<field>
equals=<value>
greater_than=<number>
less_than=<number>
limit=1..500
```

Only one filter (`equals`, `greater_than`, or `less_than`) may be used at a time. `greater_than` and `less_than` work only with integer/float fields. Field names are resolved against the Project's Store schema before any SQL is built.

Example high-score query:

```text
GET /store/records?sort_by=score&reverse=true&limit=10
```

Example filtered query:

```text
GET /store/records?where=completed&equals=true&sort_by=score&reverse=true
```

Normal newest-first pagination continues to use `before_id`. Sorted queries use an opaque `cursor` returned in the `X-ByteWyrm-Next-Cursor` response header so tied sort values paginate correctly.

### Temporary compatibility aliases

The pre-v0.7 public routes remain functional for now but are hidden from OpenAPI/Swagger:

```text
GET  /schema    -> GET  /store/schema
GET  /records   -> GET  /store/records
POST /records   -> POST /store/records
```

They can be removed after the ByteWyrm Python library becomes the normal student interface.

Public Swagger:

```text
https://api.bytewyrm.dev/docs
```

## Admin dashboard

The dashboard is served by the separate `bytewyrm-admin` service and is intended only for the trusted LAN.

Typical address:

```text
http://192.168.50.2:8001/
```

Admin API docs:

```text
http://192.168.50.2:8001/docs
```

The dashboard uses the separate `ADMIN_TOKEN`; student `bwk_...`/legacy `ssk_...` keys have no admin authority.

The dashboard uses Project/Store terminology throughout and has an obsidian/ember ByteWyrm theme.

### Dashboard capabilities

- view/create Projects
- enable or disable a Project
- set request/rate limits
- set the Store record cap and full-Store policy
- choose Append / Replace latest / Keep highest / Keep lowest Store behaviour
- choose a Store record key field and optional high/low comparison field
- restrict student reads to Project-wide or per-API-key records
- optionally restrict keyed updates to the key that originally created the record
- create/remove Store schema fields while the Store is empty
- view Store records
- manually add/edit/delete Store records
- clear the Store to unlock schema editing
- create/revoke/re-enable Project API keys
- inspect per-key reads, writes, rejected requests, rate-limit hits and recent request rates
- permanently delete a Project

## Admin JSON API

All `/admin/*` endpoints require:

```text
Authorization: Bearer <ADMIN_TOKEN>
```

### Projects

```text
GET    /admin/projects
POST   /admin/projects
GET    /admin/projects/{project_id}
PATCH  /admin/projects/{project_id}
DELETE /admin/projects/{project_id}?confirm=true
```

Create example:

```json
{
  "name": "Asteroids",
  "store_max_records": 500,
  "store_overflow_policy": "reject",
  "max_request_bytes": 2048,
  "read_rate_limit": 100,
  "write_rate_limit": 20
}
```

Project responses separate Project-level data from tools:

```json
{
  "id": "prj_...",
  "name": "Asteroids",
  "enabled": true,
  "keys": {
    "total": 2,
    "enabled": 2
  },
  "limits": {
    "max_request_bytes": 2048,
    "read_rate_limit": 100,
    "write_rate_limit": 20
  },
  "tools": {
    "store": {
      "enabled": true,
      "field_count": 2,
      "record_count": 12,
      "max_records": 500,
      "overflow_policy": "reject",
      "record_mode": "keep_highest",
      "key_field": "player",
      "compare_field": "score",
      "read_scope": "project",
      "creator_only_updates": true,
      "schema_editable": false
    }
  }
}
```

### Store record modes

Store deliberately has one append mode plus three keyed update modes:

```text
append          every write creates a new record
replace_latest  one record per key value; matching writes replace its data
keep_highest    one record per key value; replace only when compare field is higher
keep_lowest     one record per key value; replace only when compare field is lower
```

Example high-score setup:

```json
{
  "store_record_mode": "keep_highest",
  "store_key_field": "player",
  "store_compare_field": "score"
}
```

With `player` as the record key and `score` as the comparison field, writes for the same player keep only that player's best score. Key fields must be required `text`, `integer` or `boolean` fields. Highest/lowest compare fields must be required `integer` or `float` fields.

The record mode/key/compare fields can only change while the Store is empty. This prevents existing records being silently reinterpreted.

### Store read scope and ownership

```text
store_read_scope = project   a read-capable key can see all Store records
store_read_scope = own_key   a key only sees records originally created by itself
```

For keyed modes, `store_owner_only=true` prevents one API key from replacing a matching keyed record originally created by another key. Admin record editing is still allowed.

### Store pagination

Store lists records newest-first using cursor pagination:

```text
GET /store/records?limit=100
GET /store/records?limit=100&before_id=1234
```

Responses remain a simple JSON list for student-client compatibility. Pagination metadata is returned in headers:

```text
X-ByteWyrm-Has-More: true|false
X-ByteWyrm-Next-Before-ID: <record id>
```

The Python client can continue using its normal `records(limit=...)` method; a later client revision can hide cursor-following when automatic pagination is useful.

### Store full policy

Each Project Store has one deliberately simple overflow policy:

```text
reject         return 409 when the Store reaches its record cap
delete_oldest  remove the oldest record and accept the new one
```

`delete_oldest` is useful for rolling history/telemetry-style Stores. The delete and insert occur inside the same SQLite transaction.

### Store schema

```text
GET    /admin/projects/{project_id}/store/schema
POST   /admin/projects/{project_id}/store/fields
DELETE /admin/projects/{project_id}/store/fields/{field_name}
```

Store supports four deliberately small field types:

```text
integer
float
boolean
text
```

Schema editing is locked while Store records exist. Clear the Store before changing its schema.

### Store records

```text
GET    /admin/projects/{project_id}/store/records?limit=100&before_id=<record_id>
POST   /admin/projects/{project_id}/store/records?creator_key_id=<key_id>
PUT    /admin/projects/{project_id}/store/records/{record_id}
DELETE /admin/projects/{project_id}/store/records/{record_id}
DELETE /admin/projects/{project_id}/store/records?confirm=true
```

Manual/admin-created records are still attributed to an enabled write-capable Project key so the existing audit model remains intact.

### Project API keys

```text
GET  /admin/projects/{project_id}/keys
POST /admin/projects/{project_id}/keys
POST /admin/projects/{project_id}/keys/{key_id}/revoke
POST /admin/projects/{project_id}/keys/{key_id}/enable
```

New key example:

```json
{
  "name": "Drake key",
  "client_name": "Drake",
  "permissions": "rw"
}
```

The full plaintext API key is returned only when the key is created. The database stores only its SHA-256 hash and a short display prefix.

### Usage telemetry

```text
GET /admin/projects/{project_id}/usage
```

Usage is aggregated rather than storing one request-log row per request. ByteWyrm keeps:

- all-time reads/writes/successes/rejections/rate-limit hits per API key
- one recent aggregate row per active key per minute
- current-minute R/W counts
- a five-minute requests-per-minute average
- last request/status information

Minute buckets are retained for seven days; all-time totals remain.

## Break-glass CLI

`manage.py` remains available if the dashboard is unavailable.

Preferred commands:

```bash
docker compose exec api python manage.py create-project "Asteroids"
docker compose exec api python manage.py list-projects
docker compose exec api python manage.py store-add-field prj_... player text --min-length 1 --max-length 30
docker compose exec api python manage.py store-schema prj_...
docker compose exec api python manage.py store-config prj_... --mode keep_highest --key-field player --compare-field score --owner-only
docker compose exec api python manage.py create-key prj_... "Drake key" --client-name Drake --permissions rw
docker compose exec api python manage.py list-keys prj_...
docker compose exec api python manage.py disable-project prj_...
docker compose exec api python manage.py enable-project prj_...
```

The old `*-container` and generic schema command names remain CLI aliases during the transition.

## Security model

ByteWyrm assumes student input is hostile or accidentally broken.

Current layers include:

- isolated server/network
- Cloudflare Tunnel with no public API host port
- separate LAN-only admin service/network
- project-scoped hashed API keys
- read/write permissions
- strict request-size limits
- per-key rate limits
- strict Store schemas and primitive types
- SQLite constraints/foreign keys
- Store record quotas and configurable overflow policy
- keyed Store records with database-level uniqueness
- optional creator-only keyed updates
- Project-wide or per-key Store read scope
- cursor pagination for bounded reads
- aggregated per-key usage telemetry
- non-root application containers

The aim is not to become a general backend platform. ByteWyrm should stay a set of tiny, understandable tools that remove boring server work from student game projects.
