// Admin: LLM providers (ADR 0023). Add Ollama instances and OpenAI-compatible APIs, discover models, pick the default.
// Keys are write-only: the screen only ever shows where a key comes from (credential store / environment).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BarChart3, DollarSign, PlugZap, RefreshCw, Search, SlidersHorizontal, Star, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { Badge, Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import {
  api,
  json,
  type Classification,
  type HistoryRow,
  type ProviderModel,
  type ProviderRow,
  type ProvidersList,
  type ProviderTest,
} from "@/lib/api";
import { cn } from "@/lib/utils";

const CLASSES: Classification[] = ["public", "internal", "confidential"];
const RANK: Record<Classification, number> = { public: 0, internal: 1, confidential: 2 };

function useRun() {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      await qc.invalidateQueries({ queryKey: ["providers"] });
      await qc.invalidateQueries({ queryKey: ["models"] });
      await qc.invalidateQueries({ queryKey: ["limits-history"] });
    }
  };
  return { run, error, busy };
}

function LocalityBadge({ value }: { value: string }) {
  return (
    <Badge className={value === "local" ? "border-emerald-300 bg-emerald-50 text-emerald-800" : "border-sky-300 bg-sky-50 text-sky-800"}>
      {value}
    </Badge>
  );
}

function money(v: number | null | undefined) {
  return v == null ? "-" : `$${v.toFixed(4)}`;
}

