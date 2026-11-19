// Guided first-run setup (PLAN-v0.4): the token-gated wizard (steps 0 to 11) and the step components, which the
// Setup assistant (Admin) reuses with the normal admin session (`base` = /api/admin/setup, `wizard` = false).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle, CircleSlash, Download, Loader2, XCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, ClassBadge } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { StepUpDialog } from "@/components/StepUp";
import {
  api,
  json,
  setCsrf,
  setSetupCsrf,
  type CheckResult,
  type ContainerRow,
  type EnvCheck,
  type Info,
  type Me,
  type ModelDownload,
  type ModelsStatus,
  type Organization,
  type RoleRow,
  type SetupProgress,
  type SetupStatus,
  type UserRow,
} from "@/lib/api";
import { bytes, strength, T } from "@/lib/texts";
import { cn } from "@/lib/utils";
import { MfaCard } from "@/pages/Account";
import { Containers, EffectivePreview, Matrix, Roles } from "@/pages/Admin";
import Connectors from "@/pages/Connectors";
import { ContainerContent } from "@/pages/Content";
import Providers from "@/pages/Providers";

export type StepProps = { base: string; wizard: boolean; onDone: () => void; me?: Me | null };

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

function ErrorLine({ error }: { error: string }) {
  return error ? <p className="text-sm text-red-600" role="alert">{error}</p> : null;
}

function useRun() {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, run, setError };
}

const STATUS_ICON = {
  ok: <CheckCircle2 className="h-4 w-4 text-green-600" aria-label="ok" />,
  warn: <Circle className="h-4 w-4 text-amber-500" aria-label="warning" />,
  fail: <XCircle className="h-4 w-4 text-red-600" aria-label="failed" />,
  info: <Circle className="h-4 w-4 text-slate-400" aria-label="info" />,
};

function Why({ index }: { index: number }) {
  const s = T.steps[index];
  return (
    <div className="mb-4">
      <h2 className="text-lg font-semibold">
        {index}. {s.title}
      </h2>
      <p className="max-w-3xl text-sm text-muted-foreground">{s.why}</p>
    </div>
  );
}

// --- Step 0: license -------------------------------------------------------------------------------

function LicenseStep({ base, onDone }: StepProps) {
  const lic = useQuery({
    queryKey: ["setup-license"],
    queryFn: () => api<{ text: string; sha256: string; accepted_at: string | null; accepted_by: string | null }>(`${base}/license`),
  });
  const [ticked, setTicked] = useState(false);
  const [name, setName] = useState("");
  const { error, busy, run } = useRun();
  const accept = () =>
    run(async () => {
      await api(`${base}/license`, { method: "POST", body: json({ accepted: ticked, name, text_sha256: lic.data?.sha256 }) });
      onDone();
    });
  return (
    <Card className="max-w-3xl space-y-3 p-5">
      <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded border border-border bg-muted p-3 font-sans text-sm" data-testid="license-text">
        {lic.data?.text}
      </pre>
      {lic.data?.accepted_at ? (
        <p className="text-sm text-green-700">
          Accepted by {lic.data.accepted_by} on {new Date(lic.data.accepted_at).toLocaleString()}.
        </p>
      ) : null}
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" checked={ticked} onChange={(e) => setTicked(e.target.checked)} />
        {T.license.checkbox}
      </label>
      <div className="max-w-sm">
        <Label htmlFor="lic-name">{T.license.name}</Label>
        <Input id="lic-name" value={name} onChange={(e) => setName(e.target.value)} />
      </div>
      <p className="text-xs text-muted-foreground">{T.license.note}</p>
      <ErrorLine error={error} />
      <Button onClick={accept} disabled={!ticked || !name.trim() || busy || !lic.data}>
        {T.nav.next}
      </Button>
    </Card>
  );
}

// --- Step 1: environment ---------------------------------------------------------------------------

export function EnvironmentStep({ base, wizard, onDone }: StepProps) {
  const env = useQuery({
    queryKey: ["setup-env", base],
    queryFn: () => api<{ checks: EnvCheck[]; summary: Record<string, number> }>(`${base}/environment`),
  });
  const fails = env.data?.summary.fail ?? 0;
  return (
    <Card className="max-w-4xl p-5">
      <ul className="divide-y divide-border" data-testid="env-checks">
        {env.data?.checks.map((c) => (
          <li key={c.id} className="flex gap-3 py-2 text-sm" data-check={c.id} data-status={c.status}>
            <span className="mt-0.5">{STATUS_ICON[c.status]}</span>
            <div>
              <div className="font-medium">{c.label}</div>
              <div className="text-muted-foreground">{c.detail}</div>
              {c.fix && <div className="mt-1 text-xs">Fix: {c.fix}</div>}
            </div>
          </li>
        ))}
      </ul>
      {env.isLoading && <p className="text-sm text-muted-foreground">Checking...</p>}
      <p className={cn("mt-3 text-sm", fails ? "text-amber-700" : "text-green-700")}>{env.data && (fails ? T.env.hasFailures : T.env.allGood)}</p>
      <div className="mt-3 flex gap-2">
        <Button variant="outline" onClick={() => env.refetch()} disabled={env.isFetching}>
          {T.env.recheck}
        </Button>
        {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
      </div>
    </Card>
  );
}

// --- Step 2: administrator -------------------------------------------------------------------------

function StrengthMeter({ pw }: { pw: string }) {
  const s = strength(pw);
  return (
    <div className="mt-1 flex items-center gap-2" aria-label="password strength">
      <div className="flex gap-1">
        {[1, 2, 3, 4].map((i) => (
          <span key={i} className={cn("h-1.5 w-8 rounded", i <= s ? (s >= 3 ? "bg-green-600" : "bg-amber-500") : "bg-slate-200")} />
        ))}
      </div>
      <span className="text-xs text-muted-foreground">{T.admin.strength[s]}</span>
    </div>
  );
}

function SignIn({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [mfa, setMfa] = useState(false);
  const { error, busy, run } = useRun();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      if (mfa) {
        const me = await api<Me>("/api/auth/mfa/verify", { method: "POST", body: json(/^\d{6}$/.test(code) ? { code } : { recovery_code: code }) });
        setCsrf(me.csrf_token);
        onSignedIn();
        return;
      }
      const me = await api<Me>("/api/auth/login", { method: "POST", body: json({ email, password }) });
      setCsrf(me.csrf_token);
      if (me.mfa_required) setMfa(true);
      else onSignedIn();
    });
  };
  return (
    <form onSubmit={submit} className="max-w-sm space-y-2">
      <p className="text-sm">{T.admin.signInInstead}</p>
      {mfa ? (
        <Input aria-label="Authenticator code" placeholder="123456" value={code} onChange={(e) => setCode(e.target.value)} />
      ) : (
        <>
          <Input aria-label="Email" placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          <Input aria-label="Password" type="password" placeholder="password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </>
      )}
      <ErrorLine error={error} />
      <Button type="submit" disabled={busy}>
        Sign in
      </Button>
    </form>
  );
}

