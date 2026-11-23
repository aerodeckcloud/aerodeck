# Operator console

The Next.js app in `apps/dashboard`. The browser keeps the `aerodeck_session`
cookie. The server calls the control plane with an enrolled client. The
browser never sees that client secret or the control-plane bearer.

Overview and Projects read `GET /api/v1/projects`. Project detail uses
`GET /api/v1/projects/{id}` plus that project's environments and routes through
`GET /api/console/projects/{id}` (routes from `GET /api/v1/projects/{id}/routes`;
seed detail with empty env and route lists when the console client is unset).
The Routes section shows hostname and enabled state only; when the console
client is set, operators can also declare a route (`POST /api/console/projects/{id}/routes`
→ `POST /api/v1/projects/{id}/routes`), inspect DNS target/CNAME guidance
(`GET /api/console/projects/{id}/routes/{routeId}/dns` → `GET /api/v1/projects/{id}/routes/{routeId}/dns`),
and view or issue the route's TLS certificate (`GET /api/console/projects/{id}/routes/{routeId}/certificate`
and `POST /api/console/projects/{id}/routes/{routeId}/certificate/issue`, returning only
`route_id`, `hostname`, `status`, and `not_after` — never PEM or private key material).
Without the client, route/DNS/certificate endpoints return 404 and mock routes never call DigitalOcean DNS or ACME. Creating a project uses `POST /api/v1/projects`
through `POST /api/console/projects` when the console client is set; the
Projects form is hidden and create returns 404 when it is not, so the seed
list stays unchanged. Creating an environment uses
`POST /api/v1/projects/{id}/environments` through
`POST /api/console/environments` when the console client is set (slug, name,
kind, DigitalOcean region); create returns 404 when the client is unset.
Deployment environment labels link to `/projects/{id}#environments`. Project detail
can PATCH/DELETE the project (`/api/console/projects/{id}`) and its environments
(`/api/console/projects/{id}/environments/{environmentId}`) when the console client
is set; deletes clear deployment links on the control plane. Mock stays read-only.
Sandboxes read `GET /api/v1/projects/{id}/sandboxes` through
`GET /api/console/sandboxes` (grouped by project on `/sandboxes` with each
guest's UTC `expires_at` timestamp and an empty-state CTA to boot when
enrolled) and, when the console client is set, boot new
microVMs through `POST /api/console/sandboxes` (→
`POST /api/v1/projects/{id}/sandboxes`, returning `id`, `project_id`, `image`,
`vcpu`, `memory_mib`, `status`, `expires_at`, and `created_at`, never `host_id`
or `hypervisor_vm_id`). When no Firecracker host is enrolled, `503`
`sandbox_unavailable` surfaces a clear operator message; without the client,
sandbox create returns 404 and the form stays hidden. Creating a deployment uses
`POST /api/v1/deployments` through `POST /api/console/deployments` when the
console client is set, and stays on `/api/mock/v1/deployments` when it is not
([dashboard-mock.md](dashboard-mock.md)). With the console client, Deploy also
lists environments via `GET /api/console/environments` and, on submit, creates
the deployment with that `project_id`/`environment_id` (so list/detail keep the
link after a restart) then queues `POST .../environments/{id}/provision` with
the new `deployment_id` so the terminal can follow worker lines. A successful
provision enqueue also stores `provision_job_id` on the deployment so list/detail
can reload that Celery job later; older rows and enqueue failures leave it null.
A deployment also stores who started it (`trigger` of `manual`, `webhook`, `rollback`,
or `promote`, plus `actor_user_id` when that header names a console user) and,
after the worker checks out a commit, `git_sha`, `git_ref`, and `finished_at`.
Project deployment history shows the short SHA, the trigger, and the duration.
Rows created before that migration leave those fields empty.
**Redeploy** repeats that recorded commit (`POST /api/v1/deployments/{id}/redeploy`
passes `git_ref` set to `git_sha`). A row with no commit returns 422 and does not
enqueue. **Deploy latest** (`POST .../deploy-latest`) enqueues with no pin, so the
worker clones the repository default branch. **Promote**
(`POST /api/v1/deployments/{id}/promote` with `environment_id`, console
`POST /api/console/deployments/{id}/promote`) runs that succeeded row's retained
local image in another environment of the same project. Promote needs
`AERODECK_PROVISIONER_BACKEND=docker`; terraform and noop return 422. The new
row gets `trigger=promote`, copies `git_sha`, and the worker tags the image into
the target without cloning. Production needs an owner; other kinds need a
developer. A missing image, a non-succeeded row, the same environment, or
another project returns 422. The target's wait/override setting still applies.
Project history asks for the target environment; the browser does not call
FastAPI. Redeploy, deploy-latest, rollback, and promote stay on the console
routes; the mock deployment list never calls the control plane.
**Rollback** (`POST /api/v1/deployments/{id}/rollback`, console
`POST /api/console/deployments/{id}/rollback`) retags the retained image and,
by default, restores the encrypted variable snapshot captured when that row
first succeeded. The confirm dialog lists variable names that would change
and never values. The read model exposes `snapshot_variable_names` (names
only) and, on the rollback response, `variables_restored`. Rows that
succeeded before this story have no snapshot: the image still rolls back and
`variables_restored` is false so current variables stay as they are. Pass
`restore_variables: false` to skip the restore when a snapshot exists.
Each environment keeps `rollback_images_to_keep` succeeded images (default 5,
from 1 to 20). When a deployment is marked succeeded, the worker removes older
local tags. The current image is never removed, and a prune failure is logged
without marking that deployment failed. The project page shows how many
rollback images are still present (`retained_rollback_images`). Rollback or
promote of a pruned image returns 422 `image_not_retained`. These tags stay on
the Docker host; they are not pushed to a registry.
An environment can set `release_command` (at most 500 characters): one
executable and its arguments, not a shell script. After the new container is
healthy and before Traefik moves traffic, the worker runs that command inside
the container with `docker exec` and the deployment's environment variables.
The platform does not start a shell and does not print variable values.
stdout and stderr go to the deployment log. The timeout is
`AERODECK_RELEASE_COMMAND_TIMEOUT_SECONDS` (default 300, maximum 900). A
non-zero exit or a timeout fails the deployment, removes the new container,
and leaves the previous container serving. An empty command skips this step.
Rolling an image back does not undo schema changes or migrations that command
applied.

