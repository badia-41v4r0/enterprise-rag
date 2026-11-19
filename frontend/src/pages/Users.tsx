import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, Table, Th } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { api, json, type ImportResult, type RoleRow, type UserPage, type UserRow } from "@/lib/api";

type Filters = { q: string; role_id: string; status: string; never_logged_in: boolean };
type BulkAction = "add_role" | "remove_role" | "deactivate" | "activate" | "sign_out";

function ErrorLine({ error }: { error: unknown }) {
  return error ? <p className="text-sm text-red-600">{error instanceof Error ? error.message : String(error)}</p> : null;
}

function params(f: Filters): string {
  const p = new URLSearchParams();
  if (f.q) p.set("q", f.q);
  if (f.role_id) p.set("role_id", f.role_id);
  if (f.status) p.set("status", f.status);
  if (f.never_logged_in) p.set("never_logged_in", "true");
  return p.toString();
}

/** Users at company scale (R16): server-side search and filters, keyset pages, bulk actions, CSV in and out. */
export default function Users() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ["users"] });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => api<RoleRow[]>("/api/admin/roles") });
  const [filters, setFilters] = useState<Filters>({ q: "", role_id: "", status: "", never_logged_in: false });
  const query = params(filters);
  const pages = useInfiniteQuery({
    queryKey: ["users", "page", query],
    initialPageParam: "",
    queryFn: ({ pageParam }) =>
      api<UserPage>(`/api/admin/users/page?limit=50&${query}${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const users = useMemo(() => pages.data?.pages.flatMap((p) => p.items) ?? [], [pages.data]);
  const total = pages.data?.pages[0]?.total ?? 0;
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkRole, setBulkRole] = useState("");
  const roleName = (id: string) => roles.data?.find((r) => r.id === id)?.name ?? id;

  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: object }) => api(`/api/admin/users/${id}`, { method: "PATCH", body: json(body) }),
    onSuccess: invalidate,
  });
  const resetMfa = useMutation({
    mutationFn: (id: string) => api(`/api/admin/users/${id}/reset-mfa`, { method: "POST" }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/admin/users/${id}`, { method: "DELETE" }),
    onSuccess: invalidate,
  });
  const bulk = useMutation({
    mutationFn: (action: BulkAction) =>
      api<{ changed: number }>("/api/admin/users/bulk", {
        method: "POST",
        body: json({ action, user_ids: [...selected], role_id: bulkRole || null }),
      }),
    onSuccess: () => {
      setSelected(new Set());
      invalidate();
    },
  });

  const toggle = (id: string, on: boolean) =>
    setSelected((s) => {
      const next = new Set(s);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  const allShown = users.length > 0 && users.every((u) => selected.has(u.id));

  return (
    <div className="space-y-4">
      <CreateUser roles={roles.data ?? []} onCreated={invalidate} />
      <ImportUsers onApplied={invalidate} />
      <Card>
        <div className="flex flex-wrap items-center gap-2 p-3">
          <Input
            className="max-w-xs"
            placeholder="search name or email"
            value={filters.q}
            onChange={(e) => setFilters({ ...filters, q: e.target.value })}
            aria-label="search users"
          />
          <Select value={filters.role_id} onChange={(e) => setFilters({ ...filters, role_id: e.target.value })} aria-label="role filter">
            <option value="">any role</option>
            {roles.data?.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>
          <Select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })} aria-label="status filter">
            <option value="">any status</option>
            <option value="active">active</option>
            <option value="inactive">inactive</option>
          </Select>
          <label className="flex items-center gap-1 text-sm">
            <input
              type="checkbox"
              checked={filters.never_logged_in}
              onChange={(e) => setFilters({ ...filters, never_logged_in: e.target.checked })}
            />
            never logged in
          </label>
          <span className="text-xs text-muted-foreground">{total} users</span>
          <a className="ml-auto inline-flex items-center gap-1 text-sm underline" href={`/api/admin/users/export.csv?${query}`}>
            <Download className="h-4 w-4" /> Export CSV
          </a>
        </div>
        {selected.size > 0 && (
          <div className="flex flex-wrap items-center gap-2 border-t border-border bg-muted/40 p-3 text-sm">
            <span>{selected.size} selected</span>
            <Select value={bulkRole} onChange={(e) => setBulkRole(e.target.value)} aria-label="role for bulk action">
              <option value="">role…</option>
              {roles.data?.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </Select>
            <Button size="sm" variant="outline" disabled={!bulkRole} onClick={() => bulk.mutate("add_role")}>
              Add role
            </Button>
            <Button size="sm" variant="outline" disabled={!bulkRole} onClick={() => bulk.mutate("remove_role")}>
              Remove role
            </Button>
            <Button size="sm" variant="outline" onClick={() => bulk.mutate("activate")}>
              Activate
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                if (confirm(`Deactivate ${selected.size} users? Their sessions end now.`)) bulk.mutate("deactivate");
              }}
            >
              Deactivate
            </Button>
            <Button size="sm" variant="outline" onClick={() => bulk.mutate("sign_out")}>
              Sign out everywhere
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setSelected(new Set())}>
              Clear
            </Button>
            {bulk.data && <span className="text-xs">{bulk.data.changed} changed</span>}
            <ErrorLine error={bulk.error} />
          </div>
        )}
        <Table>
          <thead>
            <tr>
              <Th>
                <input
                  type="checkbox"
                  aria-label="select all shown"
                  checked={allShown}
                  onChange={(e) => users.forEach((u) => toggle(u.id, e.target.checked))}
                />
              </Th>
              <Th>Email</Th>
              <Th>Name</Th>
              <Th>Roles</Th>
              <Th>Admin</Th>
              <Th>Active</Th>
              <Th>MFA</Th>
              <Th>Last login</Th>
              <Th> </Th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <UserLine
                key={u.id}
                u={u}
                roles={roles.data ?? []}
                roleName={roleName}
                selected={selected.has(u.id)}
                onSelect={(on) => toggle(u.id, on)}
                onPatch={(body) => patch.mutate({ id: u.id, body })}
                onResetMfa={() => resetMfa.mutate(u.id)}
                onDelete={() => remove.mutate(u.id)}
              />
            ))}
          </tbody>
        </Table>
        <div className="flex items-center gap-3 px-3 pb-3">
          {pages.hasNextPage && (
            <Button size="sm" variant="outline" onClick={() => pages.fetchNextPage()} disabled={pages.isFetchingNextPage}>
              Load more
            </Button>
          )}
          <ErrorLine error={pages.error} />
          <ErrorLine error={patch.error} />
          <ErrorLine error={resetMfa.error} />
          <ErrorLine error={remove.error} />
        </div>
      </Card>
    </div>
  );
}

