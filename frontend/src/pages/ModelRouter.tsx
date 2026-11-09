// Admin > Model router (PLAN-v0.3 section 5, ADR 0029 to 0032). The router only chooses among the models a
// request is already permitted to use; off means exactly the previous behaviour.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, FlaskConical, PlugZap, Trash2 } from "lucide-react";
import { useState } from "react";
import { Badge, Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import { api, json, type Classification, type UserRow } from "@/lib/api";
import { cn } from "@/lib/utils";

type Decider = {
  id: string;
  name: string;
  kind: "prompted" | "protocol" | "jev_official";
  enabled: boolean;
  position: number;
  base_url: string | null;
  endpoint: string | null;
  model: string | null;
  self_hosted: boolean;
  locality: string;
  requested_classification: Classification | null;
  max_classification: Classification;
  locality_max_classification: Classification;
  see_context: boolean;
  timeout_ms: number | null;
  monthly_budget_usd: number | null;
  daily_request_cap: number | null;
  input_usd_per_mtok: number;
  output_usd_per_mtok: number;
  price_is_default: boolean;
  spent_this_month_usd: number;
  requests_today: number;
  key_source: string;
  available: boolean;
  status_reason: string;
};
type Lane = {
  id: string;
  name: string;
  position: number;
  enabled: boolean;
  is_default: boolean;
  conditions: Record<string, unknown>;
  targets: string[];
};
type Overview = {
  settings: { enabled: boolean; timeout_ms: number; long_context_tokens: number };
  deciders: Decider[];
  lanes: Lane[];
  models: { id: string; locality: string; max_classification: Classification; supports_tools: boolean; is_default: boolean }[];
  task_types: Record<string, string>;
  difficulty_levels: string[];
  official: { url: string; model: string };
};
type DecisionRow = {
  id: number;
  ts: string;
  dry_run: boolean;
  classification: Classification;
  tools_offered: boolean;
  permitted: string[];
  decider: string;
  attempts: { decider: string; ok: boolean; error?: string; latency_ms?: number; cost_usd?: number }[];
  answers: Record<string, unknown>;
  lane: string | null;
  chosen_model: string | null;
  reason: string;
  latency_ms: number;
  cost_usd: number;
};

const CLASSES: Classification[] = ["public", "internal", "confidential"];
const KIND_LABEL: Record<Decider["kind"], string> = {
  prompted: "Prompted LLM (registry model)",
  protocol: "Open Jev-style server (/v1/systemone)",
  jev_official: "Official Jev API (TypeSafe)",
};

function useOverview() {
  return useQuery({ queryKey: ["router"], queryFn: () => api<Overview>("/api/admin/router") });
}

function useRun() {
  const qc = useQueryClient();
  const [error, setError] = useState("");
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      await qc.invalidateQueries({ queryKey: ["router"] });
      await qc.invalidateQueries({ queryKey: ["router-decisions"] });
      await qc.invalidateQueries({ queryKey: ["limits-history"] });
    }
  };
  return { run, error };
}

function SettingsCard({ o }: { o: Overview }) {
  const { run, error } = useRun();
  const [timeout, setTimeoutMs] = useState(String(o.settings.timeout_ms));
  const [longCtx, setLongCtx] = useState(String(o.settings.long_context_tokens));
  const on = o.settings.enabled;
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h3 className="text-sm font-semibold">Model router</h3>
        <Badge className={on ? "border-emerald-300 bg-emerald-50 text-emerald-800" : "border-border text-muted-foreground"} data-testid="router-state">
          {on ? "enabled" : "disabled"}
        </Badge>
        <Button
          size="sm"
          variant={on ? "outline" : "default"}
          onClick={() => run(() => api("/api/admin/router/settings", { method: "PUT", body: json({ enabled: !on }) }))}
        >
          {on ? "Disable (kill switch)" : "Enable router"}
        </Button>
        <div className="ml-auto flex items-end gap-2">
          <div>
            <Label>Decider timeout (ms)</Label>
            <Input className="w-28" value={timeout} onChange={(e) => setTimeoutMs(e.target.value)} aria-label="decider timeout" />
          </div>
          <div>
            <Label>Long context from (tokens)</Label>
            <Input className="w-28" value={longCtx} onChange={(e) => setLongCtx(e.target.value)} aria-label="long context threshold" />
          </div>
          <Button
            size="sm"
            variant="outline"
            onClick={() =>
              run(() =>
                api("/api/admin/router/settings", { method: "PUT", body: json({ timeout_ms: Number(timeout), long_context_tokens: Number(longCtx) }) }),
              )
            }
          >
            Save
          </Button>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">
        Classification first, optimization second: the router only chooses among the models a request may already use (cleared for its
        classification, enabled, within budgets and quotas, tool-capable when tools are offered). If it is disabled, fails, times out or returns
        anything unexpected, the default model and then the existing rules decide. Disabling is immediate; enabling and every change need
        re-authentication. A question the user sends with an explicit model choice is not routed.
      </p>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </Card>
  );
}