An owner can pause automatic redeploys on an environment (`auto_deploy_paused`).
A GitHub push then creates no deployment and writes one audit row,
`deployment.auto_deploy_skipped`. The webhook secret is not in that row. A
repeat of the same delivery is ignored. Manual deploy, repeat, deploy-latest,
and promote still run while pause is on. Only an owner can change
`auto_deploy_paused` or `prod_deploy_approval`, and a request with no user is
refused. The console sends the signed-in user on that update. Someone who is
not an owner sees a forbidden message instead of a control-plane failure. A create request cannot turn approval off or start paused: a new
environment always starts with automatic deploys on, and a new production
environment always starts with approval on. Changing an environment into or out of
production also requires an owner. Moving an environment to production turns
approval on unless that owner set the flag in the same request.
Production environments can require an owner (`prod_deploy_approval`). Existing
production environments stay off, so current webhooks keep deploying until an
owner turns it on. A new production environment starts with it on. Provision uses the caller, not the user stored on the deployment. An owner-created row still waits when a developer, or a request with no user, starts it. The console sends the signed-in user on that provision call. A developer, a webhook, or a rollback then stores the row as `queued` with
`approval_required_at` set and does not start a job. An owner deploys,
promotes, and rolls back immediately. Approve and reject require the calling
user id. A request with no user is refused.
`POST /api/v1/deployments/{id}/approve` and `POST .../reject` are owner-only
(console `POST /api/console/deployments/{id}/approve` and `.../reject`).
The console returns forbidden when the caller is not an owner, and conflict when
the row is no longer waiting. Approve starts the job under the overlap setting and the audit row records the
approving user. Reject marks the row `cancelled`. A project with no members
has nobody to approve, so those deploys still start.

