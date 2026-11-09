// Same-origin API client. The session cookie is HttpOnly; the CSRF token comes from /api/auth/me.
let csrfToken = "";
// The setup wizard's own CSRF token (its session cookie is limited to /api/setup).
let setupCsrf = "";

export function setCsrf(token: string) {
  csrfToken = token;
}

export function setSetupCsrf(token: string) {
  setupCsrf = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// Sensitive admin actions answer 403 "step_up_required" unless the user re-authenticated recently.
// The app registers a handler that asks for the password (or a TOTP code) and calls /api/auth/step-up.
let stepUpHandler: (() => Promise<boolean>) | null = null;
export function setStepUpHandler(fn: (() => Promise<boolean>) | null) {
  stepUpHandler = fn;
}

export async function api<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  try {
    return await rawApi<T>(path, init);
  } catch (e) {
    if (!retried && e instanceof ApiError && e.status === 403 && e.message === "step_up_required" && stepUpHandler) {
      if (await stepUpHandler()) return api<T>(path, init, true);
    }
    throw e;
  }
}

async function rawApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
  if (method !== "GET" && setupCsrf && path.startsWith("/api/setup/")) headers.set("X-Setup-CSRF", setupCsrf);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const res = await fetch(path, { ...init, headers, credentials: "same-origin" });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const json = (body: unknown) => JSON.stringify(body);

export type Classification = "public" | "internal" | "confidential";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  roles: string[];
  csrf_token: string;
  can_manage_content: boolean;
  mfa_enabled: boolean;
  mfa_required: boolean;
  step_up_valid: boolean;
  must_change_password: boolean;
}

export interface Info {
  setup_required: boolean;
  company_name: string;
  demo: boolean;
  product: string;
}

export interface SetupStep {
  key: string;
  index: number;
  status: "pending" | "done" | "skipped";
  required: boolean;
}

export interface SetupStatus {
  mode: string;
  steps: SetupStep[];
  current_step: number;
  session: boolean;
  setup_csrf: string | null;
  admin_signed_in: boolean;
  token_source: "init" | "file";
  token_file: string;
  install_type: string | null;
  license_accepted: boolean;
}

export interface EnvCheck {
  id: string;
  label: string;
  status: "ok" | "warn" | "fail" | "info";
  detail: string;
  fix: string;
}

export interface Organization {
  company_name: string;
  cloud_max_classification: Classification;
  cloud_max_source: "database" | "environment";
  environment_cloud_max_classification: string;
}

export interface ModelDownload {
  id: string;
  model: string;
  size_bytes: number;
  status: "running" | "done" | "failed" | "cancelled";
  message: string;
  completed: number;
  total: number;
}

export interface ModelsStatus {
  ollama_url: string;
  reachable: boolean;
  error: string;
  installed: { name: string; size: number }[];
  embedding: { model: string; dimensions: number; installed: boolean; locked: boolean; confirmed: boolean };
  chat: { environment_model: string; default_model: string | null; installed: boolean };
  suggested: { name: string; purpose: string; license: string; note: string }[];
  downloads: ModelDownload[];
  registry: string;
}

export interface SetupProgress {
  admins: number;
  users: number;
  roles: number;
  containers: number;
  containers_by_classification: Record<Classification, number>;
  grants: number;
  documents: number;
  documents_ready: number;
  documents_pending: number;
  documents_failed: number;
  chunks: number;
  connectors: number;
  tools_enabled: number;
  demo: boolean;
}

export interface ChecklistItem {
  key: string;
  label: string;
  done: boolean;
  dismissed: boolean;
  dismissable: boolean;
  where: string;
}

