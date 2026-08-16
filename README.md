# ByteWyrm Server

**A tiny, self-hostable backend for small Python game projects.**

ByteWyrm gives beginner programmers a simple way to store and retrieve small amounts of game data without having to build their own web server, database, authentication system, or API.

The server handles the boring backend work. Students can use the companion Python package:

```bash
pip install bytewyrm
```

```python
from bytewyrm import ByteWyrm

wyrm = ByteWyrm("bwk_YOUR_KEY_HERE")

wyrm.store.add(
    player="Drake",
    score=12500,
)

scores = wyrm.store.records(
    sort_by="score",
    reverse=True,
    limit=10,
)
```

## Links

- **Website:** https://bytewyrm.dev
- **Hosted API:** https://api.bytewyrm.dev
- **Python client:** https://github.com/William-Nitrosis/bytewyrm
- **PyPI:** https://pypi.org/project/bytewyrm/
- **Public API docs:** https://api.bytewyrm.dev/docs

> ByteWyrm is currently an alpha project. Its API and deployment model may continue to evolve.

---

## What does ByteWyrm do?

ByteWyrm is designed around **Projects**.

A Project represents a game or student project and contains its API keys, limits, and backend tools.

```text
ByteWyrm
└── Project
    ├── API keys
    ├── Limits
    └── Tools
        └── Store
```

The first tool is **Store**: a deliberately small, schema-validated datastore for game data.

Store supports four primitive field types:

```text
integer
float
boolean
text
```

That is enough for things such as:

- high scores
- lap times
- completed levels
- simple game progress
- collected items
- quiz results
- flags and status values
- small shared datasets

ByteWyrm intentionally avoids becoming a general-purpose database platform. The goal is to provide a few understandable backend primitives that work well for small teaching projects.

---

## Why ByteWyrm?

Putting game data online normally means dealing with concepts such as:

```text
HTTP requests
URLs and endpoints
JSON encoding
authentication headers
database schemas
server deployment
rate limiting
validation
```

ByteWyrm keeps those concerns on the server.

A student can instead write:

```python
wyrm.store.add(
    player="Drake",
    score=12500,
)
```

and:

```python
records = wyrm.store.records()
```

The Python client is intentionally small and beginner-friendly, while the server enforces schemas, limits, permissions, quotas, and other safety rules.

---

## Store

Store is the main ByteWyrm tool.

Each Project defines a schema describing the data it accepts. Unknown fields, invalid types, oversized text, and other malformed data are rejected before reaching storage.

### Record modes

Store can operate in four modes:

| Mode | Behaviour |
| --- | --- |
| `append` | Every write creates a new record |
| `replace_latest` | One record is kept for each key value and matching writes replace it |
| `keep_highest` | Matching keyed records are updated only when the new comparison value is higher |
| `keep_lowest` | Matching keyed records are updated only when the new comparison value is lower |

For example, a high-score Store could use:

```text
Mode:          keep_highest
Key field:     player
Compare field: score
```

Submitting:

```text
Drake → 100
Drake → 250
Drake → 180
```

leaves:

```text
Drake → 250
```

### Queries

Store supports intentionally simple server-side querying:

```text
sort_by
reverse
where
equals
greater_than
less_than
limit
```

Only one filter condition is supported at a time.

Example:

```python
scores = wyrm.store.records(
    where="completed",
    equals=True,
    sort_by="score",
    reverse=True,
    limit=10,
)
```

The server validates query fields and values against the Store schema before building the database query.

### Capacity

Each Store has a configurable record limit and one of two overflow behaviours:

```text
reject
delete_oldest
```

`reject` refuses writes once the Store is full.

`delete_oldest` turns the Store into a bounded rolling history by removing the oldest record before accepting a new one.

### Access and ownership

Projects can configure whether API keys may read:

```text
all Project records
or
only records created by that API key
```

Keyed Stores can also restrict updates so that a record may only be replaced by the API key that originally created it.

---

## API keys

Each Project can have multiple API keys.

New keys use the prefix:

```text
bwk_...
```

Keys can independently have:

- read permission
- write permission
- a friendly name
- an optional client/student nickname
- enabled/revoked state

The plaintext key is returned only when it is created.