`POST /api/v1/deployments/{id}/cancel` (console `POST /api/console/deployments/{id}/cancel`)
marks a queued or running deployment `cancelled` and sets `finished_at` when the
worker has not yet set `swap_started_at`. The Celery task is revoked if it has
not finished; a revoke error is logged and the row stays `cancelled`, so a later
job leaves the live container alone. After `swap_started_at` is set the API
returns 409 and the swap continues. Succeeded, failed, and cancelled rows return
422. Deployment detail shows Cancel only for queued and running control-plane
rows.
Each environment row can set what happens when a deploy is already queued or
running. **Wait for the current deploy** (the default) keeps the new row
`queued` with `held_for_deployment_id` and does not start a job. Older waiting
rows are cancelled so only the newest starts after the active deploy finishes.
**Replace the current deploy** cancels the active row when `swap_started_at`
is still empty, and returns 409 once the swap has started. Push webhooks use
the same setting. The browser PATCHes
`/api/console/projects/{id}/environments/{environmentId}` and does not call
FastAPI.
A deploy counts as active from the moment the queue accepts its job
(`dispatched_at`) until it ends. Rows from before this setting existed, and
rows whose provision never ran, have no `dispatched_at` and never hold a new
deploy. A deploy that was already running when the control plane was upgraded
is not counted either, so one overlap is possible right after the upgrade.
The worker sets `finished_at` when the provision ends and then starts the
waiting row, so a deploy nobody watches in the console still lets the next one
start. The console outcome routes (`succeeded`, `failed`, `cancel`) start it
too; whichever comes second finds nothing left to start. A waiting row that
cannot be started (the queue refuses the job, for example) is marked `failed`.
A waiting row keeps what it needs to start later in `provision_replay_json`:
the git ref, the rollback source, the addon names, and any one-off variables
sent with the deploy. The variables are stored only as a token encrypted with
the same key as saved environment variables. The payload is removed when the
row starts, is cancelled, or fails.
A waiting row has no outcome yet. Its log stream shows `Waiting for the current
deployment to finish...` in place of the stub, and `succeeded` and `failed`
return 409 until it starts. Deployment detail shows a Waiting badge, re-reads
the row every few seconds, and reports an outcome only once it has started.
Without a queue (`AERODECK_REDIS_URL` unset) no route starts a waiting row; it
stays `queued` until it is cancelled.
When the console client is set and a deployment has `provision_job_id`, detail
shows a Provision job panel that polls `GET /api/console/jobs/{id}` (→
`GET /api/v1/jobs/{id}`) about every 5s until succeeded/failed, a poll
ceiling (~3 min), consecutive fetch failures, or forever-`queued` stale stop
(control plane maps unknown/expired jobs to `queued`). Already-terminal
deployments (including live Terminal Done/fail) fetch once and stop; if the
job is still active they show a Stale badge and an expiry note instead of a
spinner. Mock detail hides the panel (no jobs API). On a control-plane project
detail, each environment row has **Reconcile**, which POSTs
`/api/console/projects/{id}/environments/{environmentId}/reconcile`
(→ `POST .../reconcile` on the control plane) and shows the accepted
`job_id`. Queue down surfaces a generic unavailable message with no
success state. Without the client, reconcile returns 404. The browser
never calls FastAPI.
An empty Deploy select links operators to Projects to create an environment. Mock
create stays unlinked. If provision cannot be queued after create,
the form stays put, disables Deploy, and shows a generic message so a retry
does not create another orphan row; enqueue also marks that row `failed`. The
JSON body still returns `deployment_id` for typed client handling and server
logs, but the UI must not navigate to the terminal (opening empty logs would
stub `Done`). Without the client, Deploy does not call provision. The list and
the deployment page follow the same mock/control-plane split. Deploy uses a
native `<select>` for environments until the dashboard UI kit gains a shadcn
`Select`. The terminal reads
`GET /api/v1/deployments/{id}/logs` through
`GET /api/console/deployments/{id}/logs` when the console client is set, and
stays on `/api/mock/v1/deployments/{id}/logs` when it is not. An unknown
deployment id does not stream. When Redis is set and `aerodeck:deployment-logs:{id}` already has lines, the
control-plane stream follows until `Done`, or until
`AERODECK_DEPLOYMENT_LOG_FOLLOW_IDLE_SECONDS` (default 1800) elapses with no
new line (it does not invent `Done`). Publishes do not left-trim the list (SSE
cursor stays stable; TTL expires the key) and refuse past `MAX_LINES`. If the
list is empty at open, it polls for up to
`AERODECK_DEPLOYMENT_LOG_FIRST_PUBLISH_GRACE_SECONDS` (default 2) for the first
line, then emits the three-line stub so the Terminal still reaches `Done`.
Without Redis, the in-memory store is a snapshot of lines present at open, or
the stub when empty. A provision job may publish those lines when queued with
`{"deployment_id":"..."}` ([provision-environments.md](provision-environments.md)).
Enqueue seeds a queued line first so the stub cannot emit `Done` before the
worker. Success ends with `Done`; final provision failure publishes a failure
line without `Done` and sets status `failed`. When the stream emits `Done`, the
dashboard server records `succeeded` with `POST /api/v1/deployments/{id}/succeeded`.
A failure line (`Provision queue failed` or `Provision failed:…`, matching the
control-plane constants) triggers `POST /api/v1/deployments/{id}/failed`
(idempotent if the worker already wrote it). A down Redis broker returns `503`
before the SSE body starts.
The mock log route still marks its own row succeeded. Sign-in still
posts to `/api/mock/v1/auth/login`. When the console client is set, that
route checks `POST /api/v1/console-users/login` and sets `aerodeck_session`
from the stored user and opens `POST /api/v1/console-sessions`. The cookie
holds that token. A later read uses `POST /api/v1/console-sessions/resolve`,
so a dashboard restart keeps the operator signed in. Sign-out of a control-plane session posts
`POST /api/v1/console-sessions/logout` and clears the cookie only after that
revoke succeeds. A mock-store cookie (mock Google) clears locally and does
not call revoke. A failed revoke leaves the cookie in place. A resolve
failure other than a missing session returns 502 and leaves the operator on
the console. A wrong password
and an unknown email return the same error. Without the client, sign-in and
the session stay in the mock store. A real Google callback
links that verified email with `POST /api/v1/console-users/google` when the
console client is also set. The mock Google profile stays in the dashboard
process, including when the console client is set. That cookie is read from
the mock store first so a restart of Next.js still drops it. Registration uses `POST /api/v1/console-users` through
`POST /api/console/register` when the console client is set, and stays on
`/api/mock/v1/auth/register` when it is not. When `CONTROL_PLANE_URL` is
unset, projects and sandboxes fall back to the local seed.