function AdminStep({ onDone, me, status, onSignedIn }: StepProps & { status: SetupStatus; onSignedIn: () => void }) {
  const created = status.steps[2].status === "done";
  const [form, setForm] = useState({ email: "", display_name: "", password: "", repeat: "" });
  const { error, busy, run } = useRun();
  const create = () =>
    run(async () => {
      if (form.password !== form.repeat) throw new Error(T.password.mismatch);
      const m = await api<Me>("/api/setup/admin", {
        method: "POST",
        body: json({ email: form.email, display_name: form.display_name, password: form.password }),
      });
      setCsrf(m.csrf_token);
      onSignedIn();
    });
  if (created && !status.admin_signed_in) return <Card className="max-w-xl p-5"><SignIn onSignedIn={onSignedIn} /></Card>;
  if (created && me)
    return (
      <div className="space-y-3">
        <p className="text-sm text-green-700">
          {T.admin.created} ({me.email})
        </p>
        <p className="text-sm">{T.admin.mfaOffer}</p>
        <MfaCard me={me} />
        <Button onClick={onDone}>{T.nav.next}</Button>
      </div>
    );
  return (
    <Card className="max-w-xl space-y-2 p-5">
      <Label htmlFor="adm-email">Email</Label>
      <Input id="adm-email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoComplete="username" />
      <Label htmlFor="adm-name">Name</Label>
      <Input id="adm-name" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
      <Label htmlFor="adm-pw">Password (12 or more characters)</Label>
      <Input id="adm-pw" type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} autoComplete="new-password" />
      <StrengthMeter pw={form.password} />
      <Label htmlFor="adm-pw2">Repeat the password</Label>
      <Input id="adm-pw2" type="password" value={form.repeat} onChange={(e) => setForm({ ...form, repeat: e.target.value })} autoComplete="new-password" />
      <ErrorLine error={error} />
      <Button onClick={create} disabled={busy || !form.email || form.password.length < 12}>
        Create administrator
      </Button>
    </Card>
  );
}

// --- Step 3: installation type ---------------------------------------------------------------------

export function InstallTypeStep({ base, wizard, onDone }: StepProps) {
  const qc = useQueryClient();
  const progress = useQuery({ queryKey: ["setup-progress", base], queryFn: () => api<SetupProgress>(`${base}/progress`) });
  const [choice, setChoice] = useState<"empty" | "demo">("empty");
  const { error, busy, run } = useRun();
  const save = () =>
    run(async () => {
      await api(`${base}/install-type`, { method: "POST", body: json({ type: choice }) });
      qc.invalidateQueries({ queryKey: ["info"] });
      onDone();
    });
  if (!wizard) return <DemoRemoval base={base} />;
  return (
    <Card className="max-w-3xl space-y-3 p-5">
      {(["empty", "demo"] as const).map((k) => (
        <label key={k} className={cn("block cursor-pointer rounded border p-3", choice === k ? "border-brand bg-brand/5" : "border-border")}>
          <div className="flex items-center gap-2 text-sm font-medium">
            <input type="radio" name="install-type" checked={choice === k} onChange={() => setChoice(k)} />
            {k === "empty" ? T.installType.empty : T.installType.demo}
          </div>
          <p className="ml-6 text-sm text-muted-foreground">{k === "empty" ? T.installType.emptyWhy : T.installType.demoWhy}</p>
        </label>
      ))}
      {progress.data?.demo && <DemoRemoval base={base} />}
      {busy && choice === "demo" && <p className="text-sm text-muted-foreground">{T.installType.installing}</p>}
      <ErrorLine error={error} />
      <Button onClick={save} disabled={busy}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null} {T.nav.next}
      </Button>
    </Card>
  );
}