function UserLine(props: {
  u: UserRow;
  roles: RoleRow[];
  roleName: (id: string) => string;
  selected: boolean;
  onSelect: (on: boolean) => void;
  onPatch: (body: object) => void;
  onResetMfa: () => void;
  onDelete: () => void;
}) {
  const { u, roles } = props;
  return (
    <tr className="border-t border-border align-top">
      <td>
        <input type="checkbox" aria-label={`select ${u.email}`} checked={props.selected} onChange={(e) => props.onSelect(e.target.checked)} />
      </td>
      <td>{u.email}</td>
      <td>{u.display_name}</td>
      <td>
        <div className="flex flex-wrap gap-2">
          {roles.map((r) => (
            <label key={r.id} className="flex items-center gap-1 text-xs">
              <input
                type="checkbox"
                checked={u.role_ids.includes(r.id)}
                onChange={(e) =>
                  props.onPatch({ role_ids: e.target.checked ? [...u.role_ids, r.id] : u.role_ids.filter((x) => x !== r.id) })
                }
              />
              {props.roleName(r.id)}
            </label>
          ))}
        </div>
      </td>
      <td>
        <input type="checkbox" checked={u.is_admin} onChange={(e) => props.onPatch({ is_admin: e.target.checked })} aria-label="admin" />
      </td>
      <td>
        <input type="checkbox" checked={u.is_active} onChange={(e) => props.onPatch({ is_active: e.target.checked })} aria-label="active" />
      </td>
      <td className="whitespace-nowrap text-xs">
        {u.mfa_enabled ? "on" : "off"}
        {u.mfa_enabled && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              if (confirm(`Reset MFA for ${u.email}? Their sessions end and they log in with the password only.`)) props.onResetMfa();
            }}
          >
            reset
          </Button>
        )}
      </td>
      <td className="whitespace-nowrap text-xs">{u.last_login_at ? new Date(u.last_login_at).toLocaleString() : "never"}</td>
      <td className="whitespace-nowrap text-xs">
        {!u.is_active && (
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Delete user ${u.email}`}
            onClick={() => {
              if (confirm(`Permanently delete ${u.email}? Their conversations and personal quotas are removed. This cannot be undone.`))
                props.onDelete();
            }}
          >
            delete
          </Button>
        )}
      </td>
    </tr>
  );
}

function CreateUser({ roles, onCreated }: { roles: RoleRow[]; onCreated: () => void }) {
  const empty = { email: "", display_name: "", password: "", is_admin: false, role_ids: [] as string[] };
  const [form, setForm] = useState(empty);
  const create = useMutation({
    mutationFn: () => api("/api/admin/users", { method: "POST", body: json(form) }),
    onSuccess: () => {
      setForm(empty);
      onCreated();
    },
  });
  return (
    <Card className="p-4">
      <h3 className="mb-3 text-sm font-semibold">Create user</h3>
      <div className="grid grid-cols-1 gap-2 md:grid-cols-4">
        <Input placeholder="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
        <Input placeholder="display name" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} />
        <Input
          placeholder="password (12+ chars)"
          type="password"
          value={form.password}
          onChange={(e) => setForm({ ...form, password: e.target.value })}
        />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.is_admin} onChange={(e) => setForm({ ...form, is_admin: e.target.checked })} />
          admin
        </label>
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-sm">
        {roles.map((r) => (
          <label key={r.id} className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={form.role_ids.includes(r.id)}
              onChange={(e) =>
                setForm({ ...form, role_ids: e.target.checked ? [...form.role_ids, r.id] : form.role_ids.filter((x) => x !== r.id) })
              }
            />
            {r.name}
          </label>
        ))}
      </div>
      <Button className="mt-3" size="sm" onClick={() => create.mutate()} disabled={!form.email || !form.password}>
        Create
      </Button>
      <ErrorLine error={create.error} />
    </Card>
  );
}

function ImportUsers({ onApplied }: { onApplied: () => void }) {
  const [open, setOpen] = useState(false);
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const run = useMutation({
    mutationFn: (dry_run: boolean) => api<ImportResult>("/api/admin/users/import", { method: "POST", body: json({ csv, dry_run }) }),
    onSuccess: (r) => {
      setResult(r);
      if (r.applied) onApplied();
    },
  });
  const passwords = result?.rows.filter((r) => r.temporary_password) ?? [];
  if (!open)
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Upload className="mr-1 h-4 w-4" /> Import users from CSV
      </Button>
    );
  return (
    <Card className="space-y-3 p-4">
      <h3 className="text-sm font-semibold">Import users from CSV</h3>
      <p className="text-xs text-muted-foreground">
        Columns: <code>email</code>, <code>display_name</code>, <code>roles</code> (role names separated by <code>;</code>). Roles are
        added, never removed. Nobody becomes an administrator through a file. New users get a temporary password, shown once below.
      </p>
      <input
        type="file"
        accept=".csv,text/csv"
        onChange={async (e) => {
          const f = e.target.files?.[0];
          if (f) setCsv(await f.text());
          setResult(null);
        }}
      />
      <textarea
        className="h-28 w-full rounded border border-border bg-background p-2 font-mono text-xs"
        value={csv}
        onChange={(e) => {
          setCsv(e.target.value);
          setResult(null);
        }}
        placeholder={"email,display_name,roles\nana@acme.test,Ana,Sales;Employee"}
        aria-label="CSV content"
      />
      <div className="flex gap-2">
        <Button size="sm" variant="outline" disabled={!csv} onClick={() => run.mutate(true)}>
          Check
        </Button>
        <Button
          size="sm"
          disabled={!result || result.applied || result.errors.length > 0}
          onClick={() => run.mutate(false)}
        >
          Apply
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Close
        </Button>
      </div>
      <ErrorLine error={run.error} />
      {result && (
        <div className="space-y-2 text-sm">
          {result.errors.map((e) => (
            <p key={e} className="text-red-600">
              {e}
            </p>
          ))}
          {result.errors.length === 0 && (
            <p>
              {result.applied ? "Applied" : "Would apply"}: {result.created} new, {result.updated} updated,{" "}
              {result.rows.length - result.created - result.updated} unchanged.
            </p>
          )}
          {passwords.length > 0 && (
            <div className="rounded border border-amber-500 p-2">
              <p className="mb-1 text-xs font-semibold">Temporary passwords: copy them now, they are not shown again.</p>
              <pre className="whitespace-pre-wrap font-mono text-xs">
                {passwords.map((r) => `${r.email},${r.temporary_password}`).join("\n")}
              </pre>
            </div>
          )}
        </div>
      )}
    </Card>
  );
}