const EMPTY_DECIDER = {
  kind: "prompted" as Decider["kind"],
  name: "",
  model: "",
  base_url: "",
  api_key: "",
  self_hosted: false,
  max_classification: "",
  see_context: false,
  timeout_ms: "",
  monthly_budget_usd: "",
  daily_request_cap: "",
  input_usd_per_mtok: "",
  output_usd_per_mtok: "",
};

function AddDecider({ o }: { o: Overview }) {
  const { run, error } = useRun();
  const [f, setF] = useState(EMPTY_DECIDER);
  const set = (p: Partial<typeof EMPTY_DECIDER>) => setF({ ...f, ...p });
  const n = (v: string) => (v.trim() === "" ? undefined : Number(v));
  const submit = () =>
    run(async () => {
      await api("/api/admin/router/deciders", {
        method: "POST",
        body: json({
          name: f.name,
          kind: f.kind,
          ...(f.kind === "prompted" ? { model: f.model } : { base_url: f.base_url || undefined, model: f.model || undefined }),
          ...(f.kind !== "prompted" && f.api_key ? { api_key: f.api_key } : {}),
          self_hosted: f.kind === "protocol" && f.self_hosted,
          ...(f.max_classification ? { max_classification: f.max_classification } : {}),
          see_context: f.see_context,
          timeout_ms: n(f.timeout_ms),
          monthly_budget_usd: n(f.monthly_budget_usd),
          daily_request_cap: n(f.daily_request_cap),
          input_usd_per_mtok: n(f.input_usd_per_mtok),
          output_usd_per_mtok: n(f.output_usd_per_mtok),
        }),
      });
      setF(EMPTY_DECIDER);
    });
  return (
    <div className="mt-3 rounded-md border border-dashed border-border p-3">
      <p className="mb-2 text-xs font-medium">Add a decider</p>
      <div className="grid grid-cols-4 gap-2">
        <div>
          <Label>Kind</Label>
          <Select value={f.kind} onChange={(e) => set({ kind: e.target.value as Decider["kind"], model: "", base_url: "" })} aria-label="decider kind">
            {Object.entries(KIND_LABEL).map(([k, v]) => (
              <option key={k} value={k}>
                {v}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Name</Label>
          <Input value={f.name} onChange={(e) => set({ name: e.target.value })} aria-label="decider name" placeholder="decider name" />
        </div>
        {f.kind === "prompted" ? (
          <div className="col-span-2">
            <Label>Model (its clearance and budgets apply)</Label>
            <Select value={f.model} onChange={(e) => set({ model: e.target.value })} aria-label="decider model">
              <option value="">choose a model</option>
              {o.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id} ({m.locality}, up to {m.max_classification})
                </option>
              ))}
            </Select>
          </div>
        ) : (
          <>
            <div className="col-span-2">
              <Label>{f.kind === "jev_official" ? `Endpoint (default ${o.official.url})` : "Server URL (e.g. http://127.0.0.1:8791)"}</Label>
              <Input value={f.base_url} onChange={(e) => set({ base_url: e.target.value })} aria-label="decider URL" placeholder={f.kind === "jev_official" ? o.official.url : "http://127.0.0.1:8791"} />
            </div>
            <div>
              <Label>Model name {f.kind === "jev_official" ? `(default ${o.official.model})` : "(optional)"}</Label>
              <Input value={f.model} onChange={(e) => set({ model: e.target.value })} aria-label="decider model name" />
            </div>
            <div>
              <Label>API key (write-only, stored encrypted)</Label>
              <Input type="password" autoComplete="new-password" value={f.api_key} onChange={(e) => set({ api_key: e.target.value })} aria-label="decider API key" />
            </div>
          </>
        )}
        <div>
          <Label>Clearance (non-local: public by default, never confidential)</Label>
          <Select value={f.max_classification} onChange={(e) => set({ max_classification: e.target.value })} aria-label="decider clearance">
            <option value="">default</option>
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Timeout ms</Label>
          <Input value={f.timeout_ms} onChange={(e) => set({ timeout_ms: e.target.value })} placeholder={`${o.settings.timeout_ms}`} />
        </div>
        <div>
          <Label>Monthly budget USD / daily requests</Label>
          <div className="flex gap-1">
            <Input value={f.monthly_budget_usd} onChange={(e) => set({ monthly_budget_usd: e.target.value })} placeholder="default" />
            <Input value={f.daily_request_cap} onChange={(e) => set({ daily_request_cap: e.target.value })} placeholder="default" />
          </div>
        </div>
        {f.kind !== "prompted" && (
          <div>
            <Label>Price USD / 1M tokens (in / out)</Label>
            <div className="flex gap-1">
              <Input value={f.input_usd_per_mtok} onChange={(e) => set({ input_usd_per_mtok: e.target.value })} placeholder={f.kind === "jev_official" ? "0.042" : "0"} />
              <Input value={f.output_usd_per_mtok} onChange={(e) => set({ output_usd_per_mtok: e.target.value })} placeholder="0" />
            </div>
          </div>
        )}
        <div className="col-span-2 flex items-end gap-3 text-xs">
          {f.kind === "protocol" && (
            <label className="flex items-center gap-1" title="Allows a private/loopback address (a server you run)">
              <input type="checkbox" checked={f.self_hosted} onChange={(e) => set({ self_hosted: e.target.checked })} aria-label="decider self-hosted" />
              self-hosted
            </label>
          )}
          <label className="flex items-center gap-1" title="Only for local deciders: also send the retrieved context">
            <input type="checkbox" checked={f.see_context} onChange={(e) => set({ see_context: e.target.checked })} aria-label="decider sees context" />
            see context (local only)
          </label>
          <Button size="sm" onClick={() => void submit()} disabled={!f.name || (f.kind === "prompted" ? !f.model : f.kind === "protocol" && !f.base_url)}>
            Add decider
          </Button>
        </div>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </div>
  );
}