export function DemoRemoval({ base }: { base: string }) {
  const qc = useQueryClient();
  const inv = useQuery({
    queryKey: ["setup-demo", base],
    queryFn: () =>
      api<{ present: boolean; users: string[]; roles: string[]; containers: string[]; documents: number; connectors: string[]; mcp_servers: string[] }>(
        `${base}/demo`,
      ),
  });
  const [confirming, setConfirming] = useState(false);
  const { error, busy, run } = useRun();
  const remove = () =>
    run(async () => {
      await api(`${base}/demo`, { method: "DELETE" });
      setConfirming(false);
      qc.invalidateQueries();
    });
  if (!inv.data?.present) return <p className="text-sm text-muted-foreground">No demo data is installed.</p>;
  const d = inv.data;
  return (
    <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm">
      {!confirming ? (
        <Button variant="destructive" size="sm" onClick={() => setConfirming(true)}>
          {T.assistant.removeDemo}
        </Button>
      ) : (
        <div className="space-y-2" role="dialog" aria-label="Remove demo data">
          <p className="font-medium">{T.assistant.removeConfirm}</p>
          <ul className="list-disc pl-5" data-testid="demo-inventory">
            <li>Users: {d.users.join(", ") || "none"}</li>
            <li>Roles: {d.roles.join(", ") || "none"}</li>
            <li>Containers: {d.containers.join(", ") || "none"} ({d.documents} documents)</li>
            <li>Connectors: {d.connectors.join(", ") || "none"}</li>
            <li>MCP servers and their tools: {d.mcp_servers.join(", ") || "none"}</li>
          </ul>
          <div className="flex gap-2">
            <Button variant="destructive" size="sm" onClick={remove} disabled={busy}>
              Delete demo data
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setConfirming(false)}>
              Cancel
            </Button>
          </div>
        </div>
      )}
      <ErrorLine error={error} />
    </div>
  );
}

// --- Step 4: organization ------------------------------------------------------------------------------

export function OrganizationStep({ base, wizard, onDone }: StepProps) {
  const qc = useQueryClient();
  const org = useQuery({ queryKey: ["setup-org", base], queryFn: () => api<Organization>(`${base}/organization`) });
  const [name, setName] = useState<string | null>(null);
  const [cls, setCls] = useState<"public" | "internal" | null>(null);
  const { error, busy, run } = useRun();
  const current = org.data;
  const chosen = cls ?? (wizard && current?.cloud_max_source === "environment" ? "public" : (current?.cloud_max_classification as "public" | "internal" | undefined)) ?? "public";
  const save = () =>
    run(async () => {
      await api(`${base}/organization`, { method: "PUT", body: json({ company_name: name ?? current?.company_name ?? "", cloud_max_classification: chosen }) });
      qc.invalidateQueries({ queryKey: ["info"] });
      qc.invalidateQueries({ queryKey: ["setup-org"] });
      onDone();
    });
  return (
    <Card className="max-w-3xl space-y-3 p-5">
      <div className="max-w-sm">
        <Label htmlFor="org-name">{T.org.company}</Label>
        <Input id="org-name" value={name ?? current?.company_name ?? ""} onChange={(e) => setName(e.target.value)} />
      </div>
      <div>
        <h3 className="text-sm font-semibold">{T.org.policyTitle}</h3>
        {(["public", "internal"] as const).map((k) => (
          <label key={k} className="mt-1 flex items-center gap-2 text-sm">
            <input type="radio" name="cloud-max" checked={chosen === k} onChange={() => setCls(k)} />
            {k === "public" ? T.org.public : T.org.internal}
          </label>
        ))}
        <p className="mt-2 text-xs text-muted-foreground">
          {T.org.never} Current: <ClassBadge value={current?.cloud_max_classification ?? "public"} /> (from the {current?.cloud_max_source}).
        </p>
      </div>
      <ErrorLine error={error} />
      <Button onClick={save} disabled={busy}>
        {wizard ? T.nav.next : "Save"}
      </Button>
    </Card>
  );
}

// --- Step 5: models ---------------------------------------------------------------------------------

function DownloadProgress({ base, d, onFinished }: { base: string; d: ModelDownload; onFinished: () => void }) {
  const [state, setState] = useState(d);
  useEffect(() => {
    if (state.status !== "running") return;
    const t = setInterval(async () => {
      try {
        const n = await api<ModelDownload>(`${base}/models/download/${d.id}`);
        setState(n);
        if (n.status !== "running") onFinished();
      } catch {
        /* keep polling */
      }
    }, 1000);
    return () => clearInterval(t);
  }, [state.status, base, d.id, onFinished]);
  const pct = state.total ? Math.min(100, Math.round((state.completed / state.total) * 100)) : 0;
  return (
    <div className="space-y-1 text-sm" data-testid={`download-${state.model}`} data-status={state.status}>
      <div>
        {state.model}: {state.status} {state.status === "running" && `${pct}% (${bytes(state.completed)} of ${bytes(state.total || state.size_bytes)})`} - {state.message}
      </div>
      <div className="h-2 w-full max-w-md rounded bg-slate-200">
        <div className="h-2 rounded bg-brand" style={{ width: `${pct}%` }} />
      </div>
      {state.status === "running" && (
        <Button size="sm" variant="outline" onClick={async () => setState(await api<ModelDownload>(`${base}/models/download/${d.id}`, { method: "DELETE" }))}>
          {T.models.cancel}
        </Button>
      )}
    </div>
  );
}

