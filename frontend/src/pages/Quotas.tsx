// Admin: quotas per role (standard cap per member, collective pool) and per user (override), and usage views
// (ADR 0027). Admins see aggregates and per-user figures, never prompts or answers.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { Card, Table, Th } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import { api, json, type AdminUsage, type MyUsage, type ProvidersList, type QuotaLimits, type QuotaRow, type RoleRow, type UserRow } from "@/lib/api";
import { UsageBar } from "@/pages/Providers";

const DIMS: { key: keyof QuotaLimits; label: string }[] = [
  { key: "tokens_per_day", label: "Tokens / day" },
  { key: "tokens_per_month", label: "Tokens / month" },
  { key: "usd_per_day", label: "USD / day" },
  { key: "usd_per_month", label: "USD / month" },
];
const fmt = (k: keyof QuotaLimits) => (v: number) => (k.startsWith("usd") ? `$${v.toFixed(4)}` : v.toLocaleString("en-US"));
const num = (v: string) => (v.trim() === "" ? null : Number(v));

function useQuotas() {
  return useQuery({ queryKey: ["quotas"], queryFn: () => api<QuotaRow[]>("/api/admin/quotas") });
}

function QuotaLine({ q }: { q: QuotaRow }) {
  const qc = useQueryClient();
  const [f, setF] = useState(Object.fromEntries(DIMS.map((d) => [d.key, q[d.key] == null ? "" : String(q[d.key])])) as Record<keyof QuotaLimits, string>);
  const [error, setError] = useState("");
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["quotas"] });
    void qc.invalidateQueries({ queryKey: ["limits-history"] });
    void qc.invalidateQueries({ queryKey: ["admin-usage"] });
  };
  const save = async () => {
    setError("");
    try {
      const body: Record<string, number | null> = {};
      for (const d of DIMS) if (num(f[d.key]) !== q[d.key]) body[d.key] = num(f[d.key]);
      await api(`/api/admin/quotas/${q.id}`, { method: "PATCH", body: json(body) });
      refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <tr className="border-t border-border align-top" data-quota={`${q.subject_name}:${q.kind}:${q.provider ?? "all"}`}>
      <td className="text-xs">
        <b>{q.kind === "collective" ? "Collective pool" : q.subject === "user" ? "Personal override" : "Standard cap per member"}</b>
        <div className="text-muted-foreground">{q.provider ? `provider ${q.provider}` : "all providers"}</div>
      </td>
      {DIMS.map((d) => (
        <td key={d.key} className="w-40">
          <Input value={f[d.key]} placeholder="no limit" onChange={(e) => setF({ ...f, [d.key]: e.target.value })} aria-label={`${d.label} of ${q.subject_name} ${q.kind}`} />
          {q.used && q[d.key] != null && <UsageBar label="used" used={q.used[d.key]} limit={q[d.key]} fmt={fmt(d.key)} />}
        </td>
      ))}
      <td className="whitespace-nowrap text-right">
        <Button size="sm" variant="outline" onClick={() => void save()}>
          Save
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label={`Delete quota ${q.subject_name} ${q.kind}`}
          onClick={() => void api(`/api/admin/quotas/${q.id}`, { method: "DELETE" }).then(refresh, (e) => setError(String(e)))}
        >
          <Trash2 className="h-4 w-4" />
        </Button>
        {error && <p className="text-xs text-red-600">{error}</p>}
      </td>
    </tr>
  );
}