ByteWyrm stores a SHA-256 hash of the generated key rather than the plaintext credential.

The API key identifies the Project automatically, so public clients never choose or submit a Project ID.

---

## Rate limits and quotas

ByteWyrm is intended for beginner-written programs, which means accidental code such as this should not be able to grow the database forever:

```python
while True:
    wyrm.store.add(player="Drake", score=111)
```

Projects therefore support configurable limits including:

- maximum request size
- Store record cap
- read requests per minute
- write requests per minute
- API-key permissions
- Store schema validation

Usage telemetry is also aggregated per API key so runaway clients can be spotted from the admin dashboard.

---

# Self-hosting

ByteWyrm Server is designed to run with Docker Compose.

The included stack contains:

```text
bytewyrm-api        public FastAPI service
bytewyrm-admin      private administration service
bytewyrm-homepage   static nginx website
bytewyrm-tunnel     Cloudflare Tunnel connector
```

A typical deployment looks like:

```text
                         Internet
                            │
                       Cloudflare
                            │
                 ┌──────────┴──────────┐
                 │                     │
          bytewyrm.dev         api.bytewyrm.dev
                 │                     │
                 ▼                     ▼
             homepage                 API
             (nginx)               (FastAPI)


Trusted LAN
    │
    ▼
Admin dashboard
```

The hosted ByteWyrm deployment intentionally does **not** expose the API container directly through a host port. Public traffic reaches it through Cloudflare Tunnel.

You can adapt the Compose setup to another reverse proxy or deployment environment if preferred.

---

## Requirements

For the included deployment:

- Docker
- Docker Compose
- a writable location for persistent SQLite data
- optionally, a Cloudflare Tunnel and domain for public access

Clone the repository:

```bash
git clone https://github.com/William-Nitrosis/bytewyrm-server.git
cd bytewyrm-server
```

Create your environment file:

```bash
cp .env.example .env
```

Edit `.env` and provide your own values:

```dotenv
ADMIN_BIND_HOST=192.168.50.2
ADMIN_TOKEN=replace_with_a_long_random_secret
TUNNEL_TOKEN=replace_with_your_cloudflare_tunnel_token
```

> Never commit your real `.env` file. It is ignored by the repository and should contain deployment secrets only.

The provided Compose file expects persistent database storage under:

```text
/srv/data/bytewyrm/database
```

Create the directory or change the volume path in `compose.yaml` to suit your host.

Then start ByteWyrm:

```bash
docker compose up -d --build
```

Check the services:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f
```

---

## Cloudflare Tunnel

The included Compose stack can run a remotely-managed Cloudflare Tunnel using `TUNNEL_TOKEN`.

The hosted ByteWyrm instance routes:

```text
bytewyrm.dev
    → http://homepage:80

api.bytewyrm.dev
    → http://api:8000
```

The same tunnel can publish multiple hostnames to different Docker services.

If you do not want to use Cloudflare Tunnel, replace this layer with your preferred reverse proxy or hosting setup.

---

## Admin dashboard

ByteWyrm includes a separate administration service for managing Projects.

The dashboard can:

- create, enable, disable, and delete Projects
- define Store schemas
- configure Store record behaviour
- configure Store limits and overflow policies
- configure read scope and keyed-record ownership
- create and revoke API keys
- manually inspect, create, edit, and delete Store records
- inspect per-key reads, writes, rejected requests, and recent request rates
- inspect database usage

The admin service uses a separate `ADMIN_TOKEN`. Direct LAN access can remain bound to a trusted interface. A hosted deployment may additionally publish the dashboard through Cloudflare Tunnel **only when the hostname is protected by Cloudflare Access**.

When Cloudflare Access identity support is configured, ByteWyrm validates the signed `Cf-Access-Jwt-Assertion` application token against the Access team's public keys, issuer and application audience before trusting the verified email claim. For requests arriving through Cloudflare Access, a verified enabled ByteWyrm tutor identity is now the normal admin authentication. `ADMIN_TOKEN` is retained only for direct/LAN break-glass access and direct LAN admin API calls.

Configure the admin container with:

```dotenv
CLOUDFLARE_ACCESS_TEAM_DOMAIN=https://your-team.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUD=your_application_audience_tag
```

These settings are optional for LAN-only/self-hosted deployments that do not use Cloudflare Access.

---

## Public HTTP API

Most students should use the [`bytewyrm`](https://github.com/William-Nitrosis/bytewyrm) Python package rather than calling the HTTP API directly.

The public API is nevertheless documented with OpenAPI/Swagger.

Hosted documentation:

```text
https://api.bytewyrm.dev/docs
```

Core endpoints include:

```text
GET  /
GET  /health
GET  /whoami
```

Store endpoints include:

```text
GET  /store/schema
GET  /store/records
POST /store/records
```

Authenticated endpoints use:

```http
Authorization: Bearer bwk_YOUR_KEY_HERE
```

Example raw Store write:

```http
POST /store/records
Authorization: Bearer bwk_YOUR_KEY_HERE
Content-Type: application/json