type TestResult = { ok: boolean; latency_ms: number; error?: string; model?: string; usage?: { input_tokens: number; output_tokens: number }; cost_usd?: number; answers?: Record<string, unknown> };

function DeciderRow({ d, first, last }: { d: Decider; first: boolean; last: boolean }) {
  const { run, error } = useRun();
  const [test, setTest] = useState<TestResult | null>(null);
  const base = `/api/admin/router/deciders/${d.id}`;
  const patch = (body: Record<string, unknown>) => run(() => api(base, { method: "PATCH", body: json(body) }));
  return (
    <tr className="border-t border-border align-top text-xs" data-decider={d.name}>
      <td>
        <div className="flex gap-0.5">
          <Button size="icon" variant="ghost" disabled={first} aria-label={`move ${d.name} up`} onClick={() => void patch({ position: Math.max(0, d.position - 15) })}>
            <ArrowUp className="h-3.5 w-3.5" />
          </Button>
          <Button size="icon" variant="ghost" disabled={last} aria-label={`move ${d.name} down`} onClick={() => void patch({ position: d.position + 15 })}>
            <ArrowDown className="h-3.5 w-3.5" />
          </Button>
        </div>
      </td>
      <td>
        <b>{d.name}</b>
        <div className="text-muted-foreground">{KIND_LABEL[d.kind]}</div>
        <div className="font-mono">{d.kind === "prompted" ? d.model : d.endpoint}</div>
      </td>
      <td>
        <Badge className={d.locality === "local" ? "border-emerald-300 bg-emerald-50 text-emerald-800" : "border-sky-300 bg-sky-50 text-sky-800"}>{d.locality}</Badge>
        <div className="mt-1 flex items-center gap-1">
          up to <ClassBadge value={d.max_classification} />
        </div>
        {d.see_context && <div>sees context</div>}
      </td>
      <td>
        <div>key: {d.kind === "prompted" ? "provider's" : d.key_source === "vault" ? "credential store" : "none"}</div>
        <div>
          today {d.requests_today}
          {d.daily_request_cap != null ? ` of ${d.daily_request_cap}` : ""} requests
        </div>
        <div>
          ${d.spent_this_month_usd.toFixed(4)}
          {d.monthly_budget_usd != null ? ` of $${d.monthly_budget_usd.toFixed(4)}` : ""} this month
        </div>
        {d.kind !== "prompted" && (
          <div title={d.price_is_default ? "Third-party figure, not from TypeSafe's documentation; check the first real call" : ""}>
            price {d.input_usd_per_mtok}/{d.output_usd_per_mtok} per 1M{d.price_is_default ? " (unverified default)" : ""}
          </div>
        )}
      </td>
      <td className={cn(d.available ? "text-emerald-700" : "text-amber-700")}>{d.enabled ? (d.available ? "available" : `unavailable: ${d.status_reason}`) : "disabled"}</td>
      <td className="whitespace-nowrap text-right">
        <Button size="sm" variant="outline" aria-label={`test ${d.name}`} onClick={() => run(async () => setTest(await api<TestResult>(`${base}/test`, { method: "POST" })))}>
          <PlugZap className="h-3.5 w-3.5" /> Test
        </Button>
        <Button size="sm" variant="outline" onClick={() => void patch({ enabled: !d.enabled })} aria-label={`${d.enabled ? "disable" : "enable"} ${d.name}`}>
          {d.enabled ? "Disable" : "Enable"}
        </Button>
        <Button size="sm" variant="ghost" aria-label={`delete decider ${d.name}`} onClick={() => confirm(`Delete decider ${d.name}? Its stored key is deleted too.`) && run(() => api(base, { method: "DELETE" }))}>
          <Trash2 className="h-4 w-4" />
        </Button>
        {test && (
          <p className={cn("mt-1 max-w-xs whitespace-normal text-left", test.ok ? "text-emerald-700" : "text-red-600")} data-testid={`decider-test-${d.name}`}>
            {test.ok
              ? `OK in ${test.latency_ms} ms; ${test.usage?.input_tokens ?? 0} in / ${test.usage?.output_tokens ?? 0} out tokens; $${(test.cost_usd ?? 0).toFixed(6)}; ${JSON.stringify(test.answers)}`
              : `Failed after ${test.latency_ms} ms: ${test.error}`}
          </p>
        )}
        {error && <p className="text-red-600">{error}</p>}
      </td>
    </tr>
  );
}