Do not `terraform apply` from a laptop. Port 3000 stays VPC-only.

## 1. Enroll a client

On `aerodeck-dev-control`, run
`apps/control-plane/scripts/enroll-console-client.sh`. It generates a secret,
stores only the SHA-256 digest in `/etc/aerodeck/control-plane.env`, writes
the secret to `/etc/aerodeck/dashboard.env`, and restarts the API. Running it
again rotates that secret and keeps `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` when those lines are already in `dashboard.env`. It
replaces the whole `AERODECK_AUTH_CLIENTS` map with the `console` client. The
start script keeps the other keys already in the control-plane env file.
Google setup is [google-sign-in.md](google-sign-in.md).

Extract `enroll-console-client.sh`, `dashboard_env.py`,
`start-dashboard-on-droplet.sh`, and `publish-console-route.py` from the
control-plane image (or copy them from the repo if not yet extracted in
[startup-control-plane.md](startup-control-plane.md)):

```bash
IMAGE=$(tr -d ' \t\r\n' < /etc/aerodeck/control-plane-image)
mkdir -p /root/aerodeck
docker run --rm --entrypoint cat "$IMAGE" /app/scripts/enroll-console-client.sh > /root/enroll-console-client.sh
docker run --rm --entrypoint cat "$IMAGE" /app/scripts/dashboard_env.py > /root/dashboard_env.py
docker run --rm --entrypoint cat "$IMAGE" /app/scripts/start-dashboard-on-droplet.sh > /root/start-dashboard-on-droplet.sh
docker run --rm --entrypoint cat "$IMAGE" /app/scripts/publish-console-route.py > /root/aerodeck/publish-console-route.py
chmod +x /root/enroll-console-client.sh /root/start-dashboard-on-droplet.sh
sudo bash /root/enroll-console-client.sh
```