export function ModelsStep({ base, wizard, onDone }: StepProps) {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["setup-models", base], queryFn: () => api<ModelsStatus>(`${base}/models`) });
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<{ model: string; size_bytes: number; source: string; already_installed: boolean } | null>(null);
  const [started, setStarted] = useState<ModelDownload[]>([]);
  const [tests, setTests] = useState<Record<string, string>>({});
  const { error, busy, run } = useRun();
  const refresh = () => qc.invalidateQueries({ queryKey: ["setup-models", base] });
  const ask = (model: string) =>
    run(async () => setPreview(await api(`${base}/models/download/preview`, { method: "POST", body: json({ model }) })));
  const confirm = () =>
    run(async () => {
      if (!preview) return;
      const d = await api<ModelDownload>(`${base}/models/download`, {
        method: "POST",
        body: json({ model: preview.model, size_bytes: preview.size_bytes, confirm: true }),
      });
      setStarted((s) => [d, ...s]);
      setPreview(null);
    });
  const choose = (model: string) =>
    run(async () => {
      await api(`${base}/models/chat`, { method: "POST", body: json({ model }) });
      refresh();
    });
  const test = (kind: "chat" | "embedding", model?: string) =>
    run(async () => {
      const r = await api<{ ok: boolean; latency_ms?: number; reply?: string; dimensions?: number; error?: string }>(`${base}/models/test`, {
        method: "POST",
        body: json({ kind, model }),
      });
      setTests((t) => ({
        ...t,
        [`${kind}:${model ?? ""}`]: r.ok ? `OK in ${r.latency_ms} ms${r.dimensions ? `, ${r.dimensions} dimensions` : ""}${r.reply ? `: "${r.reply}"` : ""}` : `Failed: ${r.error}`,
      }));
    });
  const confirmEmbedding = () => run(async () => { await api(`${base}/models/embedding/confirm`, { method: "POST" }); refresh(); });
  const d = st.data;
  const embeddingName = d?.embedding.model ?? "";
  return (
    <div className="max-w-4xl space-y-4">
      <Card className="space-y-2 p-5">
        <h3 className="text-sm font-semibold">{T.models.detect}</h3>
        {d && !d.reachable && <p className="text-sm text-red-600">{T.models.unreachable}</p>}
        {d?.reachable && (
          <ul className="text-sm" data-testid="installed-models">
            {d.installed.map((m) => (
              <li key={m.name} className="flex items-center gap-2 py-0.5" data-model={m.name}>
                <span className="font-mono text-xs">{m.name}</span>
                <span className="text-xs text-muted-foreground">{bytes(m.size)}</span>
                {m.name === d.chat.default_model && <span className="rounded bg-brand/10 px-1 text-xs">chat model</span>}
                {(m.name === embeddingName || m.name === `${embeddingName}:latest`) && <span className="rounded bg-slate-100 px-1 text-xs">embedding</span>}
                {m.name !== embeddingName && m.name !== `${embeddingName}:latest` && m.name !== d.chat.default_model && (
                  <Button size="sm" variant="outline" onClick={() => choose(m.name)} aria-label={`use ${m.name}`}>
                    {T.models.makeDefault}
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
      <Card className="space-y-2 p-5">
        <h3 className="text-sm font-semibold">{T.models.embedding}</h3>
        <p className="text-sm">
          <span className="font-mono">{d?.embedding.model}</span> ({d?.embedding.dimensions} dimensions):{" "}
          {d?.embedding.installed ? "installed" : "not installed"} {d?.embedding.confirmed && "- confirmed"}
        </p>
        <p className="text-xs text-muted-foreground">{d?.embedding.locked ? T.models.locked : T.models.embeddingFixed}</p>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" onClick={() => test("embedding")}>{T.models.test}</Button>
          {!d?.embedding.confirmed && <Button size="sm" variant="outline" onClick={confirmEmbedding} disabled={!d?.embedding.installed}>Confirm</Button>}
          {d && !d.embedding.installed && <Button size="sm" onClick={() => ask(d.embedding.model)}><Download className="h-4 w-4" /> {T.models.download}</Button>}
        </div>
        {tests["embedding:"] && <p className="text-xs" data-testid="test-embedding">{tests["embedding:"]}</p>}
      </Card>
      <Card className="space-y-2 p-5">
        <h3 className="text-sm font-semibold">{T.models.chat}</h3>
        <p className="text-sm">
          Current: <span className="font-mono">{d?.chat.default_model ?? `${d?.chat.environment_model} (from the environment)`}</span>{" "}
          {d && (d.chat.installed ? "installed" : "not installed")}
        </p>
        <Button size="sm" variant="outline" onClick={() => test("chat", d?.chat.default_model ?? d?.chat.environment_model)}>{T.models.test}</Button>
        {Object.entries(tests).filter(([k]) => k.startsWith("chat:")).map(([k, v]) => <p key={k} className="text-xs" data-testid="test-chat">{v}</p>)}
      </Card>
      <Card className="space-y-2 p-5">
        <h3 className="text-sm font-semibold">{T.models.downloadTitle}</h3>
        <p className="text-xs text-muted-foreground">Nothing is downloaded until you confirm. Suggestions (permissive licenses):</p>
        <ul className="text-sm">
          {d?.suggested.map((s) => (
            <li key={s.name} className="flex items-center gap-2 py-0.5">
              <span className="font-mono text-xs">{s.name}</span> <span className="text-xs text-muted-foreground">{s.purpose}, {s.license}: {s.note}</span>
              <Button size="sm" variant="outline" onClick={() => ask(s.name)} aria-label={`download ${s.name}`}>
                <Download className="h-4 w-4" /> {T.models.download}
              </Button>
            </li>
          ))}
        </ul>
        <div className="flex gap-2">
          <Input placeholder="model name, e.g. qwen3:4b" value={name} onChange={(e) => setName(e.target.value)} className="max-w-xs" aria-label="model to download" />
          <Button size="sm" onClick={() => ask(name)} disabled={!name || busy}>
            <Download className="h-4 w-4" /> {T.models.download}
          </Button>
        </div>
        {preview && (
          <div className="rounded border border-brand/40 bg-brand/5 p-3 text-sm" role="dialog" aria-label={T.models.confirmTitle}>
            <p className="font-medium">{T.models.confirmTitle}</p>
            <p>{T.models.confirmText(preview.model, bytes(preview.size_bytes), preview.source)}</p>
            {preview.already_installed && <p className="text-xs text-muted-foreground">Already installed: this would update it.</p>}
            <div className="mt-2 flex gap-2">
              <Button size="sm" onClick={confirm} disabled={busy}>Download {bytes(preview.size_bytes)}</Button>
              <Button size="sm" variant="ghost" onClick={() => setPreview(null)}>Cancel</Button>
            </div>
          </div>
        )}
        {[...started, ...(d?.downloads ?? []).filter((x) => !started.some((s) => s.id === x.id) && x.status === "running")].map((x) => (
          <DownloadProgress key={x.id} base={base} d={x} onFinished={refresh} />
        ))}
      </Card>
      <details className="rounded-lg border border-border bg-white p-4">
        <summary className="cursor-pointer text-sm font-semibold">{T.models.cloudTitle}</summary>
        <p className="my-2 text-sm text-muted-foreground">{T.models.cloudWhy}</p>
        <Providers />
      </details>
      <ErrorLine error={error} />
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </div>
  );
}

// --- Step 6: access structure ---------------------------------------------------------------------------

function useProgress(base: string, poll = false) {
  return useQuery({
    queryKey: ["setup-progress", base],
    queryFn: () => api<SetupProgress>(`${base}/progress`),
    refetchInterval: poll ? 3000 : false,
  });
}

export function AccessStep({ base, wizard, onDone }: StepProps) {
  const p = useProgress(base, true);
  const missing = [
    !p.data?.containers && "at least one container",
    !p.data?.roles && "at least one role",
    !p.data?.grants && "at least one grant (a role's access to a container)",
  ].filter(Boolean);
  return (
    <div className="space-y-4">
      {missing.length > 0 && p.data && (
        <p className="text-sm text-amber-700" data-testid="access-missing">
          {T.access.missing} {missing.join(", ")}.
        </p>
      )}
      {p.data?.demo && <p className="text-sm text-muted-foreground">The demo installed this structure; you can change it here.</p>}
      <p className="max-w-3xl text-sm text-muted-foreground">{T.access.containerHelp}</p>
      <Containers />
      <p className="max-w-3xl text-sm text-muted-foreground">{T.access.roleHelp}</p>
      <Roles />
      <Matrix />
      <p className="max-w-3xl text-sm text-muted-foreground">{T.access.previewHelp}</p>
      <EffectivePreview />
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </div>
  );
}

// --- Step 7: people ------------------------------------------------------------------------------------

export function PeopleStep({ base, wizard, onDone }: StepProps) {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => api<RoleRow[]>("/api/admin/roles") });
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const [form, setForm] = useState({ email: "", display_name: "", role_ids: [] as string[] });
  const [csv, setCsv] = useState("");
  const [created, setCreated] = useState<{ email: string; temporary_password: string | null }[]>([]);
  const [errors, setErrors] = useState<{ line: number; email: string; error: string }[]>([]);
  const { error, busy, run } = useRun();
  const add = () =>
    run(async () => {
      const r = await api<{ email: string; temporary_password: string | null }>(`${base}/users`, { method: "POST", body: json(form) });
      setCreated((c) => [...c, r]);
      setForm({ email: "", display_name: "", role_ids: [] });
      qc.invalidateQueries({ queryKey: ["users"] });
    });
  const importCsv = () =>
    run(async () => {
      const r = await api<{ created: { email: string; temporary_password: string }[]; errors: { line: number; email: string; error: string }[] }>(
        `${base}/users/import`,
        { method: "POST", body: json({ csv }) },
      );
      setCreated((c) => [...c, ...r.created]);
      setErrors(r.errors);
      qc.invalidateQueries({ queryKey: ["users"] });
    });
  const roleName = (id: string) => roles.data?.find((r) => r.id === id)?.name ?? "";
  return (
    <div className="max-w-4xl space-y-4">
      <Card className="space-y-2 p-5">
        <h3 className="text-sm font-semibold">{T.people.one}</h3>
        <div className="grid grid-cols-2 gap-2">
          <Input placeholder="email" aria-label="person email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <Input placeholder="name" aria-label="person name" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        </div>
        <div className="flex flex-wrap gap-3 text-sm">
          {roles.data?.map((r) => (
            <label key={r.id} className="flex items-center gap-1">
              <input
                type="checkbox"
                aria-label={`role ${r.name}`}
                checked={form.role_ids.includes(r.id)}
                onChange={(e) => setForm({ ...form, role_ids: e.target.checked ? [...form.role_ids, r.id] : form.role_ids.filter((x) => x !== r.id) })}
              />
              {r.name}
            </label>
          ))}
          {roles.data?.length === 0 && <span className="text-muted-foreground">No roles yet (step 6).</span>}
        </div>
        <Button size="sm" onClick={add} disabled={!form.email || busy}>Add person</Button>
      </Card>
      <Card className="space-y-2 p-5">
        <Label htmlFor="csv">{T.people.csv}</Label>
        <Textarea id="csv" rows={5} value={csv} onChange={(e) => setCsv(e.target.value)} placeholder={"email,name,role\nana@example.com,Ana,HR"} />
        <Button size="sm" onClick={importCsv} disabled={!csv.trim() || busy}>{T.people.import}</Button>
        {errors.length > 0 && (
          <ul className="text-sm text-red-600" data-testid="csv-errors">
            {errors.map((e) => (
              <li key={`${e.line}-${e.email}`}>Line {e.line} ({e.email || "?"}): {e.error}</li>
            ))}
          </ul>
        )}
      </Card>
      {created.length > 0 && (
        <Card className="space-y-1 border-amber-300 bg-amber-50 p-4 text-sm" data-testid="temp-passwords">
          <p className="font-medium">{T.people.tempShown}</p>
          <ul className="font-mono text-xs">
            {created.map((c) => (
              <li key={c.email} data-email={c.email}>
                {c.email}: {c.temporary_password ?? "(the password you typed)"}
              </li>
            ))}
          </ul>
        </Card>
      )}
      <Card className="p-4 text-sm">
        <h3 className="mb-2 font-semibold">People ({users.data?.length ?? 0})</h3>
        <ul>
          {users.data?.map((u) => (
            <li key={u.id}>
              {u.email} {u.is_admin && "(administrator)"} {u.role_ids.map(roleName).filter(Boolean).join(", ")} {u.must_change_password && <span className="text-xs text-muted-foreground">- must change password</span>}
            </li>
          ))}
        </ul>
      </Card>
      <ErrorLine error={error} />
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </div>
  );
}

// --- Step 8: knowledge ----------------------------------------------------------------------------------

export function KnowledgeStep({ base, wizard, onDone }: StepProps) {
  const containers = useQuery({ queryKey: ["containers"], queryFn: () => api<ContainerRow[]>("/api/admin/containers") });
  const p = useProgress(base, true);
  const [selected, setSelected] = useState("");
  const current = containers.data?.find((c) => c.id === selected);
  return (
    <div className="max-w-5xl space-y-4">
      <Card className="p-4 text-sm" data-testid="ingestion-progress">
        <h3 className="mb-1 font-semibold">{T.knowledge.progress}</h3>
        Documents: {p.data?.documents ?? 0} (ready {p.data?.documents_ready ?? 0}, in progress {p.data?.documents_pending ?? 0}, failed {p.data?.documents_failed ?? 0}); indexed passages: {p.data?.chunks ?? 0}
      </Card>
      <div className="max-w-sm">
        <Label htmlFor="kn-container">{T.knowledge.pick}</Label>
        <Select id="kn-container" value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">select a container...</option>
          {containers.data?.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name} ({c.classification})
            </option>
          ))}
        </Select>
      </div>
      {current && <ContainerContent key={current.id} container={{ ...current, can_manage: true, can_contribute: true }} />}
      <details className="rounded-lg border border-border bg-white p-4">
        <summary className="cursor-pointer text-sm font-semibold">Or add a folder or web connector</summary>
        <Connectors containers={containers.data ?? []} />
      </details>
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </div>
  );
}

// --- Step 9: does it work? -------------------------------------------------------------------------------

export function CheckStep({ base, wizard, onDone }: StepProps) {
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const [userId, setUserId] = useState("");
  const [question, setQuestion] = useState("");
  const [results, setResults] = useState<CheckResult[]>([]);
  const { error, busy, run } = useRun();
  const ask = () =>
    run(async () => {
      const r = await api<CheckResult>(`${base}/check`, { method: "POST", body: json({ user_id: userId, question }) });
      setResults((x) => [r, ...x]);
    });
  return (
    <div className="max-w-5xl space-y-4">
      <Card className="space-y-2 p-5">
        <div className="grid grid-cols-3 gap-2">
          <div>
            <Label htmlFor="ck-user">{T.check.user}</Label>
            <Select id="ck-user" value={userId} onChange={(e) => setUserId(e.target.value)}>
              <option value="">select a user...</option>
              {users.data?.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.email}
                </option>
              ))}
            </Select>
          </div>
          <div className="col-span-2">
            <Label htmlFor="ck-q">{T.check.question}</Label>
            <Input id="ck-q" value={question} onChange={(e) => setQuestion(e.target.value)} />
          </div>
        </div>
        <Button size="sm" onClick={ask} disabled={!userId || !question || busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null} {T.check.ask}
        </Button>
        {!wizard && <p className="text-xs text-muted-foreground">{T.check.hiddenAnswer}</p>}
      </Card>
      <ErrorLine error={error} />
      <div className="grid gap-3 md:grid-cols-2">
        {results.map((r, i) => (
          <Card key={i} className="space-y-1 p-4 text-sm" data-testid="check-result" data-user={r.user.email}>
            <div className="font-semibold">{r.user.email}</div>
            <div>Roles: {r.user.roles.join(", ") || "none"}</div>
            <div>
              May read: {r.effective.containers.map((c) => `${c.name} (${c.classification}, ${c.level})`).join(", ") || "nothing"}
            </div>
            <div>
              Sources: {r.sources.map((s) => `${s.title} (${s.container})`).join("; ") || "none"}
            </div>
            <div>
              Model: <span className="font-mono">{r.model ?? "none (no authorized context, no model called)"}</span>{" "}
              {r.classification && <ClassBadge value={r.classification} />}
            </div>
            {r.refusal && <div className="text-amber-700">Refused: {r.refusal}</div>}
            {r.error && <div className="text-red-600">{r.error}</div>}
            {r.answer && <div className="whitespace-pre-wrap rounded bg-muted p-2" data-testid="check-answer">{r.answer}</div>}
          </Card>
        ))}
      </div>
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </div>
  );
}