function QuotaEditor({ subject, subjectId, name }: { subject: "user" | "role"; subjectId: string; name: string }) {
  const qc = useQueryClient();
  const quotas = useQuotas();
  const providers = useQuery({ queryKey: ["providers"], queryFn: () => api<ProvidersList>("/api/admin/providers") });
  const empty = { kind: "standard", provider: "", tokens_per_day: "", tokens_per_month: "", usd_per_day: "", usd_per_month: "" };
  const [f, setF] = useState(empty);
  const [error, setError] = useState("");
  const mine = (quotas.data ?? []).filter((q) => (subject === "user" ? q.user_id === subjectId : q.role_id === subjectId));
  const add = async () => {
    setError("");
    try {
      await api("/api/admin/quotas", {
        method: "POST",
        body: json({
          subject,
          ...(subject === "user" ? { user_id: subjectId } : { role_id: subjectId }),
          kind: subject === "user" ? "standard" : f.kind,
          provider: f.provider || null,
          ...Object.fromEntries(DIMS.map((d) => [d.key, num(f[d.key])])),
        }),
      });
      setF(empty);
      void qc.invalidateQueries({ queryKey: ["quotas"] });
      void qc.invalidateQueries({ queryKey: ["limits-history"] });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <div className="space-y-2" data-testid={`quotas-${subject}`}>
      <Table>
        <thead>
          <tr>
            <Th>Quota</Th>
            {DIMS.map((d) => (
              <Th key={d.key}>{d.label}</Th>
            ))}
            <Th />
          </tr>
        </thead>
        <tbody>
          {mine.map((q) => (
            <QuotaLine key={q.id} q={q} />
          ))}
        </tbody>
      </Table>
      {mine.length === 0 && <p className="text-xs text-muted-foreground">No quota for {name}.</p>}
      <div className="grid grid-cols-7 items-end gap-2">
        {subject === "role" ? (
          <div>
            <Label>Kind</Label>
            <Select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })} aria-label="quota kind">
              <option value="standard">standard cap per member</option>
              <option value="collective">collective pool</option>
            </Select>
          </div>
        ) : (
          <div className="text-xs text-muted-foreground">Personal override (replaces the role standard caps)</div>
        )}
        <div>
          <Label>Provider</Label>
          <Select value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value })} aria-label="quota provider">
            <option value="">all providers</option>
            {(providers.data?.providers ?? []).map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.slug}
              </option>
            ))}
          </Select>
        </div>
        {DIMS.map((d) => (
          <div key={d.key}>
            <Label>{d.label}</Label>
            <Input placeholder="no limit" value={f[d.key]} onChange={(e) => setF({ ...f, [d.key]: e.target.value })} aria-label={`new quota ${d.label}`} />
          </div>
        ))}
        <Button size="sm" onClick={() => void add()}>
          Add quota
        </Button>
      </div>
      {error && <p className="text-sm text-red-600">{error}</p>}
    </div>
  );
}

export function RoleQuotas({ roles }: { roles: RoleRow[] }) {
  const [roleId, setRoleId] = useState("");
  const role = roles.find((r) => r.id === roleId);
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Quotas per role</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        A <b>standard cap</b> gives every member the same allowance, counted per person; a person with several roles gets the highest standard cap among
        them, unless they have a personal override (Users tab). A <b>collective pool</b> is one allowance shared by everyone with the role; every pool
        of every role a person holds is charged, and a request is refused when any applicable cap is used up. Tokens are input plus output; USD is
        the estimated cost; days and months are UTC.
      </p>
      <Select value={roleId} onChange={(e) => setRoleId(e.target.value)} aria-label="quota role" className="mb-3 w-72">
        <option value="">choose a role</option>
        {roles.map((r) => (
          <option key={r.id} value={r.id}>
            {r.name}
          </option>
        ))}
      </Select>
      {role && <QuotaEditor subject="role" subjectId={role.id} name={role.name} />}
    </Card>
  );
}