{
  "player": "Drake",
  "score": 12500
}
```

The API key determines the Project namespace automatically.

---

## Pagination

Normal Store reads use bounded cursor pagination.

Newest-first reads can use:

```text
before_id
```

Sorted queries use an opaque cursor returned by the server so tied sort values can be paginated without skipping or duplicating records.

The Python client hides most of this detail from beginner users.

---

## Usage telemetry

ByteWyrm tracks lightweight usage information for Project API keys.

Rather than writing one database log row for every HTTP request, usage is aggregated into:

- all-time per-key totals
- recent per-minute buckets

This provides useful dashboard statistics without allowing request logging itself to become an uncontrolled source of database growth.

Tracked information includes:

- reads
- writes
- successful requests
- rejected requests
- rate-limit hits
- recent request rate
- last request/status information

---

## Persistence and database upgrades

ByteWyrm currently uses SQLite.

The default Compose deployment stores the database outside the container so rebuilding or replacing application containers does not remove Project data.

Do **not** delete `server.db` when upgrading between normal ByteWyrm releases.

Schema upgrades are performed automatically when the application starts.

SQLite is configured with foreign keys, a busy timeout, and WAL mode.

---

## Break-glass CLI

A small management CLI is included for situations where the dashboard is unavailable.

Examples:

```bash
docker compose exec api python manage.py create-project "Asteroids"

docker compose exec api python manage.py list-projects

docker compose exec api python manage.py store-add-field \
    prj_... player text \
    --min-length 1 \
    --max-length 30

docker compose exec api python manage.py store-config \
    prj_... \
    --mode keep_highest \
    --key-field player \
    --compare-field score \
    --owner-only

docker compose exec api python manage.py create-key \
    prj_... \
    "Drake key" \
    --client-name Drake \
    --permissions rw
