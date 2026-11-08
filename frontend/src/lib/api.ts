// Same-origin API client. The session cookie is HttpOnly; the CSRF token comes from /api/auth/me.
let csrfToken = "";

export function setCsrf(token: string) {
  csrfToken = token;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== "GET" && csrfToken) headers.set("X-CSRF-Token", csrfToken);
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
}
export interface RoleRow {
  id: string;
  name: string;
  description: string;
}
export interface ContainerRow {
  id: string;
  name: string;
  slug: string;
  description: string;
  classification: Classification;
  can_read?: boolean;
  can_manage?: boolean;
}
export interface DocumentRow {
  id: string;
  container_id: string;
  title: string;
  filename: string;
  restricted: boolean;
  current_version_id: string | null;
  latest_status: string | null;
  chunk_count: number;
}
export interface GrantRow {
  id: string;
  role_id: string;
  resource_type: "container" | "document" | "tool";
  resource_id: string;
  action: "read" | "manage" | "execute";
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
  | { type: "error"; message: string }
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