export function UserQuotas({ users }: { users: UserRow[] }) {
  const [userId, setUserId] = useState("");
  const user = users.find((u) => u.id === userId);
  const usage = useQuery({
    queryKey: ["user-usage", userId],
    queryFn: () => api<MyUsage>(`/api/admin/users/${userId}/usage`),
    enabled: !!userId,
  });
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Personal quota and usage</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        A personal override replaces the standard caps of the user's roles (collective pools still apply). Figures only: prompts and answers are
        never shown here.
      </p>
      <Select value={userId} onChange={(e) => setUserId(e.target.value)} aria-label="quota user" className="mb-3 w-72">
        <option value="">choose a user</option>
        {users.map((u) => (
          <option key={u.id} value={u.id}>
            {u.email}
          </option>
        ))}
      </Select>
      {user && (
        <>
          <QuotaEditor subject="user" subjectId={user.id} name={user.email} />
          {usage.data && <UsageSummary data={usage.data} />}
        </>
      )}
    </Card>
  );
}

export function UsageSummary({ data }: { data: MyUsage }) {
  return (
    <div className="mt-3 space-y-2 text-sm" data-testid="usage-summary">
      <p>
        Today: {data.today.tokens.toLocaleString("en-US")} tokens, {data.today.requests} requests, ${data.today.cost_usd.toFixed(4)}. This month:{" "}
        {data.this_month.tokens.toLocaleString("en-US")} tokens, {data.this_month.requests} requests, ${data.this_month.cost_usd.toFixed(4)}.
      </p>
      {data.allowances.map((a, i) => (
        <div key={i} className="rounded-md border border-border p-2" data-allowance={a.kind}>
          <p className="text-xs font-medium">
            {a.kind === "collective" ? "Shared allowance" : "Your allowance"} ({a.source}
            {a.provider ? `, provider ${a.provider}` : ", all providers"})
          </p>
          <div className="grid grid-cols-2 gap-x-6">
            {DIMS.filter((d) => a.limits[d.key] != null).map((d) =>
              a.used ? (
                <UsageBar key={d.key} label={d.label} used={a.used[d.key]} limit={a.limits[d.key]} fmt={fmt(d.key)} />
              ) : (
                <p key={d.key} className="text-xs">
                  {d.label}: {fmt(d.key)(a.limits[d.key] as number)} shared,{" "}
                  {a.exhausted.includes(d.key) ? <b className="text-red-600">used up</b> : "available"}
                </p>
              ),
            )}
          </div>
        </div>
      ))}
      {data.allowances.length === 0 && <p className="text-xs text-muted-foreground">No allowance applies: no personal or role quota.</p>}
    </div>
  );
}

export function UsageOverview() {
  const q = useQuery({ queryKey: ["admin-usage"], queryFn: () => api<AdminUsage>("/api/admin/usage") });
  if (!q.data) return null;
  const money = (v: number) => `$${v.toFixed(4)}`;
  return (
    <Card className="p-4" data-testid="admin-usage">
      <h3 className="mb-2 text-sm font-semibold">
        Usage (today {q.data.day}, month {q.data.month}, UTC)
      </h3>
      <div className="grid grid-cols-2 gap-4">
        <Table>
          <thead>
            <tr>
              <Th>User</Th>
              <Th>Tokens today</Th>
              <Th>Tokens month</Th>
              <Th>USD month</Th>
            </tr>
          </thead>
          <tbody>
            {q.data.users.map((u) => (
              <tr key={u.user_id ?? "none"} className="border-t border-border text-xs">
                <td>{u.email}</td>
                <td>{u.today.tokens.toLocaleString("en-US")}</td>
                <td>{u.month.tokens.toLocaleString("en-US")}</td>
                <td>{money(u.month.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </Table>
        <Table>
          <thead>
            <tr>
              <Th>Collective pool</Th>
              <Th>Members</Th>
              <Th>Used today / month (tokens)</Th>
            </tr>
          </thead>
          <tbody>
            {q.data.pools.map((p) => (
              <tr key={p.id} className="border-t border-border text-xs">
                <td>
                  {p.subject_name}
                  {p.provider ? ` (${p.provider})` : ""}
                </td>
                <td>{p.members}</td>
                <td>
                  {p.used?.tokens_per_day.toLocaleString("en-US")} / {p.used?.tokens_per_month.toLocaleString("en-US")}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      </div>
    </Card>
  );
}