Do not commit `/etc/aerodeck/dashboard.env`. Do not paste the secret into chat.
If `aerodeck-dashboard` is already running, the script recreates it so it
picks up the new secret.

## 2. Open port 3000 to the VPC

The control firewall allows the VPC to reach port 8000 and port 3000. Apply
that rule from GitHub Actions: **IaC plan and apply**, branch `main`,
environment **dev**, Apply checked. The plan adds the port 3000 rule. It must
not replace the control Droplet or Postgres.

Until that apply finishes, Traefik cannot open the console.

## 3. Build and run the dashboard

### Option A: Pull from DigitalOcean Container Registry (`start-dashboard-on-droplet.sh`)

Every merge to `main` touching `apps/dashboard/**` builds `apps/dashboard/Dockerfile`
in GitHub Actions (`.github/workflows/dashboard-image.yml`), runs
`apps/control-plane/scripts/prune_docr_images.py` (`--keep 2` so the 1-repository /
5-image Starter registry never fills up), and pushes
`registry.digitalocean.com/aerodeck/control-plane:dashboard-main` (and
`:dashboard-<sha>`).

On `aerodeck-dev-control`, pull the latest image from DOCR, recreate
`aerodeck-dashboard`, prune dangling layers, and wait for `:3000/login` → `200`
with one command:

```bash
sudo bash /root/start-dashboard-on-droplet.sh
```

(`start-dashboard-on-droplet.sh` reads `DOCR_TOKEN`, `DIGITALOCEAN_TOKEN`, or
`AERODECK_DIGITALOCEAN_TOKEN` from `/etc/aerodeck/control-plane.env` to log in
to `registry.digitalocean.com`, pulls
`registry.digitalocean.com/aerodeck/control-plane:dashboard-main` or
`/etc/aerodeck/dashboard-image`, tags it `aerodeck/dashboard:dev`, and
restarts `aerodeck-dashboard`.)

### Option B: Automatic deploy from GitHub Actions via a local self-hosted runner

Because `aerodeck-dev-control` restricts SSH port `22` to your local machine's
`/32` IP (`operator_ssh_cidrs`), registering your local machine as a
self-hosted runner lets `.github/workflows/dashboard-image.yml` automatically
SSH into the Droplet and run `/root/start-dashboard-on-droplet.sh` after every
`main` image push:

1. From Git Bash on your local machine (requires `gh` CLI authenticated), run
   `apps/control-plane/scripts/setup-local-runner.sh` with the control Droplet
   public IPv4 to set `CONTROL_DROPLET_HOST`, set
   `DEPLOY_DASHBOARD_SELF_HOSTED=true`, download the GitHub Actions runner into
   `~/actions-runner-aerodeck`, and register it with `--labels self-hosted`:
   ```bash
   bash apps/control-plane/scripts/setup-local-runner.sh 188.166.67.201
   cd ~/actions-runner-aerodeck && ./run.cmd
   ```
