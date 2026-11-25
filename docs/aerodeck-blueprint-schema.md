# Aerodeck Cloud Application Blueprint Schema (`aerodeck.yaml` / `aerodeck.json`)

The operator Docs page publishes this schema at `/docs` and
`/docs/aerodeck-blueprint-schema.md` so an agent can generate `aerodeck.yaml`
from a sample repository.

Aerodeck Cloud supports a declarative **Application Blueprint Schema (v1)** so any GitHub repository can declare its build strategy, exposed port, required runtime environment variables, secrets, and attached database addons (`postgres`, `redis`, `mongodb`).

When an operator enters a GitHub URL on **`/deploy`**, Aerodeck Cloud inspects the repository (`POST /api/v1/deployments/inspect`) in the following priority order:

1. **`aerodeck.yaml` / `aerodeck.yml` / `aerodeck.json`** at the repository root (authoritative blueprint).
2. **Automatic Fallback Synthesis** from `docker-compose.yaml` / `compose.yaml`, `Dockerfile`, `.env.example` / `.env.sample`, and `mock-secrets/` when no `aerodeck.yaml` is present.

---

## Agent instructions

Given a sample application repository, write `aerodeck.yaml` at the repository root that validates against this schema (version `1`).

Rules for the agent:

- Prefer `build.strategy: dockerfile` when a root `Dockerfile` exists.
- Prefer `build.strategy: compose` when the app is defined by `docker-compose.yaml` / `compose.yaml` and no root Dockerfile is intended.
- Prefer `build.strategy: auto` when a project file is enough (`package.json` with `scripts.start`, `requirements.txt` / `pyproject.toml`, `go.mod`, `index.html`, and so on).
- Set `build.port` to the port the process listens on inside the container.
- Only declare `env` entries the app needs. Mark secrets with `secret: true`. Use `generate: hex32` only for signing keys or similar random secrets.
- Only add `postgres`, `redis`, or `mongodb` addons the app actually uses.
- Do not invent cloud providers other than what this schema describes.
- Output a single `aerodeck.yaml` file and nothing else unless asked.

---

## 1. Full `aerodeck.yaml` Example

```yaml
version: 1
name: dev-portal

build:
  # "dockerfile" | "compose" | "auto"
  strategy: dockerfile
  dockerfile: Dockerfile
  compose_file: docker-compose.yaml
  service: portal
  port: 8000

addons:
  # Managed containerized database addons attached to the app's private network
  - type: postgres
    env_var: DATABASE_URL
    version: "16"
  - type: redis
    env_var: REDIS_URL
    version: "7"

env:
  - key: KEYCLOAK_CLIENT_SECRET
    description: "Keycloak confidential OIDC client secret"
    required: true
    secret: true
  - key: GITLAB_TOKEN
    description: "GitLab API token used to trigger tenant pipelines"
    required: true
    secret: true
  - key: SESSION_SECRET_KEY
    description: "32+ character secret key used to sign session cookies"
    required: true
    secret: true
    generate: hex32
  - key: KEYCLOAK_REALM
    description: "Keycloak realm identifier"
    required: false
    secret: false
    default: "dev-portal"
```

---

## 2. Schema Reference

### Root Fields
| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `version` | `1` | Yes | Schema version (`1`). |
| `name` | `string` | No | Human-readable application or service name. Defaults to the GitHub repository name. |
| `build` | `BlueprintBuild` | No | Build strategy, Dockerfile/Compose path, and listening TCP port. |
| `addons` | `BlueprintAddon[]` | No | Database addons (`postgres`, `redis`, `mongodb`) to provision and attach. |
| `env` | `BlueprintEnvVar[]` | No | Runtime environment variables and secrets prompted on `/deploy`. |

### `build` Object (`BlueprintBuild`)
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `strategy` | `"dockerfile" \| "compose" \| "auto"` | `"dockerfile"` | Whether to build a root `Dockerfile`, a `docker-compose.yaml` service, or synthesize a zero-config `Dockerfile` (Node.js, Python, Go, Static HTML). |
| `dockerfile` | `string` | `"Dockerfile"` | Relative path to the `Dockerfile`. |
| `compose_file` | `string \| null` | `null` | Relative path to `docker-compose.yaml` when `strategy: compose`. |
| `service` | `string \| null` | `null` | Primary web service name in `docker-compose.yaml`. |
| `port` | `integer` (`1–65535`) | `8000` | Internal container TCP port receiving HTTP traffic from Traefik. |
| `runtime` | `"node" \| "python" \| "go" \| "java" \| "rust" \| "static" \| null` | `null` | Runtime chosen from a root project file when `strategy` is `auto`. Authors may omit it. A root `Dockerfile` does not set it. |
| `marker` | `string \| null` | `null` | Root file that chose `runtime`. A `package.json` with `scripts.start` sets `runtime` to `node` and `marker` to `package.json`. `pnpm-lock.yaml` or `yarn.lock` beside it selects that package manager; otherwise npm is used. |

### `addons[]` Items (`BlueprintAddon`)
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `type` | `"postgres" \| "redis" \| "mongodb"` | *Required* | Database engine to provision on the tenant's isolated Docker network. |
| `env_var` | `string` | `"DATABASE_URL"`, `"REDIS_URL"`, or `"MONGODB_URI"` | Environment variable name injected into the application container. |
| `version` | `string` | `"16"` (Postgres) / `"7"` (Redis & MongoDB) | Container image major version tag (`postgres:<version>-alpine`, `redis:<version>-alpine`, `mongo:<version>`). |

### `env[]` Items (`BlueprintEnvVar`)
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `key` | `string` (`^[A-Za-z_][A-Za-z0-9_]{0,127}$`) | *Required* | Environment variable name. |
| `description` | `string` | `""` | Help text displayed below the input field on `/deploy`. |
| `required` | `boolean` | `true` | When `true`, `/deploy` requires a value before queuing the deployment. |
| `secret` | `boolean` | `false` | Masks the input field (`type="password"`) in the `/deploy` form. |
| `default` | `string \| null` | `null` | Pre-filled default value in the `/deploy` form. |
| `generate` | `"hex32" \| null` | `null` | Enables a 1-click **Generate** button in `/deploy` for signing keys and tokens. |

---

## 3. Automatic Platform Variables Injected by Aerodeck Cloud

On every deployment, Aerodeck Cloud automatically injects:

- `PORT`: The detected or declared `build.port`.
- `HOST`: `0.0.0.0`.
- `AERODECK_PUBLIC_URL`, `PUBLIC_URL`, `PORTAL_PUBLIC_URL`: `https://<project>-<env>.dev.aerodeckcloud.com`.
- `DATABASE_URL` / `REDIS_URL` / `MONGODB_URI`: Automatically populated whenever the matching addon is enabled.