// A usage bar: amber from 80 percent, red with "limit reached" at 100 percent. No limit: just the figure.
export function UsageBar({ label, used, limit, fmt = String }: { label: string; used: number; limit: number | null; fmt?: (v: number) => string }) {
  const pct = limit == null ? 0 : limit <= 0 ? 100 : Math.min(100, (used / limit) * 100);
  const state = limit == null ? "none" : pct >= 100 ? "reached" : pct >= 80 ? "warn" : "ok";
  return (
    <div className="text-xs" data-usage={label} data-state={state}>
      <div className="flex justify-between gap-2">
        <span>{label}</span>
        <span className={cn(state === "reached" ? "font-medium text-red-600" : state === "warn" ? "text-amber-700" : "text-muted-foreground")}>
          {fmt(used)}
          {limit != null ? ` of ${fmt(limit)}` : " (no limit)"}
          {state === "reached" ? " - limit reached" : state === "warn" ? " - over 80%" : ""}
        </span>
      </div>
      {limit != null && (
        <div className="mt-0.5 h-1.5 w-full rounded bg-muted">
          <div
            className={cn("h-1.5 rounded", state === "reached" ? "bg-red-500" : state === "warn" ? "bg-amber-500" : "bg-emerald-500")}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

const tokens = (v: number) => v.toLocaleString("en-US");

// Optional number field: "" means "not set" (null), so a limit can be reset to its default.
const num = (v: string) => (v.trim() === "" ? null : Number(v));
const str = (v: number | null | undefined) => (v == null ? "" : String(v));

function Usage30({ p }: { p: ProviderRow }) {
  const q = useQuery({
    queryKey: ["provider-daily", p.id],
    queryFn: () => api<{ day: string; requests: number; tokens: number; cost_usd: number }[]>(`/api/admin/providers/${p.id}/usage/daily?days=30`),
  });
  if (!q.data) return <p className="text-xs text-muted-foreground">Loading...</p>;
  const max = Math.max(1, ...q.data.map((d) => d.tokens));
  return (
    <div className="mt-2" data-testid={`chart-${p.slug}`}>
      <p className="mb-1 text-xs font-medium">Tokens per day, last 30 days (UTC)</p>
      <div className="flex h-16 items-end gap-[2px]">
        {q.data.map((d) => (
          <div
            key={d.day}
            title={`${d.day}: ${tokens(d.tokens)} tokens, ${d.requests} requests, ${money(d.cost_usd)}`}
            className="flex-1 rounded-t bg-brand/60"
            style={{ height: `${Math.max(d.tokens > 0 ? 4 : 1, (d.tokens / max) * 100)}%` }}
          />
        ))}
      </div>
    </div>
  );
}

function ModelLimits({ p, m, onDone }: { p: ProviderRow; m: ProviderModel; onDone: () => void }) {
  const { run, error, busy } = useRun();
  const [f, setF] = useState({
    input_usd_per_mtok: str(m.input_usd_per_mtok),
    output_usd_per_mtok: str(m.output_usd_per_mtok),
    max_output_tokens: str(m.max_output_tokens),
    max_input_tokens: str(m.max_input_tokens),
    tokens_per_day: str(m.tokens_per_day),
    tokens_per_month: str(m.tokens_per_month),
    monthly_budget_usd: str(m.monthly_budget_usd),
  });
  const [cls, setCls] = useState<string>(m.requested_classification ?? "");
  const [tools, setTools] = useState(m.supports_tools);
  const [jev, setJev] = useState({
    cost_tier: m.jev_router.cost_tier ?? "",
    allowed_models: (m.jev_router.allowed_models ?? []).join(", "),
    excluded_models: (m.jev_router.excluded_models ?? []).join(", "),
  });
  const list = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const save = () =>
    run(async () => {
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(f)) if (num(v) !== (m as unknown as Record<string, number | null>)[k]) body[k] = num(v);
      if ((cls || null) !== m.requested_classification) body.max_classification = cls || null;
      if (tools !== m.supports_tools) body.supports_tools = tools;
      if (m.is_jev_router)
        body.jev_router = {
          ...(jev.cost_tier ? { cost_tier: jev.cost_tier } : {}),
          ...(list(jev.allowed_models).length ? { allowed_models: list(jev.allowed_models) } : {}),
          ...(list(jev.excluded_models).length ? { excluded_models: list(jev.excluded_models) } : {}),
        };
      if (Object.keys(body).length) await api(`/api/admin/providers/${p.id}/models/${m.id}`, { method: "PATCH", body: json(body) });
      onDone();
    });
  const field = (k: keyof typeof f, label: string, placeholder: string) => (
    <div>
      <Label>{label}</Label>
      <Input value={f[k]} placeholder={placeholder} onChange={(e) => setF({ ...f, [k]: e.target.value })} aria-label={`${label} of ${m.model_id}`} />
    </div>
  );
  return (
    <div className="grid grid-cols-4 gap-2 rounded-md border border-dashed border-border p-2">
      {field("input_usd_per_mtok", "Input USD / 1M tokens", "unknown")}
      {field("output_usd_per_mtok", "Output USD / 1M tokens", "unknown")}
      {field("max_output_tokens", "Max output tokens", `provider (${p.max_output_tokens ?? p.default_max_output_tokens})`)}
      {field("max_input_tokens", "Max input tokens", `provider (${p.max_input_tokens ?? "none"})`)}
      {field("tokens_per_day", "Tokens per day (this model)", "no limit")}
      {field("tokens_per_month", "Tokens per month (this model)", "no limit")}
      {field("monthly_budget_usd", "USD per month (this model)", "no limit")}
      <div>
        <Label>Clearance of this model (only lower)</Label>
        <Select value={cls} onChange={(e) => setCls(e.target.value)} aria-label={`clearance of ${m.model_id}`}>
          <option value="">{m.is_jev_router ? "default (public)" : `provider's (${p.max_classification})`}</option>
          {CLASSES.filter((c) => RANK[c] <= RANK[p.max_classification]).map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </Select>
      </div>
      <label className="flex items-end gap-1 pb-2 text-xs">
        <input type="checkbox" checked={tools} onChange={(e) => setTools(e.target.checked)} aria-label={`tools for ${m.model_id}`} />
        may be offered tools
      </label>
      {m.is_jev_router && (
        <div className="col-span-4 grid grid-cols-3 gap-2 rounded-md bg-muted/40 p-2">
          <p className="col-span-3 text-xs text-muted-foreground">
            OpenRouter Jev Router: OpenRouter's router picks and serves a model from its catalog and sees the whole conversation (public data
            only by default). A cost tier is a preference, not a price cap: the budgets above enforce the real limit, and an unknown price
            counts as 0 in the worst-case check, so keep the monthly ceiling low and prefer an allowed list of models whose price you know.
          </p>
          <div>
            <Label>Cost tier</Label>
            <Select value={jev.cost_tier} onChange={(e) => setJev({ ...jev, cost_tier: e.target.value })} aria-label="jev router cost tier">
              <option value="">Jev's default</option>
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </Select>
          </div>
          <div>
            <Label>Allowed models (patterns, comma-separated)</Label>
            <Input value={jev.allowed_models} onChange={(e) => setJev({ ...jev, allowed_models: e.target.value })} placeholder="e.g. meta-llama/*:free" aria-label="jev router allowed models" />
          </div>
          <div>
            <Label>Excluded models (always enforced)</Label>
            <Input value={jev.excluded_models} onChange={(e) => setJev({ ...jev, excluded_models: e.target.value })} placeholder="e.g. anthropic/claude-opus*" aria-label="jev router excluded models" />
          </div>
        </div>
      )}
      <div className="flex items-end">
        <Button size="sm" onClick={() => void save()} disabled={busy}>
          Save model limits
        </Button>
      </div>
      {error && <p className="col-span-4 text-sm text-red-600">{error}</p>}
    </div>
  );
}

function DefaultModel({ data }: { data: ProvidersList }) {
  const { run, error } = useRun();
  const [choice, setChoice] = useState<string | null>(null);
  const options = data.providers
    .filter((p) => p.enabled)
    .flatMap((p) => p.models.filter((m) => m.enabled).map((m) => ({ id: m.model_id, label: `${m.model_id} (${p.locality}, up to ${p.max_classification})` })));
  const value = choice ?? data.default_model_id ?? "";
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Default model</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        Used when a user does not pick a model, but only when it is cleared for the request's classification. Otherwise the router
        falls back to a cleared model (local first): confidential data never goes to a cloud default.
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={value} onChange={(e) => setChoice(e.target.value)} className="w-[28rem]" aria-label="default model">
          <option value="">Automatic (prefer local models)</option>
          {options.map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </Select>
        <Button
          size="sm"
          disabled={value === (data.default_model_id ?? "")}
          onClick={() => run(() => api("/api/admin/models/default", { method: "PUT", body: json({ model_id: value || null }) })).then(() => setChoice(null))}
        >
          Set default
        </Button>
        <span className="text-xs text-muted-foreground" data-testid="current-default">
          current: {data.default_model_id ?? "automatic"}
        </span>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </Card>
  );
}

const EMPTY = {
  kind: "ollama" as "ollama" | "api",
  name: "",
  base_url: "",
  self_hosted: false,
  api_key: "",
  max_classification: "",
  monthly_budget_usd: "",
  daily_request_limit: "",
  models: "",
};

function AddProvider({ cloudMax }: { cloudMax: Classification }) {
  const { run, error, busy } = useRun();
  const [f, setF] = useState(EMPTY);
  const set = (patch: Partial<typeof EMPTY>) => setF({ ...f, ...patch });
  const submit = () =>
    run(async () => {
      await api("/api/admin/providers", {
        method: "POST",
        body: json({
          kind: f.kind,
          name: f.name,
          base_url: f.base_url,
          self_hosted: f.kind === "api" && f.self_hosted,
          ...(f.api_key ? { api_key: f.api_key } : {}),
          ...(f.max_classification ? { max_classification: f.max_classification } : {}),
          ...(f.monthly_budget_usd ? { monthly_budget_usd: Number(f.monthly_budget_usd) } : {}),
          ...(f.daily_request_limit ? { daily_request_limit: Number(f.daily_request_limit) } : {}),
          models: f.models
            .split(",")
            .map((m) => m.trim())
            .filter(Boolean),
        }),
      });
      setF(EMPTY);
    });
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Add a provider</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        <b>Ollama</b>: an Ollama instance (this host or another one). <b>API</b>: any OpenAI-compatible endpoint (OpenRouter, OpenAI, Together, Groq,
        vLLM, LM Studio...). Native non-OpenAI APIs (Anthropic, Gemini) are not supported. Locality is decided by the address: only loopback and
        private networks are local; anything public is cloud, starts cleared for <i>public</i> data only and can be raised to at most{" "}
        <i>{cloudMax}</i>. Private addresses need kind Ollama or "self-hosted".
      </p>
      <div className="grid grid-cols-4 gap-2">
        <div>
          <Label>Kind</Label>
          <Select value={f.kind} onChange={(e) => set({ kind: e.target.value as "ollama" | "api" })} aria-label="provider kind">
            <option value="ollama">Ollama</option>
            <option value="api">API (OpenAI-compatible)</option>
          </Select>
        </div>
        <div>
          <Label>Name</Label>
          <Input placeholder="provider name" value={f.name} onChange={(e) => set({ name: e.target.value })} />
        </div>
        <div className="col-span-2">
          <Label>Base URL</Label>
          <Input
            placeholder={f.kind === "ollama" ? "http://127.0.0.1:11434" : "https://api.example.com/v1"}
            aria-label="base URL"
            value={f.base_url}
            onChange={(e) => set({ base_url: e.target.value })}
          />
        </div>
        <div>
          <Label>API key (write-only, stored encrypted)</Label>
          <Input type="password" autoComplete="new-password" placeholder="optional" aria-label="API key" value={f.api_key} onChange={(e) => set({ api_key: e.target.value })} />
        </div>
        <div>
          <Label>Clearance (highest data it may receive)</Label>
          <Select value={f.max_classification} onChange={(e) => set({ max_classification: e.target.value })} aria-label="clearance">
            <option value="">automatic (local: confidential; cloud: public)</option>
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Monthly ceiling USD (default from the environment)</Label>
          <Input placeholder="env default" value={f.monthly_budget_usd} onChange={(e) => set({ monthly_budget_usd: e.target.value })} aria-label="monthly ceiling" />
        </div>
        <div>
          <Label>Daily request limit</Label>
          <Input placeholder="env default" value={f.daily_request_limit} onChange={(e) => set({ daily_request_limit: e.target.value })} aria-label="daily request limit" />
        </div>
        <div className="col-span-3">
          <Label>Models (optional; you can also discover them after saving)</Label>
          <Input placeholder="model ids, comma-separated" value={f.models} onChange={(e) => set({ models: e.target.value })} aria-label="models" />
        </div>
        <div className="flex items-end gap-3">
          {f.kind === "api" && (
            <label className="flex items-center gap-1 text-xs" title="Allows a private/loopback address (vLLM, LM Studio, a LAN gateway)">
              <input type="checkbox" checked={f.self_hosted} onChange={(e) => set({ self_hosted: e.target.checked })} aria-label="self-hosted" />
              self-hosted
            </label>
          )}
          <Button size="sm" onClick={() => void submit()} disabled={busy || !f.name || !f.base_url}>
            Add provider
          </Button>
        </div>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </Card>
  );
}

function EditProvider({ p, onDone }: { p: ProviderRow; onDone: () => void }) {
  const { run, error, busy } = useRun();
  const [f, setF] = useState({
    name: p.name,
    base_url: p.base_url,
    self_hosted: p.self_hosted,
    api_key: "",
    max_classification: p.requested_classification ?? "",
  });
  const [lim, setLim] = useState({
    monthly_budget_usd: str(p.metered ? p.monthly_budget_usd : null),
    daily_request_limit: str(p.metered ? p.daily_request_limit : null),
    max_output_tokens: str(p.max_output_tokens),
    max_input_tokens: str(p.max_input_tokens),
    tokens_per_day: str(p.tokens_per_day),
    tokens_per_month: str(p.tokens_per_month),
    max_turn_tokens: str(p.max_turn_tokens),
  });
  const save = () =>
    run(async () => {
      const body: Record<string, unknown> = {};
      if (f.name !== p.name) body.name = f.name;
      if (!p.builtin && f.base_url !== p.base_url) body.base_url = f.base_url;
      if (!p.builtin && p.kind === "api" && f.self_hosted !== p.self_hosted) body.self_hosted = f.self_hosted;
      if (f.api_key) body.api_key = f.api_key;
      if ((f.max_classification || null) !== p.requested_classification) body.max_classification = f.max_classification || null;
      const current: Record<string, number | null> = {
        monthly_budget_usd: p.metered ? p.monthly_budget_usd : null,
        daily_request_limit: p.metered ? p.daily_request_limit : null,
        max_output_tokens: p.max_output_tokens,
        max_input_tokens: p.max_input_tokens,
        tokens_per_day: p.tokens_per_day,
        tokens_per_month: p.tokens_per_month,
        max_turn_tokens: p.max_turn_tokens,
      };
      // Limits may be raised as well as lowered (step-up; every change is in the change history).
      for (const [k, v] of Object.entries(lim)) if (num(v) !== current[k]) body[k] = num(v);
      await api(`/api/admin/providers/${p.id}`, { method: "PATCH", body: json(body) });
      onDone();
    });
  const allowed = CLASSES.filter((c) => RANK[c] <= RANK[p.locality_max_classification]);
  return (
    <div className="mt-3 grid grid-cols-4 gap-2 rounded-md border border-dashed border-border p-3">
      <div>
        <Label>Name</Label>
        <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} aria-label={`name of ${p.slug}`} />
      </div>
      <div className="col-span-2">
        <Label>Base URL {p.builtin && "(from the environment)"}</Label>
        <Input value={f.base_url} disabled={p.builtin} onChange={(e) => setF({ ...f, base_url: e.target.value })} aria-label={`base URL of ${p.slug}`} />
      </div>
      <div>
        <Label>Clearance (max {p.locality_max_classification} for {p.locality})</Label>
        <Select value={f.max_classification} onChange={(e) => setF({ ...f, max_classification: e.target.value })} aria-label={`clearance of ${p.slug}`}>
          <option value="">automatic ({p.locality_max_classification})</option>
          {allowed.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label>Replace API key</Label>
        <Input type="password" autoComplete="new-password" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} aria-label={`new API key of ${p.slug}`} />
      </div>
      <div>
        <Label>Monthly ceiling USD{p.metered ? ` (default ${p.budget_cap_usd})` : ""}</Label>
        <Input placeholder={p.metered ? `default ${p.budget_cap_usd}` : "no limit"} value={lim.monthly_budget_usd} onChange={(e) => setLim({ ...lim, monthly_budget_usd: e.target.value })} aria-label={`monthly ceiling of ${p.slug}`} />
      </div>
      <div>
        <Label>Requests per day{p.daily_cap_env != null && p.metered ? ` (default ${p.daily_cap_env})` : ""}</Label>
        <Input placeholder={p.metered ? `default ${p.daily_cap_env}` : "no limit"} value={lim.daily_request_limit} onChange={(e) => setLim({ ...lim, daily_request_limit: e.target.value })} aria-label={`daily requests of ${p.slug}`} />
      </div>
      <div>
        <Label>Max output tokens per request</Label>
        <Input placeholder={`default ${p.default_max_output_tokens}`} value={lim.max_output_tokens} onChange={(e) => setLim({ ...lim, max_output_tokens: e.target.value })} aria-label={`max output tokens of ${p.slug}`} />
      </div>
      <div>
        <Label>Max input tokens per request</Label>
        <Input placeholder="no limit" value={lim.max_input_tokens} onChange={(e) => setLim({ ...lim, max_input_tokens: e.target.value })} aria-label={`max input tokens of ${p.slug}`} />
      </div>
      <div>
        <Label>Tokens per day</Label>
        <Input placeholder="no limit" value={lim.tokens_per_day} onChange={(e) => setLim({ ...lim, tokens_per_day: e.target.value })} aria-label={`tokens per day of ${p.slug}`} />
      </div>
      <div>
        <Label>Tokens per month</Label>
        <Input placeholder="no limit" value={lim.tokens_per_month} onChange={(e) => setLim({ ...lim, tokens_per_month: e.target.value })} aria-label={`tokens per month of ${p.slug}`} />
      </div>
      <div>
        <Label>Per-turn token cap (tool loop)</Label>
        <Input placeholder="no limit" value={lim.max_turn_tokens} onChange={(e) => setLim({ ...lim, max_turn_tokens: e.target.value })} aria-label={`per-turn cap of ${p.slug}`} />
      </div>
      <div className="flex items-end gap-2">
        {!p.builtin && p.kind === "api" && (
          <label className="flex items-center gap-1 text-xs">
            <input type="checkbox" checked={f.self_hosted} onChange={(e) => setF({ ...f, self_hosted: e.target.checked })} />
            self-hosted
          </label>
        )}
        <Button size="sm" onClick={() => void save()} disabled={busy}>
          Save changes
        </Button>
        {p.key_source === "vault" && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => run(() => api(`/api/admin/providers/${p.id}`, { method: "PATCH", body: json({ clear_api_key: true }) }))}
          >
            Remove key
          </Button>
        )}
      </div>
      {error && <p className="col-span-4 text-sm text-red-600">{error}</p>}
    </div>
  );
}