// --- Step 10: tools and router ----------------------------------------------------------------------------

export function ToolsStep({ wizard, onDone }: StepProps) {
  return (
    <Card className="max-w-3xl space-y-2 p-5 text-sm">
      <p>{T.tools.mcp}</p>
      <p>{T.tools.router}</p>
      <p className="text-muted-foreground">Both are in the admin area (Admin &gt; MCP registry, Admin &gt; Model router) after the setup.</p>
      {wizard && <Button onClick={onDone}>{T.nav.next}</Button>}
    </Card>
  );
}

// --- Step 11: review and finish ---------------------------------------------------------------------------

type Summary = {
  counts: SetupProgress;
  organization: Organization;
  checklist: { key: string; status: "ok" | "warn"; label: string; fix: string }[];
  environment: Record<string, number>;
  install_type: string | null;
  known_password_admins: string[];
  production: boolean;
};

export function ReviewStep({ base, wizard, onFinished }: StepProps & { onFinished?: (m: Me) => void }) {
  const sum = useQuery({ queryKey: ["setup-summary", base], queryFn: () => api<Summary>(`${base}/summary`) });
  const { error, busy, run } = useRun();
  const finish = () =>
    run(async () => {
      const me = await api<Me>(`${base}/finish`, { method: "POST" });
      setCsrf(me.csrf_token);
      setSetupCsrf("");
      onFinished?.(me);
    });
  const s = sum.data;
  return (
    <div className="max-w-4xl space-y-4">
      {s && (
        <Card className="space-y-1 p-5 text-sm" data-testid="review-summary">
          <h3 className="font-semibold">Summary</h3>
          <div>Installation: {s.install_type ?? "empty"}; company: {s.organization.company_name || "(not set)"}; cloud ceiling: {s.organization.cloud_max_classification}</div>
          <div>
            Administrators {s.counts.admins}, users {s.counts.users}, roles {s.counts.roles}, containers {s.counts.containers}, grants {s.counts.grants}, documents {s.counts.documents} ({s.counts.documents_ready} ready)
          </div>
          <div>Environment: {s.environment.ok ?? 0} ok, {s.environment.warn ?? 0} warnings, {s.environment.fail ?? 0} failed</div>
        </Card>
      )}
      {s && (
        <Card className="p-5">
          <h3 className="mb-2 text-sm font-semibold">Security checklist</h3>
          <ul className="space-y-1 text-sm" data-testid="security-checklist">
            {s.checklist.map((c) => (
              <li key={c.key} className="flex gap-2" data-item={c.key} data-status={c.status}>
                {STATUS_ICON[c.status]}
                <span>
                  {c.label}
                  {c.fix && <span className="block text-xs text-muted-foreground">{c.fix}</span>}
                </span>
              </li>
            ))}
          </ul>
          {s.install_type === "demo" && <p className="mt-2 text-sm text-amber-700">{T.finish.privateNetwork}</p>}
          {s.known_password_admins.length > 0 && <p className="mt-2 text-sm text-red-600">{T.finish.knownAdmin}</p>}
        </Card>
      )}
      <div className="flex gap-2">
        <a className="inline-flex h-9 items-center rounded-md border border-border px-4 text-sm hover:bg-muted" href={`${base}/report`} download="enterprise-rag-setup-report.md">
          {T.finish.report}
        </a>
        {wizard && (
          <Button onClick={finish} disabled={busy}>
            {T.finish.finish}
          </Button>
        )}
      </div>
      <ErrorLine error={error} />
    </div>
  );
}

