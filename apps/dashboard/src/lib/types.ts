export type DeploymentStatus = "draft" | "queued" | "running" | "succeeded" | "failed" | "cancelled";

export type Project = {
  id: string;
  slug: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectSource = "control-plane" | "mock";

export type ProjectPage = {
  items: Project[];
  total: number;
  limit: number;
  offset: number;
  source: ProjectSource;
};

export type Sandbox = {
  id: string;
  project_id: string;
  image: string;
  vcpu: number;
  memory_mib: number;
  ttl_seconds: number;
  status: "pending" | "running" | "stopped" | "expired";
  expires_at: string;
  created_at: string;
};

export type SandboxPage = {
  items: Sandbox[];
  total: number;
  limit: number;
  offset: number;
  source: ProjectSource;
};

export type DeploymentTrigger = "manual" | "webhook" | "rollback" | "promote";

export type Deployment = {
  id: string;
  repo_url: string;
  owner: string;
  repo: string;
  project_id?: string | null;
  environment_id?: string | null;
  /** Last provision Celery job id when enqueue succeeded; omit/null on older rows. */
  provision_job_id?: string | null;
  /** 40-char commit recorded after checkout. Null until then, and on older rows. */
  git_sha?: string | null;
  git_ref?: string | null;
  trigger?: DeploymentTrigger | null;
  actor_user_id?: string | null;
  started_at: string;
  finished_at?: string | null;
  swap_started_at?: string | null;
  /** Set while this row is waiting behind another deployment. Status stays queued. */
  held_for_deployment_id?: string | null;
  /** Set while a production deploy is waiting for an owner. Status stays queued. */
  approval_required_at?: string | null;
  approved_at?: string | null;
  status: DeploymentStatus;
  /** Last confirmed deploy step while status is draft (#343). */
  draft_step?: "build" | "security" | "resources" | "variables" | "target" | "launch" | null;
  /** Sorted names from the encrypted release snapshot. Null when none (#273). */
  snapshot_variable_names?: string[] | null;
  /** Set on rollback responses: true when the snapshot was applied (#273). */
  variables_restored?: boolean | null;
};

/** Control-plane Celery job status from `GET /api/v1/jobs/{id}`. */
export type ProvisionJobStatus = "queued" | "running" | "retrying" | "succeeded" | "failed";

export const PROVISION_JOB_STATUSES = new Set<ProvisionJobStatus>([
  "queued",
  "running",
  "retrying",
  "succeeded",
  "failed",
]);

export const PROVISION_JOB_ACTIVE_STATUSES = new Set<ProvisionJobStatus>([
  "queued",
  "running",
  "retrying",
]);

export type ProvisionJob = {
  job_id: string;
  status: ProvisionJobStatus;
  result?: Record<string, string | boolean> | null;
  error?: string | null;
};

/** Control-plane job kinds accepted by provision / reconcile enqueue. */
export type JobKind = "provision" | "reconcile";

export const JOB_KINDS = new Set<JobKind>(["provision", "reconcile"]);

/** `202` body from `POST .../environments/{id}/provision|reconcile`. */
export type AcceptedJob = {
  job_id: string | null;
  /** Present when the new deployment is waiting behind this one and no job was started. */
  active_deployment_id?: string | null;
  kind: JobKind;
  project_id: string;
  environment_id: string;
  status: ProvisionJobStatus;
  /** True when the row is stored and waiting for an owner, so no job was started. */
  approval_pending?: boolean;
};

export type DeployOverlap = "wait" | "override";

export type EnvironmentOption = {
  id: string;
  project_id: string;
  project_slug: string;
  project_name: string;
  slug: string;
  name: string;
  kind: string;
  region: string;
  /** How a new deploy behaves while another is still queued or running. Defaults to wait. */
  deploy_overlap?: DeployOverlap;
  /** Succeeded images this environment keeps for rollback. Defaults to 5. */
  rollback_images_to_keep?: number;
  /** Succeeded images that have not been pruned. */
  retained_rollback_images?: number;
  /** Executable and arguments run before traffic moves. Null clears it. */
  release_command?: string | null;
  /** When true, a GitHub push does not start a deploy. */
  auto_deploy_paused?: boolean;
  /** When true, a developer or webhook deploy to production waits for an owner. */
  prod_deploy_approval?: boolean;
};

export type EnvironmentOptionPage = {
  items: EnvironmentOption[];
  total: number;
  source: ProjectSource;
};

/** Traefik route declared on a control-plane project (`GET .../routes`). */
export type ProjectRoute = {
  id: string;
  project_id: string;
  environment_id: string;
  hostname: string;
  enabled: boolean;
};

export type DnsRecordType = "A" | "AAAA" | "CNAME";

export const DNS_RECORD_TYPES = new Set<DnsRecordType>(["A", "AAAA", "CNAME"]);

export type DnsRecordStatus = "published" | "unpublished";

export const DNS_RECORD_STATUSES = new Set<DnsRecordStatus>(["published", "unpublished"]);

/** Public DNS guidance for a project route (`GET .../routes/{route_id}/dns`). */
export type RouteDnsRecord = {
  route_id: string;
  hostname: string;
  record_type: DnsRecordType;
  data: string;
  ttl: number;
  status: DnsRecordStatus;
};

export type RouteCertificateStatus = "pending" | "issued" | "failed";

export const ROUTE_CERTIFICATE_STATUSES = new Set<RouteCertificateStatus>([
  "pending",
  "issued",
  "failed",
]);

/**
 * Public TLS certificate status for a project route (`GET .../routes/{route_id}/certificate`).
 * Never contains PEM, private key material, or raw provider errors.
 */
export type RouteCertificate = {
  route_id: string;
  hostname: string;
  status: RouteCertificateStatus;
  not_after: string | null;
};

export type ProjectDetail = {
  project: Project;
  environments: EnvironmentOption[];
  /** Empty on mock; populated from the control plane when enrolled. */
  routes: ProjectRoute[];
  /** True when the control-plane routes list failed (distinct from an empty list). */
  routes_unavailable: boolean;
  source: ProjectSource;
};

export type ApiError = {
  detail: string;
  code: string;
  deployment_id?: string;
};

/** Console Deploy create when the deployment row exists but provision was not queued. */
export const PROVISION_ENQUEUE_FAILED = "provision_enqueue_failed";

export type PublicUser = {
  id: string;
  email: string;
  created_at: string;
};

export type AuthErrorCode =
  | "missing_email"
  | "missing_password"
  | "invalid_email"
  | "weak_password"
  | "email_taken"
  | "invalid_credentials"
  | "unauthenticated"
  | "invalid_json";

export type DeployLogEvent = {
  delayMs: number;
  text: string;
};

export type AddonType = "postgres" | "redis" | "mongodb";

export type BlueprintEnvVar = {
  key: string;
  description: string;
  required: boolean;
  secret: boolean;
  default: string | null;
  generate: "hex32" | null;
};

export type BlueprintAddon = {
  type: AddonType;
  env_var: string;
  version: string;
};

export type BlueprintBuild = {
  strategy: "dockerfile" | "compose" | "auto";
  dockerfile: string;
  compose_file: string | null;
  service: string | null;
  port: number;
  runtime?: "node" | "python" | "go" | "java" | "rust" | "static" | null;
  marker?: string | null;
};

export type DeploymentBlueprint = {
  version: number;
  name: string;
  repo_url: string;
  source: string;
  build: BlueprintBuild;
  addons: BlueprintAddon[];
  env: BlueprintEnvVar[];
};

export type RuntimeLogsStatus = "running" | "exited" | "not_found" | "unavailable";

export type ContainerRuntimeLogs = {
  container: string;
  status: RuntimeLogsStatus;
  lines: string[];
  error: string | null;
};

export type EnvironmentVarSummary = {
  key: string;
  is_secret: boolean;
  updated_at: string;
};

export type EnvironmentVarUpsertItem = {
  key: string;
  value: string;
  is_secret?: boolean;
};

export type EnvironmentVarCloneResult = {
  copied: number;
  skipped: number;
  items: EnvironmentVarSummary[];
};

export type EnvironmentWebhook = {
  id: string;
  project_id: string;
  environment_id: string;
  repo_url: string;
  branch: string;
  addons: AddonType[];
  enabled: boolean;
  has_secret: boolean;
  secret: string | null;
  last_delivery_id: string | null;
  last_triggered_at: string | null;
};

export type EnvironmentWebhookInput = {
  repo_url: string;
  branch: string;
  addons?: AddonType[];
  enabled?: boolean;
  rotate_secret?: boolean;
};

export type ContainerAction = "start" | "stop" | "restart";

export type ContainerActionResult = {
  container: string;
  action: ContainerAction;
  status: RuntimeLogsStatus;
  ok: boolean;
  error: string | null;
};

export type ContainerMetrics = {
  container: string;
  status: RuntimeLogsStatus;
  cpu_percent: string | null;
  memory_usage: string | null;
  memory_percent: string | null;
  started_at: string | null;
  restart_count: number;
  error: string | null;
};

export type ProjectRole = "owner" | "developer" | "viewer";

export type ProjectMember = {
  id: string;
  project_id: string;
  user_id: string;
  email: string;
  role: ProjectRole;
  created_at: string;
  updated_at: string;
};

export type AuditEvent = {
  id: string;
  project_id: string;
  environment_id: string | null;
  actor: string;
  action: string;
  resource_type: string;
  resource_id: string | null;
  summary: string;
  created_at: string;
};

export type NotificationProvider = "slack" | "discord" | "webhook";

export type NotificationChannel = {
  id: string;
  project_id: string;
  provider: NotificationProvider;
  url_hint: string;
  notify_on_started: boolean;
  notify_on_succeeded: boolean;
  notify_on_failed: boolean;
  enabled: boolean;
  updated_at: string;
};

export type NotificationChannelInput = {
  provider: NotificationProvider;
  webhook_url?: string;
  notify_on_started: boolean;
  notify_on_succeeded: boolean;
  notify_on_failed: boolean;
  enabled: boolean;
};

export type NotificationTestResult = {
  ok: boolean;
  status_code: number | null;
  error: string | null;
};