```

Run the CLI help for the full command list.

---

## Security model

ByteWyrm assumes public client input may be malformed, accidental, or hostile.

The provided deployment uses several independent layers:

- Project-scoped API keys
- hashed key storage
- separate read/write permissions
- request-size limits
- per-key rate limits
- Store record quotas
- strict primitive Store schemas
- schema-aware query validation
- bounded pagination
- optional creator-only keyed updates
- Project-wide or per-key read scope
- SQLite constraints and foreign keys
- non-root application containers
- separate public API and admin Docker networks
- optional Cloudflare Tunnel with no direct public API port
- aggregated usage telemetry

The goal is not to make ByteWyrm a general-purpose cloud database.

It is deliberately constrained so small game projects can use an online backend without exposing beginners to unnecessary backend complexity.

---

## Repository layout

```text
.
├── api/
│   ├── app/
│   │   ├── main.py
│   │   ├── admin_main.py
│   │   ├── database.py
│   │   ├── store_engine.py
│   │   ├── usage.py
│   │   └── templates/
│   ├── Dockerfile
│   └── requirements.txt
├── site/
│   ├── index.html
│   ├── styles.css
│   └── script.js
├── compose.yaml
├── .env.example
└── README.md
```

---

## Related project

### ByteWyrm Python client

Repository:

https://github.com/William-Nitrosis/bytewyrm

Install:

```bash
pip install bytewyrm
```

PyPI:

https://pypi.org/project/bytewyrm/

---

## Project status

ByteWyrm is an experimental teaching project and is currently under active development.

The current focus is keeping the system:

- small
- understandable
- difficult to misuse accidentally
- easy for students to call from Python
- easy for tutors or self-hosters to control

Future tools may be added alongside Store where they require genuinely different backend behaviour, while features that can naturally be expressed through Store remain part of Store.

---

## Multi-tutor admin rollout

The hosted admin dashboard validates human identity from Cloudflare Access and
maps the verified email to ByteWyrm's local `tutors` table. Tutor role and
Project ownership are then enforced by ByteWyrm itself.

Current authentication behaviour:

- Cloudflare Access JWTs are cryptographically validated by ByteWyrm.
- The first verified Access identity on a fresh installation is bootstrapped as
  the ByteWyrm `superadmin`.
- That bootstrap is one-shot. Once any tutor exists, later unknown Access
  identities are not automatically created and receive `403`.
- Enabled Cloudflare-authenticated tutors do not enter or share `ADMIN_TOKEN`.
- `ADMIN_TOKEN` is retained only for direct/LAN break-glass dashboard sessions
  and direct LAN admin API calls.
- Remote sign-out ends the Cloudflare Access session.

Registered tutors can be inspected without modifying them:

```bash
docker compose exec admin python manage.py list-tutors
```

Project ownership and per-tutor authorization are added separately; this step
does not yet change which Projects an authenticated admin session can access.


## Multi-tutor ownership (v0.11)

Projects are now linked to a ByteWyrm tutor owner. Existing Projects are
assigned to the bootstrapped superadmin during the schema v6 -> v7 upgrade.

Authorization rules:

- superadmins can see and manage every Project
- regular tutors can see and manage only Projects they own
- another tutor's Project returns `404 Project not found`
- direct LAN access with `ADMIN_TOKEN` remains unrestricted as a break-glass path
- a Cloudflare-authenticated identity must map to an enabled ByteWyrm tutor
- dashboard totals and traffic are scoped to a regular tutor's own Projects
- physical database storage information is only shown to superadmin/break-glass access

The shared admin token remains a second gate during this rollout. Tutor
management and tokenless normal tutor sessions come after ownership isolation
has been tested.
## Tutor management UI (v0.11.1)

Superadmins can now manage ByteWyrm tutor identities from the dashboard at:

```text
/dashboard/tutors
```

The tutor manager can:

- add an exact Cloudflare-verified email to the ByteWyrm allowlist
- set an optional display name
- grant `tutor` or `superadmin` role
- enable or disable an account
- inspect last-seen time and Project counts
- open a tutor and inspect the Projects they own

Regular tutors cannot see or access tutor-management routes. Direct-LAN
break-glass admin sessions retain superadmin-equivalent access. ByteWyrm refuses
to remove the final enabled superadmin and prevents a logged-in superadmin from
disabling or demoting their own account through the dashboard.

Email addresses are the immutable local identity key used to match a verified
Cloudflare Access identity. If a tutor's email changes, add the new identity
rather than editing the existing email.

Adding a tutor to ByteWyrm does not grant access through Cloudflare by itself;
the identity must still satisfy the Cloudflare Access policy protecting the
admin hostname.



## v0.12 admin authentication

The hosted admin dashboard now uses Cloudflare Access as the normal browser
authentication layer. ByteWyrm validates the signed Access application JWT, maps
the verified email to an enabled tutor row, and then applies tutor/superadmin
authorization. No shared ByteWyrm admin token is entered by normal tutors.

```text
Google / configured IdP
        ↓
Cloudflare Access
        ↓
verified Cf-Access-Jwt-Assertion
        ↓
ByteWyrm tutor account
        ↓
Project authorization
```

`ADMIN_TOKEN` is deliberately retained for direct trusted-LAN break-glass access.
The `/login` page is therefore only used when the request has no Cloudflare
Access identity. Remote users are redirected directly to the dashboard after
Access authenticates them.

The admin JSON API follows the same rule: a verified enabled Access tutor does
not need the bearer admin token, while direct LAN API calls still require
`Authorization: Bearer <ADMIN_TOKEN>`.

Remote dashboard sign-out redirects to the application-domain Cloudflare Access
logout endpoint. ByteWyrm also deletes any legacy `bytewyrm_admin_session` cookie
from Cloudflare-authenticated browsers so the old shared token is not retained
client-side after upgrading.