function Deciders({ o }: { o: Overview }) {
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Deciders</h3>
      <p className="mb-2 text-xs text-muted-foreground">
        Tried in order; the first that answers decides. A decider is skipped when the request's classification is above its clearance, when it is
        over budget, fails, times out or returns anything outside the expected format. The <b>rules</b> decider (deterministic: question length,
        context size, tools offered) is always last. Deciders receive the question and metadata only; retrieved context only if local and "see
        context" is on.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Order</Th>
            <Th>Decider</Th>
            <Th>Where / clearance</Th>
            <Th>Key and budget</Th>
            <Th>Status</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {o.deciders.map((d, i) => (
            <DeciderRow key={d.id} d={d} first={i === 0} last={i === o.deciders.length - 1} />
          ))}
          <tr className="border-t border-border text-xs text-muted-foreground" data-decider="rules">
            <td />
            <td>
              <b>rules</b> (built in, always last)
            </td>
            <td>in the app</td>
            <td>free</td>
            <td className="text-emerald-700">available</td>
            <td />
          </tr>
        </tbody>
      </Table>
      <AddDecider o={o} />
    </Card>
  );
}

const EMPTY_LANE = { name: "", task_types: [] as string[], difficulty_min: "", difficulty_max: "", needs_tools: "", needs_long_context: "", classifications: [] as string[], targets: [] as string[], is_default: false };

