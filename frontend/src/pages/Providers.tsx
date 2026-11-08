// Admin: LLM providers (ADR 0023). Add Ollama instances and OpenAI-compatible APIs, discover models, pick the default.
// Keys are write-only: the screen only ever shows where a key comes from (credential store / environment).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { PlugZap, RefreshCw, Search, Star, Trash2 } from "lucide-react";
import { useState } from "react";
import { Badge, Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import { api, json, type Classification, type ProviderRow, type ProvidersList, type ProviderTest } from "@/lib/api";
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
          <Label>Monthly ceiling USD (API only lowers the env cap)</Label>
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
    monthly_budget_usd: "",
    daily_request_limit: "",
  });
  const save = () =>
    run(async () => {
      const body: Record<string, unknown> = {};
      if (f.name !== p.name) body.name = f.name;
      if (!p.builtin && f.base_url !== p.base_url) body.base_url = f.base_url;
      if (!p.builtin && p.kind === "api" && f.self_hosted !== p.self_hosted) body.self_hosted = f.self_hosted;
      if (f.api_key) body.api_key = f.api_key;
      if ((f.max_classification || null) !== p.requested_classification) body.max_classification = f.max_classification || null;
      if (f.monthly_budget_usd) body.monthly_budget_usd = Number(f.monthly_budget_usd);
      if (f.daily_request_limit) body.daily_request_limit = Number(f.daily_request_limit);
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
      {p.metered && (
        <>
          <div>
            <Label>Monthly ceiling USD (max {p.budget_cap_usd})</Label>
            <Input placeholder={String(p.monthly_budget_usd ?? "")} value={f.monthly_budget_usd} onChange={(e) => setF({ ...f, monthly_budget_usd: e.target.value })} />
          </div>
          <div>
            <Label>Daily requests {p.daily_cap_env != null ? `(max ${p.daily_cap_env})` : ""}</Label>
            <Input placeholder={String(p.daily_request_limit ?? "none")} value={f.daily_request_limit} onChange={(e) => setF({ ...f, daily_request_limit: e.target.value })} />
          </div>
        </>
      )}
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
            Edit
          </Button>
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
        {p.metered ? (
          <>
            <li>
              Spent this month: {money(p.spent_this_month_usd)} of {money(p.monthly_budget_usd)} (env cap {money(p.budget_cap_usd)})
            </li>
            <li>
              Requests today: {p.requests_today}
              {p.daily_request_limit != null ? ` of ${p.daily_request_limit}` : " (no daily cap)"}
            </li>
          </>
        ) : (
          <li>Not metered (local Ollama)</li>
        )}
        {p.free_only && <li>Free models only (ids ending in :free)</li>}
      </ul>
      {test && (
        <p className={cn("mt-2 text-xs", test.ok ? "text-emerald-700" : "text-red-600")} data-testid={`test-${p.slug}`}>
          {test.ok ? `Connection OK (${test.locality}, ${test.models_available} models available)` : `Connection failed: ${test.error}`}
        </p>
      )}
      {editing && <EditProvider p={p} onDone={() => setEditing(false)} />}
      <Table className="mt-2">
        <thead>
          <tr>
            <Th>Model</Th>
            <Th>Enabled</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {p.models.map((m) => (
            <tr key={m.id} className="border-t border-border" data-model={m.model_id}>
              <td className="font-mono text-xs">
                {m.model_id}
                {m.is_default && (
                  <Badge className="ml-2 border-amber-300 bg-amber-50 text-amber-800">
                    <Star className="mr-1 h-3 w-3" /> default
                  </Badge>
                )}
                {m.env_managed && <span className="ml-2 font-sans text-muted-foreground">(from the environment)</span>}
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
    </div>
  );
}