// --- The wizard -------------------------------------------------------------------------------------------

function TokenScreen({ status, onOk }: { status: SetupStatus; onOk: () => void }) {
  const [token, setToken] = useState("");
  const { error, busy, run } = useRun();
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const r = await api<{ setup_csrf: string }>("/api/setup/token", { method: "POST", body: json({ token }) });
      setSetupCsrf(r.setup_csrf);
      onOk();
    });
  };
  return (
    <div className="flex h-full items-center justify-center bg-muted p-4">
      <Card className="w-full max-w-lg space-y-3 p-6">
        <h1 className="text-lg font-semibold">{T.wizardTitle}</h1>
        <h2 className="text-sm font-semibold">{T.token.title}</h2>
        <p className="text-sm text-muted-foreground">{T.token.why}</p>
        <p className="text-xs text-muted-foreground">{status.token_source === "init" ? T.token.initHint : T.token.fileHint(status.token_file)}</p>
        <form onSubmit={submit} className="space-y-2">
          <Label htmlFor="setup-token">{T.token.label}</Label>
          <Input id="setup-token" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" spellCheck={false} placeholder="XXXX-XXXX-XXXX-XXXX-XXXX-XXXX" />
          <ErrorLine error={error} />
          <Button type="submit" disabled={!token.trim() || busy}>
            {T.token.submit}
          </Button>
        </form>
        <p className="text-xs text-muted-foreground">{T.token.lost}</p>
        <p className="text-xs text-amber-700">{T.token.network}</p>
      </Card>
    </div>
  );
}