function describe(c: Record<string, unknown>) {
  const parts: string[] = [];
  if (Array.isArray(c.task_types)) parts.push(`task ${(c.task_types as string[]).join("/")}`);
  if (c.difficulty_min != null || c.difficulty_max != null) parts.push(`difficulty ${c.difficulty_min ?? 1}-${c.difficulty_max ?? 5}`);
  if (c.needs_tools != null) parts.push(c.needs_tools ? "needs tools" : "no tools");
  if (c.needs_long_context != null) parts.push(c.needs_long_context ? "long context" : "short context");
  if (Array.isArray(c.classifications)) parts.push((c.classifications as string[]).join("/"));
  return parts.join(", ") || "any request";
}

function Lanes({ o }: { o: Overview }) {
  const { run, error } = useRun();
  const [f, setF] = useState(EMPTY_LANE);
  const [pick, setPick] = useState("");
  const toggle = (list: string[], v: string) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v]);
  const tri = (v: string) => (v === "" ? undefined : v === "yes");
  const submit = () =>
    run(async () => {
      await api("/api/admin/router/lanes", {
        method: "POST",
        body: json({
          name: f.name,
          is_default: f.is_default,
          targets: f.targets,
          conditions: {
            ...(f.task_types.length ? { task_types: f.task_types } : {}),
            ...(f.difficulty_min ? { difficulty_min: Number(f.difficulty_min) } : {}),
            ...(f.difficulty_max ? { difficulty_max: Number(f.difficulty_max) } : {}),
            ...(tri(f.needs_tools) !== undefined ? { needs_tools: tri(f.needs_tools) } : {}),
            ...(tri(f.needs_long_context) !== undefined ? { needs_long_context: tri(f.needs_long_context) } : {}),
            ...(f.classifications.length ? { classifications: f.classifications } : {}),
          },
        }),
      });
      setF(EMPTY_LANE);
    });
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Lanes</h3>
      <p className="mb-2 text-xs text-muted-foreground">
        The first enabled lane whose conditions match the decider's answers sends the request to its first target that is permitted for this
        request; otherwise the default lane, then the default model, then the existing rules. A target that is not permitted (not cleared, over
        budget, not tool-capable) is simply skipped.
      </p>
      <Table>
        <thead>
          <tr>
            <Th>Lane</Th>
            <Th>When</Th>
            <Th>Targets (in order)</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {o.lanes.map((l) => (
            <tr key={l.id} className={cn("border-t border-border text-xs", !l.enabled && "opacity-60")} data-lane={l.name}>
              <td>
                <b>{l.name}</b> {l.is_default && <Badge className="border-amber-300 bg-amber-50 text-amber-800">default</Badge>}
              </td>
              <td>{l.is_default ? "when no other lane has a permitted target" : describe(l.conditions)}</td>
              <td className="font-mono">{l.targets.join(" > ")}</td>
              <td className="whitespace-nowrap text-right">
                <Button size="sm" variant="outline" onClick={() => run(() => api(`/api/admin/router/lanes/${l.id}`, { method: "PATCH", body: json({ enabled: !l.enabled }) }))}>
                  {l.enabled ? "Disable" : "Enable"}
                </Button>
                <Button size="sm" variant="ghost" aria-label={`delete lane ${l.name}`} onClick={() => run(() => api(`/api/admin/router/lanes/${l.id}`, { method: "DELETE" }))}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
      <div className="mt-3 space-y-2 rounded-md border border-dashed border-border p-3 text-xs">
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <Label>Lane name</Label>
            <Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} aria-label="lane name" placeholder="lane name" />
          </div>
          <div>
            <Label>Difficulty from / to (1-5)</Label>
            <div className="flex gap-1">
              <Input className="w-14" value={f.difficulty_min} onChange={(e) => setF({ ...f, difficulty_min: e.target.value })} aria-label="difficulty min" />
              <Input className="w-14" value={f.difficulty_max} onChange={(e) => setF({ ...f, difficulty_max: e.target.value })} aria-label="difficulty max" />
            </div>
          </div>
          <div>
            <Label>Needs tools</Label>
            <Select value={f.needs_tools} onChange={(e) => setF({ ...f, needs_tools: e.target.value })} aria-label="lane needs tools">
              <option value="">any</option>
              <option value="yes">yes</option>
              <option value="no">no</option>
            </Select>
          </div>
          <div>
            <Label>Needs long context</Label>
            <Select value={f.needs_long_context} onChange={(e) => setF({ ...f, needs_long_context: e.target.value })} aria-label="lane long context">
              <option value="">any</option>
              <option value="yes">yes</option>
              <option value="no">no</option>
            </Select>
          </div>
          <label className="flex items-center gap-1">
            <input type="checkbox" checked={f.is_default} onChange={(e) => setF({ ...f, is_default: e.target.checked })} aria-label="default lane" />
            default lane
          </label>
        </div>
        <div className="flex flex-wrap gap-3">
          <span className="font-medium">Task types:</span>
          {Object.keys(o.task_types).map((t) => (
            <label key={t} className="flex items-center gap-1" title={o.task_types[t]}>
              <input type="checkbox" checked={f.task_types.includes(t)} onChange={() => setF({ ...f, task_types: toggle(f.task_types, t) })} aria-label={`lane task ${t}`} />
              {t}
            </label>
          ))}
          <span className="ml-4 font-medium">Classifications:</span>
          {CLASSES.map((c) => (
            <label key={c} className="flex items-center gap-1">
              <input type="checkbox" checked={f.classifications.includes(c)} onChange={() => setF({ ...f, classifications: toggle(f.classifications, c) })} aria-label={`lane classification ${c}`} />
              {c}
            </label>
          ))}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <Label>Add target model</Label>
            <Select value={pick} onChange={(e) => setPick(e.target.value)} aria-label="lane target">
              <option value="">choose a model</option>
              {o.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id} ({m.locality}, up to {m.max_classification}
                  {m.supports_tools ? "" : ", no tools"})
                </option>
              ))}
            </Select>
          </div>
          <Button size="sm" variant="outline" disabled={!pick} onClick={() => (setF({ ...f, targets: [...f.targets.filter((t) => t !== pick), pick] }), setPick(""))}>
            Add target
          </Button>
          <span className="font-mono">{f.targets.join(" > ") || "no target yet"}</span>
          {f.targets.length > 0 && (
            <Button size="sm" variant="ghost" onClick={() => setF({ ...f, targets: [] })}>
              clear
            </Button>
          )}
          <Button size="sm" onClick={() => void submit()} disabled={!f.name || f.targets.length === 0}>
            Add lane
          </Button>
        </div>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
    </Card>
  );
}

