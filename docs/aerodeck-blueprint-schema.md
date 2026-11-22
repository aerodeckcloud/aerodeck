# AeroDeck Application Blueprint Schema (`aerodeck.yaml` / `aerodeck.json`)

AeroDeck supports a declarative **Application Blueprint Schema (v1)** so any GitHub repository can declare its build strategy, exposed port, required runtime environment variables, secrets, and attached database addons (`postgres`, `redis`).

When an operator enters a GitHub URL on **`/deploy`**, AeroDeck inspects the repository (`POST /api/v1/deployments/inspect`) in the following priority order:

1. **`aerodeck.yaml` / `aerodeck.yml` / `aerodeck.json`** at the repository root (authoritative blueprint).
2. **Automatic Fallback Synthesis** from `docker-compose.yaml` / `compose.yaml`, `Dockerfile`, `.env.example` / `.env.sample`, and `mock-secrets/` when no `aerodeck.yaml` is present.

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
| `addons` | `BlueprintAddon[]` | No | Database addons (`postgres`, `redis`) to provision and attach. |
| `env` | `BlueprintEnvVar[]` | No | Runtime environment variables and secrets prompted on `/deploy`. |

### `build` Object (`BlueprintBuild`)
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `strategy` | `"dockerfile" \| "compose" \| "auto"` | `"dockerfile"` | Whether to build a root `Dockerfile`, a `docker-compose.yaml` service, or synthesize a zero-config `Dockerfile` (Node.js, Python, Go, Static HTML). |
| `dockerfile` | `string` | `"Dockerfile"` | Relative path to the `Dockerfile`. |
| `compose_file` | `string \| null` | `null` | Relative path to `docker-compose.yaml` when `strategy: compose`. |
| `service` | `string \| null` | `null` | Primary web service name in `docker-compose.yaml`. |
| `port` | `integer` (`1–65535`) | `8000` | Internal container TCP port receiving HTTP traffic from Traefik. |

### `addons[]` Items (`BlueprintAddon`)
| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `type` | `"postgres" \| "redis"` | *Required* | Database engine to provision on the tenant's isolated Docker network. |
| `env_var` | `string` | `"DATABASE_URL"` or `"REDIS_URL"` | Environment variable name injected into the application container. |
| `version` | `string` | `"16"` (Postgres) / `"7"` (Redis) | Container image major version tag (`postgres:<version>-alpine`, `redis:<version>-alpine`). |

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

## 3. Automatic Platform Variables Injected by AeroDeck

On every deployment, AeroDeck automatically injects:
- `PORT`: The detected or declared `build.port`.
- `HOST`: `0.0.0.0`.
- `AERODECK_PUBLIC_URL`, `PUBLIC_URL`, `PORTAL_PUBLIC_URL`: `https://<project>-<env>.dev.app.aero-deck.app`.
- `DATABASE_URL` / `REDIS_URL`: Automatically populated whenever the `postgres` or `redis` addon is enabled.