2. Or configure manually in GitHub (**Settings → Actions → Runners → New
   self-hosted runner**) and **Settings → Secrets and variables → Actions →
   Variables**:
   - `DEPLOY_DASHBOARD_SELF_HOSTED` = `true`
   - `CONTROL_DROPLET_HOST` = `<control-public-ipv4>` (for example `188.166.67.201`)
   - *(Optional)* `CONTROL_SSH_KEY_PATH` if your key is not at `~/.ssh/aerodeck_ed25519`.
3. When `DEPLOY_DASHBOARD_SELF_HOSTED` is unset or `false`, the `deploy` job
   skips cleanly so CI never waits when your laptop is offline.

### Option C: Build directly on the Droplet with a swapfile

If building directly from git on the `s-1vcpu-1gb` Droplet without DOCR, enable
a 2 GB swapfile once so `next build` does not hit the 1 GB RAM limit:

```bash
if ! swapon --show | grep -q '/swapfile'; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

if [ ! -d /root/aerodeck-repo/.git ]; then
  git clone https://github.com/mangatohjunior/aerodeck.git /root/aerodeck-repo
fi
cd /root/aerodeck-repo
git fetch origin
git checkout -f origin/main
docker build -t aerodeck/dashboard:dev /root/aerodeck-repo/apps/dashboard

docker rm -f aerodeck-dashboard
docker run -d --name aerodeck-dashboard --network host --restart unless-stopped \
  --env-file /etc/aerodeck/dashboard.env \
  -e HOSTNAME=0.0.0.0 \
  aerodeck/dashboard:dev
for i in {1..15}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/login || true)
  if [ "$code" = "200" ]; then echo "200"; break; fi
  sleep 1
done
```

### Option D: Build locally and copy a tar

If building locally from Git Bash in `apps/dashboard`, copy a tar file (do not pipe `docker save` into `ssh` on Windows; Git Bash corrupts the stream and `docker load` reports `unrecognized image format`):

```bash
cd /c/Users/bonito/Desktop/aerodeck/aerodeck/apps/dashboard
docker build -t aerodeck/dashboard:dev .
docker save -o dashboard.tar aerodeck/dashboard:dev
scp -i ~/.ssh/aerodeck_ed25519 dashboard.tar root@<control-public-ipv4>:/tmp/dashboard.tar
ssh -i ~/.ssh/aerodeck_ed25519 root@<control-public-ipv4> "docker load -i /tmp/dashboard.tar && rm /tmp/dashboard.tar"
```

On the Droplet (wait a few seconds for Next.js to bind port 3000 before probing `/login`):

```bash
docker rm -f aerodeck-dashboard
docker run -d --name aerodeck-dashboard --network host --restart unless-stopped \
  --env-file /etc/aerodeck/dashboard.env \
  -e HOSTNAME=0.0.0.0 \
  aerodeck/dashboard:dev
for i in {1..15}; do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3000/login || true)
  if [ "$code" = "200" ]; then echo "200"; break; fi
  sleep 1
done
```

`200` means the console is listening. Port 3000 does not answer from your
laptop. `--network host` is what lets the server reach the API on
`127.0.0.1:8000`.

## 4. Publish dev.app.aero-deck.app

Before running `publish-console-route.py`, ensure `/etc/aerodeck/control-plane.env`
has the DigitalOcean DNS and Let's Encrypt settings configured ([edge-routing.md](edge-routing.md)),
otherwise the route stays in memory and issues a self-signed `AeroDeck Fake CA` certificate.