export interface CheckResult {
  user: { id: string; email: string; roles: string[] };
  effective: EffectivePermissions;
  sources: { title: string; container: string; classification: Classification }[];
  model: string | null;
  classification: Classification | null;
  refusal: string | null;
  error: string | null;
  answer: string | null;
  answer_hidden: boolean;
}
export interface SecretRow {
  name: string;
  description: string;
  version: number;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  last_used_by: string | null;
  used_by: string[];
}
export interface OpenRouterSettings {
  configured: boolean;
  key_source: "vault" | "env" | "none";
  enabled: boolean;
  model: string;
  monthly_budget_usd: number;
  budget_cap_usd: number;
  spent_this_month_usd: number;
  cloud_max_classification: string;
}
export interface Citation {
  n: number;
  chunk_id: string;
  document_id: string;
  title: string;
  container: string;
  classification: Classification;
  heading?: string | null;
  page?: number | null;
  score: number;
}
export interface Conversation {
  id: string;
  title: string;
  created_at: string;
}
export interface ToolLog {
  name: string;
  ok: boolean;
  classification?: string;
  refused?: string;
  flags?: string[];
}
export interface MessageRow {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: { items?: Citation[]; tools?: ToolLog[] };
  model_used: string | null;
  classification: Classification;
}
export interface ModelInfo {
  id: string;
  provider: string;
  model: string;
  locality: "local" | "cloud";
  max_classification: Classification;
  is_default: boolean;
}
export interface UsageFigures {
  requests: number;
  tokens: number;
  cost_usd: number;
  estimated_requests?: number;
}
export interface ProviderModel {
  id: string;
  model_id: string;
  model: string;
  enabled: boolean;
  env_managed: boolean;
  is_default: boolean;
  max_output_tokens: number | null;
  max_input_tokens: number | null;
  tokens_per_day: number | null;
  tokens_per_month: number | null;
  monthly_budget_usd: number | null;
  input_usd_per_mtok: number | null;
  output_usd_per_mtok: number | null;
  price_source: string | null;
  supports_tools: boolean;
  requested_classification: Classification | null;
  is_jev_router: boolean;
  jev_router: { cost_tier?: "low" | "medium" | "high"; allowed_models?: string[]; excluded_models?: string[] };
  today: UsageFigures;
  month: UsageFigures;
}
export interface ProviderRow {
  id: string;
  slug: string;
  name: string;
  kind: "ollama" | "api";
  builtin: boolean;
  enabled: boolean;
  base_url: string;
  self_hosted: boolean;
  locality: "local" | "cloud";
  requested_classification: Classification | null;
  max_classification: Classification;
  locality_max_classification: Classification;
  key_source: "vault" | "env" | "none";
  secret_name: string | null;
  metered: boolean;
  monthly_budget_usd: number | null;
  budget_cap_usd: number | null;
  spent_this_month_usd: number;
  daily_request_limit: number | null;
  daily_cap_env: number | null;
  requests_today: number;
  max_output_tokens: number | null;
  default_max_output_tokens: number;
  max_input_tokens: number | null;
  tokens_per_day: number | null;
  tokens_per_month: number | null;
  max_turn_tokens: number | null;
  today: UsageFigures;
  month: UsageFigures;
  available: boolean;
  status_reason: string;
  models: ProviderModel[];
}
export interface ProvidersList {
  providers: ProviderRow[];
  default_model_id: string | null;
  cloud_max_classification: Classification;
}
export interface ProviderTest {
  ok: boolean;
  locality: string;
  models_available?: number;
  error?: string;
  warning?: string | null;
}
export interface ChunkView {
  id: string;
  document_title: string;
  container_name: string;
  ordinal: number;
  text: string;
  meta: Record<string, unknown>;
}
export interface UserRow {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  is_active: boolean;
  role_ids: string[];
  mfa_enabled: boolean;
  must_change_password?: boolean;
}
export interface RoleRow {
  id: string;
  name: string;
  description: string;
  user_count: number;
  grant_count: number;
}
export type AccessLevel = "none" | "read" | "contribute" | "manage";
export interface EffectivePermissions {
  user_id: string;
  is_admin: boolean;
  is_active: boolean;
  roles: string[];
  containers: { id: string; name: string; classification: Classification; level: AccessLevel }[];
  restricted_documents: { id: string; title: string; container: string }[];
  tools: string[];
}
export interface ContainerRow {
  id: string;
  name: string;
  slug: string;
  description: string;
  classification: Classification;
  can_read?: boolean;
  can_contribute?: boolean;
  can_manage?: boolean;
}
export interface DocumentRow {
  id: string;
  container_id: string;
  title: string;
  filename: string;
  restricted: boolean;
  current_version_id: string | null;
  latest_version_id: string | null;
  latest_status: string | null;
  latest_error: string | null;
  version_count: number;
  chunk_count: number;
  can_read: boolean;
}
export interface VersionRow {
  id: string;
  sha256: string;
  status: string;
  error: string | null;
  size_bytes: number;
  created_at: string;
  created_by: string | null;
  is_current: boolean;
}
export interface DocumentDetail extends DocumentRow {
  versions: VersionRow[];
}
export interface GrantRow {
  id: string;
  role_id: string;
  resource_type: "container" | "document" | "tool";
  resource_id: string;
  action: "read" | "contribute" | "manage" | "execute";
}
export interface ToolRow {
  id: string;
  server: string;
  name: string;
  model_name: string;
  description: string;
  classification: Classification;
  enabled: boolean;
  server_enabled: boolean;
}
export interface AuditRow {
  id: number;
  ts: string;
  user_id: string | null;
  action: string;
  resource: string;
  decision: string;
  details: Record<string, unknown>;
}
export interface JobRow {
  id: string;
  version_id: string;
  state: string;
  attempts: number;
  error: string | null;
  created_at: string;
}

