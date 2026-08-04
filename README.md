# Rivas AI

**A multi-tenant Bale assistant platform that provisions one isolated Telegram relay service per user.**

Rivas routes Bale conversations to dedicated Telethon-backed service containers. Tenant identity, encrypted session material, routing metadata, registration requests, runtime health, and audit events are coordinated through a shared control plane.

## Engineering highlights

- Multi-tenant routing from one shared Bale bot
- Dedicated relay container per tenant
- MySQL-backed tenant registry and request logs
- Encrypted Telegram string-session storage
- Interactive OTP and 2FA onboarding
- Idempotent tenant provisioning and selective reconciliation
- Container health checks and automatic recovery
- Admin CLI and interactive registration workflow
- Activation notifications after successful provisioning
- Docker Compose deployment with local administration tools
- Proxy support for host and container network contexts

## Technology

`Python` · `Bale Bot API` · `Telethon` · `FastAPI` · `MySQL` · `Docker SDK` · `Docker Compose` · `Cryptography` · `AsyncIO`

## Architecture

```text
Bale user
   │
   ▼
Shared Rivas bot
   │
   ▼
Tenant registry in MySQL
   │
   ├── tenant identity and routing
   ├── encrypted session material
   ├── registration state
   └── request and audit events
   │
   ▼
Dedicated relay container
   │
   ▼
Dedicated Telethon user session
   │
   ▼
Configured Telegram AI service
```

Each active tenant receives an isolated relay process and Telegram session. The shared bot resolves the Bale user to the correct tenant endpoint before forwarding a request.

## Core components

| Component | Responsibility |
| --- | --- |
| `rivas-bot` | Bale UX, registration flow, request routing, and user notifications |
| `mira-telegram-service` | Per-tenant Telethon relay API |
| `rivas-admin` | Tenant creation, session onboarding, enable/disable, and reconciliation |
| MySQL | Tenant registry, mappings, requests, registration state, and audit records |
| Auto-heal service | Restarts unhealthy managed containers |
| phpMyAdmin | Local database administration |

## Quick start

```bash
cp .env.example .env
docker compose up -d --build
```

The default stack starts:

- MySQL
- phpMyAdmin on a local-only binding
- the shared Rivas bot
- the container recovery service
- an optional standalone relay profile

Run the standalone relay when required:

```bash
docker compose --profile standalone up -d --build
```

## Tenant onboarding

Add a tenant through the admin CLI:

```bash
rivas-admin tenant-add \
  --tenant-slug user001 \
  --owner-name "User 001" \
  --phone "+98912XXXXXXX" \
  --bale-user-id "123456" \
  --bale-chat-id "123456"
```

During interactive onboarding, the operator can complete Telegram OTP and 2FA. The resulting string session is encrypted before storage and used only by the assigned tenant runtime.

Existing sessions can be supplied through environment input rather than command history:

```bash
export TG_STRING_SESSION='...'
scripts/onboard_tenant.sh user001 "User 001" +98912XXXXXXX 123456 123456
```

Clear the shell value after provisioning.

## Declarative tenant synchronization

A local, ignored `users.json` file can describe the desired tenant set:

```bash
cp users.example.json users.json
PYTHONPATH=src .venv/bin/python -m rivas.load_users \
  --config users.json \
  --write-back
```

Synchronization behavior:

- creates newly defined tenants
- updates only changed tenants
- leaves unchanged containers running
- disables and removes runtime containers for removed tenants
- sends activation notifications after successful provisioning

Tenant files may contain sensitive phone and session data and must not be committed.

## Interactive registration workflow

Users without an active tenant follow a staged registration flow in Bale. Operators can review pending requests with:

```bash
scripts/registration_admin.sh
```

The tool supports selecting one request, collecting required information, completing OTP/2FA, provisioning the container, updating the database, and notifying the user.

## Runtime reliability

- service and tenant health checks
- `restart: unless-stopped`
- automatic restart for unhealthy containers
- selective reconciliation instead of full-stack restarts
- configurable request and hard relay timeouts
- explicit tenant enable/disable state
- runtime scripts for recovery and reconciliation

A duplicated or invalid Telegram authorization key requires generating a new session and reprovisioning only the affected tenant.

## Configuration

The root `.env` controls:

- Bale bot credentials
- Telegram API credentials
- encryption master key
- MySQL connectivity
- tenant image and Docker network
- relay timeouts and payload limits
- host/container proxy differences
- activation notifications
- audit-channel settings
- auto-heal timing

Keep the master encryption key stable and backed up securely. Losing it can make encrypted tenant sessions unrecoverable.

## Security model

- One Telegram session is isolated per tenant container.
- String sessions are encrypted before database storage.
- Runtime secrets are injected through environment variables.
- phpMyAdmin should remain local or access-controlled.
- Tenant ownership must be checked before routing or administrative changes.
- Phone numbers, OTP data, sessions, prompts, and responses are sensitive.
- Docker-socket access is highly privileged and should be limited to the provisioning component.
- All platform use must comply with Telegram, Bale, and upstream-service rules.

## Verification

```bash
docker compose config --quiet
curl -fsS http://127.0.0.1:8088/ready
```

Also verify:

- database migrations and tenant mappings
- encrypted-session round trips
- selective create/update/disable reconciliation
- container health and recovery
- end-to-end routing with controlled test users
- activation notifications

## Project status

Rivas AI demonstrates multi-tenant control-plane design, per-user runtime isolation, encrypted credential handling, container orchestration, interactive onboarding, health recovery, and cross-platform message routing.