function ProviderCard({ p }: { p: ProviderRow }) {
  const { run, error, busy } = useRun();
  const [editing, setEditing] = useState(false);
  const [test, setTest] = useState<ProviderTest | null>(null);
  const [found, setFound] = useState<{ model: string; configured: boolean }[] | null>(null);
  const [manual, setManual] = useState("");
  const [chart, setChart] = useState(false);
  const [limitsOf, setLimitsOf] = useState<string | null>(null);
  const [synced, setSynced] = useState<string>("");
  const base = `/api/admin/providers/${p.id}`;
  const addModel = (model: string) =>
    run(async () => {
      await api(`${base}/models`, { method: "POST", body: json({ model }) });
      setFound((xs) => xs?.map((x) => (x.model === model ? { ...x, configured: true } : x)) ?? null);
    });
  return (
    <Card className={cn("p-4", !p.enabled && "opacity-70")} data-provider={p.slug}>
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold">{p.name}</h3>
        <span className="font-mono text-xs text-muted-foreground">{p.slug}</span>
        <Badge className="border-border">{p.kind === "ollama" ? "Ollama" : "API"}</Badge>
        {p.builtin && <Badge className="border-border">built-in</Badge>}
        <LocalityBadge value={p.locality} />
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          up to <ClassBadge value={p.max_classification} />
        </span>
        <span className={cn("text-xs", p.available ? "text-emerald-700" : "text-amber-700")} data-testid={`status-${p.slug}`}>
          {p.enabled ? (p.available ? "available" : `unavailable: ${p.status_reason}`) : "disabled"}
        </span>
        <div className="ml-auto flex gap-1">
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run(async () => setTest(await api<ProviderTest>(`${base}/test`, { method: "POST" })))}>
            <PlugZap className="h-3.5 w-3.5" /> Test
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => run(async () => setFound((await api<{ models: { model: string; configured: boolean }[] }>(`${base}/discover`, { method: "POST" })).models))}
          >
            <Search className="h-3.5 w-3.5" /> Discover models
          </Button>
          <Button size="sm" variant="outline" onClick={() => setEditing(!editing)}>
            <SlidersHorizontal className="h-3.5 w-3.5" /> Edit and limits
          </Button>
          <Button size="sm" variant="outline" onClick={() => setChart(!chart)} aria-label={`usage chart of ${p.slug}`}>
            <BarChart3 className="h-3.5 w-3.5" />
          </Button>
          {p.slug === "openrouter" && p.builtin && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  const r = await api<{ updated: Record<string, unknown>; not_listed: string[] }>(`${base}/prices/sync`, { method: "POST" });
                  setSynced(`Prices synced: ${Object.keys(r.updated).length} changed${r.not_listed.length ? `, not listed: ${r.not_listed.join(", ")}` : ""}`);
                })
              }
            >
              <DollarSign className="h-3.5 w-3.5" /> Sync prices
            </Button>
          )}
          <Button size="sm" variant="outline" onClick={() => run(() => api(base, { method: "PATCH", body: json({ enabled: !p.enabled }) }))}>
            {p.enabled ? "Disable" : "Enable"}
          </Button>
          {!p.builtin && (
            <Button
              size="sm"
              variant="ghost"
              aria-label={`Delete provider ${p.slug}`}
              onClick={() => confirm(`Delete provider ${p.name}? Its stored API key is deleted too.`) && run(() => api(base, { method: "DELETE" }))}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>
      <ul className="mt-2 grid grid-cols-2 gap-x-6 text-xs text-muted-foreground" data-testid={p.slug === "openrouter" ? "openrouter-status" : undefined}>
        <li>
          URL: <span className="font-mono">{p.base_url}</span>
          {p.self_hosted && " (self-hosted)"}
        </li>
        <li>
          API key: {p.key_source === "vault" ? "configured (credential store)" : p.key_source === "env" ? "configured (environment variable)" : "not set"}
        </li>
        {!p.metered && <li>Not metered (local Ollama); token limits and quotas still apply</li>}
        <li>Max output tokens per request: {p.max_output_tokens ?? `${p.default_max_output_tokens} (default)`}</li>
      </ul>
      <div className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1" data-testid={`usage-${p.slug}`}>
        <UsageBar label="Requests today" used={p.requests_today} limit={p.metered ? p.daily_request_limit : null} />
        <UsageBar label="USD this month" used={p.spent_this_month_usd} limit={p.metered ? p.monthly_budget_usd : null} fmt={money} />
        <UsageBar label="Tokens today" used={p.today.tokens} limit={p.tokens_per_day} fmt={tokens} />
        <UsageBar label="Tokens this month" used={p.month.tokens} limit={p.tokens_per_month} fmt={tokens} />
      </div>
      {test && (
        <p className={cn("mt-2 text-xs", test.ok ? "text-emerald-700" : "text-red-600")} data-testid={`test-${p.slug}`}>
          {test.ok ? `Connection OK (${test.locality}, ${test.models_available} models available)` : `Connection failed: ${test.error}`}
        </p>
      )}
      {synced && <p className="mt-1 text-xs text-muted-foreground">{synced}</p>}
      {chart && <Usage30 p={p} />}
      {editing && <EditProvider p={p} onDone={() => setEditing(false)} />}
      <Table className="mt-2">
        <thead>
          <tr>
            <Th>Model</Th>
            <Th>Price (USD per 1M in / out)</Th>
            <Th>Tokens today / month</Th>
            <Th>Enabled</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {p.models.map((m) => (
            <Fragment key={m.id}>
            <tr className="border-t border-border" data-model={m.model_id}>
              <td className="font-mono text-xs">
                {m.model_id}
                {m.is_default && (
                  <Badge className="ml-2 border-amber-300 bg-amber-50 text-amber-800">
                    <Star className="mr-1 h-3 w-3" /> default
                  </Badge>
                )}
                {m.env_managed && <span className="ml-2 font-sans text-muted-foreground">(from the environment)</span>}
              </td>
              <td className="text-xs">
                {m.input_usd_per_mtok == null && m.output_usd_per_mtok == null ? (
                  p.metered ? (
                    <span className="text-amber-700" title="Unknown price: the worst-case check counts it as 0; only the reported cost and the request cap bound it">
                      unknown
                    </span>
                  ) : (
                    <span className="text-muted-foreground">0 (local)</span>
                  )
                ) : (
                  <span>
                    {m.input_usd_per_mtok ?? 0} / {m.output_usd_per_mtok ?? 0}
                    <span className="ml-1 text-muted-foreground">({m.price_source})</span>
                  </span>
                )}
              </td>
              <td className="text-xs">
                {tokens(m.today.tokens)} / {tokens(m.month.tokens)}
                {m.month.cost_usd > 0 && <span className="ml-1 text-muted-foreground">{money(m.month.cost_usd)}</span>}
              </td>
              <td>
                <input
                  type="checkbox"
                  checked={m.enabled}
                  aria-label={`enable model ${m.model_id}`}
                  onChange={(e) => run(() => api(`${base}/models/${m.id}`, { method: "PATCH", body: json({ enabled: e.target.checked }) }))}
                />
              </td>
              <td className="text-right">
                {!m.is_default && m.enabled && p.enabled && (
                  <Button
                    size="sm"
                    variant="outline"
                    aria-label={`make ${m.model_id} the default`}
                    onClick={() => run(() => api("/api/admin/models/default", { method: "PUT", body: json({ model_id: m.model_id }) }))}
                  >
                    Make default
                  </Button>
                )}
                <Button size="sm" variant="ghost" aria-label={`limits of ${m.model_id}`} onClick={() => setLimitsOf(limitsOf === m.id ? null : m.id)}>
                  Price and limits
                </Button>
                {!m.env_managed && !m.is_default && (
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`remove model ${m.model_id}`}
                    onClick={() => run(() => api(`${base}/models/${m.id}`, { method: "DELETE" }))}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                )}
              </td>
            </tr>
            {limitsOf === m.id && (
              <tr>
                <td colSpan={5}>
                  <ModelLimits p={p} m={m} onDone={() => setLimitsOf(null)} />
                </td>
              </tr>
            )}
            </Fragment>
          ))}
        </tbody>
      </Table>
      <div className="mt-2 flex gap-2">
        <Input
          className="max-w-sm"
          placeholder="type a model id by hand"
          aria-label={`add model to ${p.slug}`}
          value={manual}
          onChange={(e) => setManual(e.target.value)}
        />
        <Button size="sm" variant="outline" disabled={!manual.trim()} onClick={() => void addModel(manual.trim()).then(() => setManual(""))}>
          Add model
        </Button>
      </div>
      {found && (
        <div className="mt-2 rounded-md bg-muted/40 p-2" data-testid={`discovered-${p.slug}`}>
          <p className="mb-1 text-xs font-medium">Models offered by the provider ({found.length})</p>
          <div className="flex flex-wrap gap-1">
            {found.map((m) => (
              <Button
                key={m.model}
                size="sm"
                variant="outline"
                disabled={m.configured}
                aria-label={`add discovered ${m.model}`}
                onClick={() => void addModel(m.model)}
              >
                {m.configured ? "added: " : "+ "}
                {m.model}
              </Button>
            ))}
          </div>
        </div>
      )}
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </Card>
  );
}