const STEP_ICON = {
  done: <CheckCircle2 className="h-4 w-4 text-green-600" />,
  skipped: <CircleSlash className="h-4 w-4 text-slate-400" />,
  pending: <Circle className="h-4 w-4 text-slate-300" />,
};

export default function SetupWizard({ info, onFinished }: { info: Info; onFinished: (m: Me) => void }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["setup-status"], queryFn: () => api<SetupStatus>("/api/setup/status") });
  const me = useQuery({
    queryKey: ["setup-me", status.data?.admin_signed_in],
    enabled: !!status.data?.admin_signed_in,
    queryFn: async () => {
      const m = await api<Me>("/api/auth/me");
      setCsrf(m.csrf_token);
      return m;
    },
  });
  const [index, setIndex] = useState<number | null>(null);
  const top = useRef<HTMLDivElement>(null);
  if (status.data?.setup_csrf) setSetupCsrf(status.data.setup_csrf);
  const refresh = () => qc.invalidateQueries({ queryKey: ["setup-status"] });
  if (status.isLoading || !status.data) return <div className="p-8 text-sm text-muted-foreground">Loading...</div>;
  const st = status.data;
  if (!st.session) return <TokenScreen status={st} onOk={refresh} />;
  const current = index ?? st.current_step;
  const go = (i: number) => {
    setIndex(i);
    top.current?.scrollTo(0, 0);
  };
  const mark = async (key: string, s: "done" | "skipped") => {
    await api(`/api/setup/steps/${key}`, { method: "POST", body: json({ status: s }) });
    await refresh();
    go(Math.min(current + 1, 11));
  };
  const key = T.steps[current].key;
  const needsAdmin = current >= 3 && !st.admin_signed_in;
  const props: StepProps = { base: "/api/setup", wizard: true, onDone: () => mark(key, "done"), me: me.data };
  const canSkip = current >= 3 && current <= 10;
  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-border px-4 py-2">
        <span className="font-semibold">{T.product}</span>
        <span className="text-xs text-muted-foreground">{info.company_name || "Setup"}</span>
        <span className="ml-auto text-xs text-amber-700">{T.token.network}</span>
      </header>
      {info.demo && <div className="bg-amber-100 px-4 py-1 text-center text-xs text-amber-900" role="status">{T.demoBanner}</div>}
      <div className="flex min-h-0 flex-1">
        <nav className="w-60 shrink-0 overflow-y-auto border-r border-border p-3" aria-label="Setup steps">
          <p className="mb-2 text-xs text-muted-foreground">{T.wizardIntro}</p>
          <ol className="space-y-0.5">
            {T.steps.map((s, i) => {
              const sst = st.steps[i]?.status ?? "pending";
              const reachable = i <= st.current_step || sst !== "pending";
              return (
                <li key={s.key}>
                  <button
                    className={cn("flex w-full items-center gap-2 rounded px-2 py-1 text-left text-sm", i === current ? "bg-muted font-medium" : "", !reachable && "opacity-50")}
                    onClick={() => reachable && go(i)}
                    disabled={!reachable}
                    data-step={s.key}
                    data-status={sst}
                  >
                    {STEP_ICON[sst]} {i}. {s.title}
                  </button>
                </li>
              );
            })}
          </ol>
        </nav>
        <main ref={top} className="min-h-0 flex-1 overflow-y-auto p-6" data-current-step={key}>
          <Why index={current} />
          {needsAdmin ? (
            <Card className="max-w-xl p-5">
              <SignIn onSignedIn={refresh} />
            </Card>
          ) : current === 0 ? (
            <LicenseStep {...props} onDone={async () => { await refresh(); go(1); }} />
          ) : current === 1 ? (
            <EnvironmentStep {...props} />
          ) : current === 2 ? (
            <AdminStep {...props} status={st} onSignedIn={async () => { await refresh(); await qc.invalidateQueries({ queryKey: ["setup-me"] }); }} onDone={() => go(3)} />
          ) : current === 3 ? (
            <InstallTypeStep {...props} onDone={async () => { await refresh(); await qc.invalidateQueries({ queryKey: ["info"] }); go(4); }} />
          ) : current === 4 ? (
            <OrganizationStep {...props} onDone={async () => { await refresh(); go(5); }} />
          ) : current === 5 ? (
            <ModelsStep {...props} />
          ) : current === 6 ? (
            <AccessStep {...props} />
          ) : current === 7 ? (
            <PeopleStep {...props} />
          ) : current === 8 ? (
            <KnowledgeStep {...props} />
          ) : current === 9 ? (
            <CheckStep {...props} />
          ) : current === 10 ? (
            <ToolsStep {...props} />
          ) : (
            <ReviewStep {...props} onFinished={onFinished} />
          )}
          <div className="mt-6 flex gap-2 border-t border-border pt-3">
            {current > 0 && (
              <Button variant="ghost" size="sm" onClick={() => go(current - 1)}>
                {T.nav.back}
              </Button>
            )}
            {canSkip && !needsAdmin && (
              <Button variant="ghost" size="sm" onClick={() => mark(key, "skipped")}>
                {T.nav.skip}
              </Button>
            )}
          </div>
        </main>
      </div>
      {me.data && <StepUpDialog mfa={me.data.mfa_enabled} />}
    </div>
  );
}