type DryRunResult = {
  router_enabled: boolean;
  tools_offered: boolean;
  permitted: string[];
  decider: string;
  attempts: DecisionRow["attempts"];
  answers: Record<string, unknown> | null;
  lane: string | null;
  chosen_model: string | null;
  fallback_model: string | null;
  reason: string;
  latency_ms: number;
  cost_usd: number;
};

function DryRun({ o }: { o: Overview }) {
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const [f, setF] = useState({ question: "", classification: "public" as Classification, persona: "", context_tokens: "0", sample: false });
  const [out, setOut] = useState<DryRunResult | null>(null);
  const [error, setError] = useState("");
  const external = o.deciders.filter((d) => d.enabled && d.available && d.locality !== "local");
  const go = async () => {
    setError("");
    setOut(null);
    try {
      setOut(
        await api<DryRunResult>("/api/admin/router/dry-run", {
          method: "POST",
          body: json({
            question: f.question,
            classification: f.classification,
            persona_user_id: f.persona,
            context_tokens: Number(f.context_tokens || 0),
            sample_text_confirmed: f.sample,
          }),
        }),
      );
      await qc.invalidateQueries({ queryKey: ["router-decisions"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Card className="p-4">
      <h3 className="mb-1 flex items-center gap-1 text-sm font-semibold">
        <FlaskConical className="h-4 w-4" /> Dry run
      </h3>
      <p className="mb-2 text-xs text-muted-foreground">
        Which model would answer, for which persona and classification, without generating an answer. The question is not stored.
      </p>
      {external.length > 0 && (
        <p className="mb-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900">
          The text may be sent to a decider outside this network ({external.map((d) => d.name).join(", ")}) when the classification is within its
          clearance. Use sample text only.
        </p>
      )}
      <div className="grid grid-cols-4 gap-2">
        <div className="col-span-4">
          <Label>Question</Label>
          <Input value={f.question} onChange={(e) => setF({ ...f, question: e.target.value })} aria-label="dry run question" placeholder="sample question" />
        </div>
        <div>
          <Label>Classification</Label>
          <Select value={f.classification} onChange={(e) => setF({ ...f, classification: e.target.value as Classification })} aria-label="dry run classification">
            {CLASSES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Persona</Label>
          <Select value={f.persona} onChange={(e) => setF({ ...f, persona: e.target.value })} aria-label="dry run persona">
            <option value="">choose a user</option>
            {(users.data ?? []).map((u) => (
              <option key={u.id} value={u.id}>
                {u.email}
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label>Retrieved context tokens</Label>
          <Input value={f.context_tokens} onChange={(e) => setF({ ...f, context_tokens: e.target.value })} aria-label="dry run context tokens" />
        </div>
        <div className="flex items-end gap-2 text-xs">
          {external.length > 0 && (
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={f.sample} onChange={(e) => setF({ ...f, sample: e.target.checked })} aria-label="this is sample text" />
              this is sample text
            </label>
          )}
          <Button size="sm" disabled={!f.question || !f.persona} onClick={() => void go()}>
            Run
          </Button>
        </div>
      </div>
      {error && <p className="mt-2 text-sm text-red-600">{error}</p>}
      {out && (
        <div className="mt-3 space-y-1 text-xs" data-testid="dry-run-result">
          <p>
            Chosen: <b className="font-mono">{out.chosen_model ?? `(none: existing rules -> ${out.fallback_model})`}</b> - {out.reason}
            {out.lane ? ` (lane ${out.lane})` : ""}. Decider: <b>{out.decider}</b>, {out.latency_ms} ms, ${out.cost_usd.toFixed(6)}.
            {!out.router_enabled && " (The router is disabled: real chats are not routed yet.)"}
          </p>
          <p>
            Permitted for this request: <span className="font-mono">{out.permitted.join(", ") || "none"}</span>; tools offered: {out.tools_offered ? "yes" : "no"}
          </p>
          <p>Answers: {JSON.stringify(out.answers)}</p>
          <p>Attempts: {out.attempts.map((a) => `${a.decider} ${a.ok ? "ok" : `failed (${a.error})`}`).join("; ")}</p>
        </div>
      )}
    </Card>
  );
}

function DecisionLog() {
  const q = useQuery({ queryKey: ["router-decisions"], queryFn: () => api<DecisionRow[]>("/api/admin/router/decisions?limit=50") });
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Decision log</h3>
      <p className="mb-2 text-xs text-muted-foreground">Never stores the question or any context.</p>
      <Table>
        <thead>
          <tr>
            <Th>When (UTC)</Th>
            <Th>Class</Th>
            <Th>Permitted</Th>
            <Th>Decider</Th>
            <Th>Answers</Th>
            <Th>Chosen</Th>
            <Th>Reason</Th>
            <Th>ms / USD</Th>
          </tr>
        </thead>
        <tbody>
          {(q.data ?? []).map((d) => (
            <tr key={d.id} className="border-t border-border align-top text-xs" data-decision={d.id}>
              <td className="whitespace-nowrap">
                {d.ts.slice(0, 19).replace("T", " ")}
                {d.dry_run && <div className="text-muted-foreground">dry run</div>}
              </td>
              <td>
                <ClassBadge value={d.classification} />
              </td>
              <td className="font-mono">{d.permitted.join(", ")}</td>
              <td title={d.attempts.map((a) => `${a.decider}: ${a.ok ? "ok" : a.error}`).join("\n")}>{d.decider}</td>
              <td className="max-w-xs break-all font-mono">
                {d.answers.task_type ? `${d.answers.task_type}, difficulty ${d.answers.difficulty}, tools ${d.answers.needs_tools}, long ${d.answers.needs_long_context}` : "-"}
              </td>
              <td className="font-mono">{d.chosen_model ?? "-"}</td>
              <td>{d.reason}</td>
              <td>
                {d.latency_ms} / {d.cost_usd.toFixed(6)}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export default function ModelRouter() {
  const o = useOverview();
  if (!o.data) return <p className="text-sm text-muted-foreground">{o.error ? String(o.error) : "Loading the model router..."}</p>;
  return (
    <div className="space-y-4">
      <SettingsCard key={`${o.data.settings.timeout_ms}-${o.data.settings.long_context_tokens}`} o={o.data} />
      <Deciders o={o.data} />
      <Lanes o={o.data} />
      <DryRun o={o.data} />
      <DecisionLog />
    </div>
  );
}