export type ChatEvent =
  | { type: "meta"; conversation_id: string }
  | { type: "citations"; items: Citation[] }
  | { type: "route"; model: string; classification: Classification }
  | { type: "delta"; text: string }
  | { type: "tool_call"; id: string; name: string; arguments: string }
  | { type: "tool_result"; id: string; name: string; ok: boolean; classification: Classification }
  | { type: "refusal"; reason: string; classification: Classification }
  | { type: "error"; message: string; code?: "quota_exceeded" | "limit_reached" }
  | { type: "done"; message_id: string; model: string | null; classification: Classification; answer: string };

/** POST /api/chat and parse the SSE stream with fetch (cookie auth, CSRF header). */
export async function streamChat(
  body: { message: string; conversation_id?: string | null; model?: string | null },
  onEvent: (e: ChatEvent) => void,
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  if (!res.ok || !res.body) {
    let msg = res.statusText;
    try {
      msg = (await res.json()).detail ?? msg;
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, msg);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      const data = frame
        .split("\n")
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).trim())
        .join("");
      if (data) onEvent(JSON.parse(data) as ChatEvent);
    }
  }
}

export interface ConnectorRow {
  id: string;
  name: string;
  kind: "folder" | "web";
  container_id: string;
  container_name: string;
  config: Record<string, unknown>;
  secret_names: string[];
  schedule_minutes: number | null;
  enabled: boolean;
  status: string;
  last_sync_at: string | null;
  last_error: string | null;
  item_count: number;
}
export interface SyncRunRow {
  id: string;
  status: string;
  dry_run: boolean;
  triggered_by: string;
  created_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  counts: Record<string, number>;
  errors: { items?: { item: string; error: string }[]; fatal?: string | null };
}
export interface PreviewResult {
  counts: Record<string, number>;
  items: { item: string; action: string }[];
  errors: { item: string; error: string }[];
  fatal: string | null;
  warning: string;
}

export interface RegistryTool {
  id: string;
  name: string;
  model_name: string;
  description: string;
  description_for_model: string;
  input_schema: Record<string, unknown>;
  classification: Classification;
  enabled: boolean;
  needs_review: boolean;
  review_reason: string | null;
  approved: boolean;
  arg_constraints: Record<string, Record<string, unknown>>;
  cannot_enable: string | null;
}
export interface RegistryServer {
  id: string;
  name: string;
  builtin: boolean;
  transport: string;
  url: string | null;
  description: string;
  status: "pending" | "approved" | "disabled";
  auth_secret: string | null;
  max_classification: Classification;
  result_classification: Classification;
  send_permission_context: boolean;
  limits: Record<string, number>;
  runtime: Record<string, unknown>;
  last_discovered_at: string | null;
  tools: RegistryTool[];
}

export type QuotaLimits = {
  tokens_per_day: number | null;
  tokens_per_month: number | null;
  usd_per_day: number | null;
  usd_per_month: number | null;
};
export interface QuotaRow extends QuotaLimits {
  id: string;
  subject: "user" | "role";
  user_id: string | null;
  role_id: string | null;
  subject_name: string;
  kind: "standard" | "collective";
  provider: string | null;
  used: Record<keyof QuotaLimits, number> | null;
}
export interface Allowance {
  kind: "standard" | "collective";
  provider: string | null;
  source: string;
  limits: QuotaLimits;
  used?: Record<keyof QuotaLimits, number>;
  exhausted: (keyof QuotaLimits)[];
}
export interface MyUsage {
  day: string;
  month: string;
  today: UsageFigures;
  this_month: UsageFigures;
  daily: { day: string; requests: number; tokens: number; cost_usd: number }[];
  allowances: Allowance[];
}
export interface AdminUsage {
  day: string;
  month: string;
  models: { provider: string; model: string; today: UsageFigures; month: UsageFigures }[];
  users: { user_id: string | null; email: string; today: UsageFigures; month: UsageFigures }[];
  pools: (QuotaRow & { members: number })[];
}
export interface HistoryRow {
  ts: string;
  who: string | null;
  action: string;
  resource: string;
  details: Record<string, unknown>;
}