export default function Providers() {
  const list = useQuery({ queryKey: ["providers"], queryFn: () => api<ProvidersList>("/api/admin/providers") });
  const qc = useQueryClient();
  if (!list.data) return <p className="text-sm text-muted-foreground">{list.error ? String(list.error) : "Loading providers..."}</p>;
  return (
    <div className="space-y-4">
      <DefaultModel data={list.data} />
      <AddProvider cloudMax={list.data.cloud_max_classification} />
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">Providers ({list.data.providers.length})</h3>
        <Button size="sm" variant="ghost" onClick={() => void qc.invalidateQueries({ queryKey: ["providers"] })}>
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </Button>
      </div>
      {list.data.providers.map((p) => (
        <ProviderCard key={p.id} p={p} />
      ))}
      <ChangeHistory />
    </div>
  );
}

export function ChangeHistory() {
  const q = useQuery({ queryKey: ["limits-history"], queryFn: () => api<HistoryRow[]>("/api/admin/limits/history?limit=50") });
  return (
    <Card className="p-4" data-testid="limits-history">
      <h3 className="mb-1 text-sm font-semibold">Change history: limits, prices, quotas and the model router</h3>
      <p className="mb-2 text-xs text-muted-foreground">
        Limits can be raised as well as lowered, so a compromised admin session could raise budgets: check this list, and also set a credit limit
        on each provider's own key.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>When (UTC)</Th>
            <Th>Who</Th>
            <Th>What</Th>
            <Th>Change</Th>
          </tr>
        </thead>
        <tbody>
          {(q.data ?? []).map((h, i) => (
            <tr key={i} className="border-t border-border align-top text-xs">
              <td className="whitespace-nowrap">{h.ts.slice(0, 19).replace("T", " ")}</td>
              <td>{h.who ?? "-"}</td>
              <td>
                {h.action} <span className="font-mono text-muted-foreground">{h.resource}</span>
              </td>
              <td className="max-w-md break-all font-mono">{JSON.stringify(h.details.changes ?? h.details)}</td>
            </tr>
          ))}
        </tbody>
      </Table>
      {q.data && q.data.length === 0 && <p className="text-xs text-muted-foreground">No changes yet.</p>}
    </Card>
  );
}