Operator scripts live in `/root/aerodeck` on the control Droplet.
`start-dashboard-on-droplet.sh` automatically copies and invokes
`apps/control-plane/scripts/publish-console-route.py` during deployment once
`:3000/login` responds 200. It creates the console project, the dev environment,
and the route to the control private IPv4 (`10.41.0.2`, or `AERODECK_CONTROL_PRIVATE_IP`)
port 3000, then issues or renews the certificate. If the route was previously created with an older
`backend_host` or a fake/staging certificate (`AeroDeck Fake CA` or staging),
re-running the script updates `backend_host` and automatically calls
`/certificate/renew` (or pass `--renew` to force renewal). The client secret
stays in `/etc/aerodeck/dashboard.env`.

```bash
mkdir -p /root/aerodeck
python3 /root/aerodeck/publish-console-route.py
```

That publishes a CNAME `dev.app` to `edge.aero-deck.app`. Open
`https://dev.app.aero-deck.app`. `dev` uses a trusted certificate only when
`AERODECK_ACME_DIRECTORY_URL` is the Let's Encrypt production directory.
`AERODECK_ENV=staging` and `AERODECK_ENV=production` always use that directory
([edge-routing.md](edge-routing.md)). Sign in there. The session cookie is
`Secure` in this container, so sign-in is on HTTPS. `app.aero-deck.app` stays
unused until the production console exists.

## 5. Boot sandboxes from the console

Open `/sandboxes` in the operator console ([sandboxes.md](sandboxes.md)):

1. When the console client (`CONTROL_PLANE_URL`, `CONTROL_PLANE_CLIENT_ID`,
   `CONTROL_PLANE_CLIENT_SECRET`) is set, `/sandboxes` renders the **New
   sandbox** card above the project-grouped guest list.
2. Select a project, enter a declared guest image name (for example
   `alpine-3.20`; lowercase letters, digits, `.`, `_`, `-` with no path
   separators), and set `vcpu` (`1–8`, default `1`), `memory_mib` (`32–4096`,
   default `128`), and `ttl_seconds` (`30–86400`, default `3600`), then click
   **Boot sandbox**.
3. The browser POSTs `/api/console/sandboxes`, which calls
   `POST /api/v1/projects/{project_id}/sandboxes` with the enrolled bearer and
   returns only `{ id, project_id, image, vcpu, memory_mib, ttl_seconds, status, expires_at, created_at }`
   (`host_id` and `hypervisor_vm_id` are never exposed to the browser).
4. The new guest is prepended to its project group on `/sandboxes`, showing its
   UTC `expires_at` timestamp (`YYYY-MM-DD HH:MM:SS UTC`) and `running` status
   (with a `TTL elapsed` badge if `expires_at` has passed before the next reap).

### Memory vs Firecracker hypervisor and unavailable hosts

- **`AERODECK_SANDBOX_HYPERVISOR=memory` (default on laptop/CI and dev until a
  KVM host is enrolled):** Boots succeed in-memory without an enrolled KVM host.
- **`AERODECK_SANDBOX_HYPERVISOR=firecracker` with no enrolled KVM host:**
  `POST /api/v1/projects/{project_id}/sandboxes` returns `503` with
  `{"code":"sandbox_unavailable"}`, which `POST /api/console/sandboxes` maps to
  `503` `{"error":"No enrolled Firecracker sandbox host is available.","code":"sandbox_unavailable"}`.
  The **New sandbox** form displays `No enrolled Firecracker sandbox host is available.`
  Enroll an out-of-band bare-metal KVM host with `POST /api/v1/sandbox-hosts`
  ([sandboxes.md](sandboxes.md)); never `terraform apply` Firecracker hosts from
  this repository ([ADR 0001](../adr/0001-sandbox-runtime-outside-iac.md)).
- **Hypervisor boot failure (`502`):** Returns `502`
  `{"error":"Hypervisor failed to boot the sandbox microVM.","code":"sandbox_failed"}`
  without leaking upstream hypervisor URLs or host details.
- **Mock / local seed (`CONTROL_PLANE_URL` unset):** `/sandboxes` shows only the
  in-memory seed grouped by project, hides the **New sandbox** form, returns
  `404` from `POST /api/console/sandboxes`, and never boots real microVMs.
