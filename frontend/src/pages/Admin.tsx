import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Trash2 } from "lucide-react";
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import {
  api,
  json,
  type AccessLevel,
  type AuditRow,
  type Classification,
  type ContainerRow,
  type DocumentRow,
  type EffectivePermissions,
  type GrantRow,
  type JobRow,
  type SecretRow,
  type RoleRow,
  type ToolRow,
  type UserRow,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import Connectors from "@/pages/Connectors";
import Providers from "@/pages/Providers";
import Registry from "@/pages/Registry";
import { ContainerContent } from "@/pages/Content";

const TABS = ["Users", "Roles", "Containers", "Grants", "Connectors", "MCP registry", "Tools", "Secrets", "Providers", "Audit", "Jobs"] as const;
type Tab = (typeof TABS)[number];
const CLASSIFICATIONS = ["public", "internal", "confidential"] as const;
const CLASSES: Classification[] = ["public", "internal", "confidential"];

function useInvalidate() {
  const qc = useQueryClient();
  return (...keys: string[]) => keys.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
}

function ErrorLine({ error }: { error: unknown }) {
  return error ? <p className="text-sm text-red-600">{error instanceof Error ? error.message : String(error)}</p> : null;
}

const useRoles = () => useQuery({ queryKey: ["roles"], queryFn: () => api<RoleRow[]>("/api/admin/roles") });
const useContainers = () =>
  useQuery({ queryKey: ["containers"], queryFn: () => api<ContainerRow[]>("/api/admin/containers") });
const useTools = () => useQuery({ queryKey: ["tools"], queryFn: () => api<ToolRow[]>("/api/admin/tools") });

// --- Users ---------------------------------------------------------------------------------------

function Users() {
  const inv = useInvalidate();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const roles = useRoles();
  const [form, setForm] = useState({ email: "", display_name: "", password: "", is_admin: false, role_ids: [] as string[] });
  const create = useMutation({
    mutationFn: () => api("/api/admin/users", { method: "POST", body: json(form) }),
    onSuccess: () => {
      setForm({ email: "", display_name: "", password: "", is_admin: false, role_ids: [] });
      inv("users");
    },
  });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: object }) =>
      api(`/api/admin/users/${id}`, { method: "PATCH", body: json(body) }),
    onSuccess: () => inv("users"),
  });
  const roleName = (id: string) => roles.data?.find((r) => r.id === id)?.name ?? id;
  const resetMfa = useMutation({
    mutationFn: (id: string) => api(`/api/admin/users/${id}/reset-mfa`, { method: "POST" }),
    onSuccess: () => inv("users"),
  });

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <h3 className="mb-3 text-sm font-semibold">Create user</h3>
        <div className="grid grid-cols-4 gap-2">
          <Input placeholder="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />
          <Input
            placeholder="display name"
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
          />
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
          {roles.data?.map((r) => (
            <label key={r.id} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={form.role_ids.includes(r.id)}
                onChange={(e) =>
                  setForm({
                    ...form,
                    role_ids: e.target.checked ? [...form.role_ids, r.id] : form.role_ids.filter((x) => x !== r.id),
                  })
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
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Email</Th>
              <Th>Name</Th>
              <Th>Roles</Th>
              <Th>Admin</Th>
              <Th>Active</Th>
              <Th>MFA</Th>
            </tr>
          </thead>
          <tbody>
            {users.data?.map((u) => (
              <tr key={u.id} className="border-t border-border align-top">
                <td>{u.email}</td>
                <td>{u.display_name}</td>
                <td>
                  <div className="flex flex-wrap gap-2">
                    {roles.data?.map((r) => (
                      <label key={r.id} className="flex items-center gap-1 text-xs">
                        <input
                          type="checkbox"
                          checked={u.role_ids.includes(r.id)}
                          onChange={(e) =>
                            patch.mutate({
                              id: u.id,
                              body: {
                                role_ids: e.target.checked ? [...u.role_ids, r.id] : u.role_ids.filter((x) => x !== r.id),
                              },
                            })
                          }
                        />
                        {roleName(r.id)}
                      </label>
                    ))}
                  </div>
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={u.is_admin}
                    onChange={(e) => patch.mutate({ id: u.id, body: { is_admin: e.target.checked } })}
                    aria-label="admin"
                  />
                </td>
                <td>
                  <input
                    type="checkbox"
                    checked={u.is_active}
                    onChange={(e) => patch.mutate({ id: u.id, body: { is_active: e.target.checked } })}
                    aria-label="active"
                  />
                </td>
                <td className="whitespace-nowrap text-xs">
                  {u.mfa_enabled ? "on" : "off"}
                  {u.mfa_enabled && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => {
                        if (confirm(`Reset MFA for ${u.email}? Their sessions end and they log in with the password only.`))
                          resetMfa.mutate(u.id);
                      }}
                    >
                      reset
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
        <div className="px-3 pb-2">
          <ErrorLine error={patch.error} />
          <ErrorLine error={resetMfa.error} />
        </div>
      </Card>
    </div>
  );
}

// --- Roles ---------------------------------------------------------------------------------------

const TEMPLATES = [
  { id: "", label: "no template" },
  { id: "reader", label: "Reader (read)" },
  { id: "contributor", label: "Contributor (read + upload)" },
  { id: "manager", label: "Manager (read + upload + delete)" },
];

function Roles() {
  const inv = useInvalidate();
  const roles = useRoles();
  const containers = useContainers();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const [form, setForm] = useState({ name: "", template: "", container_ids: [] as string[] });
  const [error, setError] = useState("");
  const [members, setMembers] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    inv("roles", "users", "grants");
  };
  const create = () =>
    run(async () => {
      await api("/api/admin/roles", {
        method: "POST",
        body: json({ name: form.name, template: form.template || null, container_ids: form.container_ids }),
      });
      setForm({ name: "", template: "", container_ids: [] });
    });
  const rename = (r: RoleRow) => {
    const name = prompt("New name", r.name);
    if (name && name !== r.name) run(() => api(`/api/admin/roles/${r.id}`, { method: "PATCH", body: json({ name }) }));
  };
  const clone = (r: RoleRow) => {
    const name = prompt("Name of the copy", `${r.name} (copy)`);
    if (name) run(() => api(`/api/admin/roles/${r.id}/clone`, { method: "POST", body: json({ name }) }));
  };
  const remove = (r: RoleRow) => {
    if (r.user_count === 0) {
      if (confirm(`Delete role ${r.name}?`)) run(() => api(`/api/admin/roles/${r.id}`, { method: "DELETE" }));
      return;
    }
    const others = roles.data?.filter((x) => x.id !== r.id) ?? [];
    const target = prompt(
      `${r.user_count} user(s) hold ${r.name}. Type the name of the role to move them to:\n${others.map((o) => o.name).join(", ")}`,
    );
    const to = others.find((o) => o.name === target);
    if (target && !to) setError(`No role named ${target}`);
    if (to) run(() => api(`/api/admin/roles/${r.id}?reassign_to=${to.id}`, { method: "DELETE" }));
  };
  const saveMembers = (r: RoleRow) => {
    const current = users.data?.filter((u) => u.role_ids.includes(r.id)).map((u) => u.id) ?? [];
    const add = picked.filter((id) => !current.includes(id));
    const removeIds = current.filter((id) => !picked.includes(id));
    run(() => api(`/api/admin/roles/${r.id}/members`, { method: "POST", body: json({ add, remove: removeIds }) }));
    setMembers(null);
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <h3 className="mb-3 text-sm font-semibold">Create role</h3>
        <div className="flex flex-wrap items-center gap-2">
          <Input placeholder="role name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="max-w-xs" />
          <Select value={form.template} onChange={(e) => setForm({ ...form, template: e.target.value })} className="w-64" aria-label="template">
            {TEMPLATES.map((t) => (
              <option key={t.id} value={t.id}>
                {t.label}
              </option>
            ))}
          </Select>
          <Button size="sm" onClick={create} disabled={!form.name}>
            Create role
          </Button>
        </div>
        {form.template && (
          <div className="mt-2 flex flex-wrap gap-3 text-sm">
            <span className="text-xs text-muted-foreground">on containers:</span>
            {containers.data?.map((c) => (
              <label key={c.id} className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={form.container_ids.includes(c.id)}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      container_ids: e.target.checked ? [...form.container_ids, c.id] : form.container_ids.filter((x) => x !== c.id),
                    })
                  }
                />
                {c.name}
              </label>
            ))}
          </div>
        )}
        <ErrorLine error={error} />
      </Card>
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Role</Th>
              <Th>Users</Th>
              <Th>Grants</Th>
              <Th>Actions</Th>
            </tr>
          </thead>
          <tbody>
            {roles.data?.map((r) => (
              <Fragment key={r.id}>
                <tr className="border-t border-border" data-role={r.name}>
                  <td>{r.name}</td>
                  <td>{r.user_count}</td>
                  <td>{r.grant_count}</td>
                  <td className="flex gap-1">
                    <Button variant="outline" size="sm" onClick={() => rename(r)}>
                      Rename
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => clone(r)}>
                      Clone
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setMembers(members === r.id ? null : r.id);
                        setPicked(users.data?.filter((u) => u.role_ids.includes(r.id)).map((u) => u.id) ?? []);
                      }}
                    >
                      Members
                    </Button>
                    <Button variant="ghost" size="icon" onClick={() => remove(r)} aria-label={`Delete ${r.name}`}>
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </td>
                </tr>
                {members === r.id && (
                  <tr>
                    <td colSpan={4} className="bg-muted/50">
                      <div className="flex flex-wrap gap-3 text-xs">
                        {users.data?.map((u) => (
                          <label key={u.id} className="flex items-center gap-1">
                            <input
                              type="checkbox"
                              checked={picked.includes(u.id)}
                              onChange={(e) => setPicked(e.target.checked ? [...picked, u.id] : picked.filter((x) => x !== u.id))}
                            />
                            {u.email}
                          </label>
                        ))}
                      </div>
                      <Button className="mt-2" size="sm" onClick={() => saveMembers(r)}>
                        Save members
                      </Button>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

// --- Containers and documents ---------------------------------------------------------------------

function Containers() {
  const inv = useInvalidate();
  const containers = useContainers();
  const [selected, setSelected] = useState<string | null>(null);
  const [form, setForm] = useState({ name: "", description: "", classification: "internal" as Classification });
  const create = useMutation({
    mutationFn: () => api("/api/admin/containers", { method: "POST", body: json(form) }),
    onSuccess: () => {
      setForm({ name: "", description: "", classification: "internal" });
      inv("containers");
    },
  });
  const patch = useMutation({
    mutationFn: ({ id, classification }: { id: string; classification: Classification }) =>
      api(`/api/admin/containers/${id}`, { method: "PATCH", body: json({ classification }) }),
    onSuccess: () => inv("containers"),
  });
  const current = containers.data?.find((c) => c.id === selected);
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <h3 className="mb-3 text-sm font-semibold">Create container</h3>
        <div className="flex gap-2">
          <Input placeholder="name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input
            placeholder="description"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
          <Select
            value={form.classification}
            onChange={(e) => setForm({ ...form, classification: e.target.value as Classification })}
            className="w-40"
          >
            {CLASSES.map((c) => (
              <option key={c}>{c}</option>
            ))}
          </Select>
          <Button size="sm" onClick={() => create.mutate()} disabled={!form.name}>
            Create
          </Button>
        </div>
        <ErrorLine error={create.error} />
      </Card>
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Slug</Th>
              <Th>Classification</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {containers.data?.map((c) => (
              <tr key={c.id} className={cn("border-t border-border", c.id === selected && "bg-muted")}>
                <td>{c.name}</td>
                <td className="font-mono text-xs">{c.slug}</td>
                <td>
                  <Select
                    value={c.classification}
                    onChange={(e) => patch.mutate({ id: c.id, classification: e.target.value as Classification })}
                    className="w-40"
                  >
                    {CLASSES.map((x) => (
                      <option key={x}>{x}</option>
                    ))}
                  </Select>
                </td>
                <td>
                  <Button variant="outline" size="sm" onClick={() => setSelected(c.id)}>
                    Documents
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
      {current && <ContainerContent key={current.id} container={{ ...current, can_manage: true, can_contribute: true }} />}
    </div>
  );
}

// --- Grants --------------------------------------------------------------------------------------

function Grants() {
  const inv = useInvalidate();
  const roles = useRoles();
  const containers = useContainers();
  const tools = useTools();
  const [roleId, setRoleId] = useState("");
  const [rtype, setRtype] = useState<GrantRow["resource_type"]>("container");
  const [docContainer, setDocContainer] = useState("");
  const [resourceId, setResourceId] = useState("");
  const [action, setAction] = useState<GrantRow["action"]>("read");
  const grants = useQuery({
    queryKey: ["grants", roleId],
    queryFn: () => api<GrantRow[]>(`/api/admin/grants${roleId ? `?role_id=${roleId}` : ""}`),
  });
  const docs = useQuery({
    queryKey: ["docs", docContainer],
    queryFn: () => api<DocumentRow[]>(`/api/containers/${docContainer}/documents`),
    enabled: rtype === "document" && !!docContainer,
  });
  const create = useMutation({
    mutationFn: () =>
      api("/api/admin/grants", {
        method: "POST",
        body: json({ role_id: roleId, resource_type: rtype, resource_id: resourceId, action }),
      }),
    onSuccess: () => inv("grants"),
  });
  const remove = useMutation({
    mutationFn: (id: string) => api(`/api/admin/grants/${id}`, { method: "DELETE" }),
    onSuccess: () => inv("grants"),
  });
  const actions: GrantRow["action"][] = rtype === "container" ? ["read", "contribute", "manage"] : rtype === "document" ? ["read"] : ["execute"];
  const label = (g: GrantRow) => {
    if (g.resource_type === "container") return containers.data?.find((c) => c.id === g.resource_id)?.name ?? g.resource_id;
    if (g.resource_type === "tool") return tools.data?.find((t) => t.id === g.resource_id)?.model_name ?? g.resource_id;
    return `document ${g.resource_id.slice(0, 8)}`;
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <div className="grid grid-cols-5 items-end gap-2">
          <div>
            <Label>Role</Label>
            <Select value={roleId} onChange={(e) => setRoleId(e.target.value)}>
              <option value="">all roles</option>
              {roles.data?.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label>Resource type</Label>
            <Select
              value={rtype}
              onChange={(e) => {
                const t = e.target.value as GrantRow["resource_type"];
                setRtype(t);
                setResourceId("");
                setAction(t === "tool" ? "execute" : "read");
              }}
            >
              <option value="container">container</option>
              <option value="document">document (restricted)</option>
              <option value="tool">tool</option>
            </Select>
          </div>
          <div>
            <Label>Resource</Label>
            {rtype === "container" && (
              <Select value={resourceId} onChange={(e) => setResourceId(e.target.value)}>
                <option value="">select...</option>
                {containers.data?.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            )}
            {rtype === "tool" && (
              <Select value={resourceId} onChange={(e) => setResourceId(e.target.value)}>
                <option value="">select...</option>
                {tools.data?.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.model_name}
                  </option>
                ))}
              </Select>
            )}
            {rtype === "document" && (
              <div className="flex gap-1">
                <Select value={docContainer} onChange={(e) => setDocContainer(e.target.value)}>
                  <option value="">container...</option>
                  {containers.data?.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </Select>
                <Select value={resourceId} onChange={(e) => setResourceId(e.target.value)}>
                  <option value="">document...</option>
                  {docs.data?.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.title}
                      {d.restricted ? " (restricted)" : ""}
                    </option>
                  ))}
                </Select>
              </div>
            )}
          </div>
          <div>
            <Label>Action</Label>
            <Select value={action} onChange={(e) => setAction(e.target.value as GrantRow["action"])}>
              {actions.map((a) => (
                <option key={a}>{a}</option>
              ))}
            </Select>
          </div>
          <Button size="sm" onClick={() => create.mutate()} disabled={!roleId || !resourceId}>
            Add grant
          </Button>
        </div>
        <ErrorLine error={create.error} />
      </Card>
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Role</Th>
              <Th>Type</Th>
              <Th>Resource</Th>
              <Th>Action</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {grants.data?.map((g) => (
              <tr key={g.id} className="border-t border-border">
                <td>{roles.data?.find((r) => r.id === g.role_id)?.name}</td>
                <td>{g.resource_type}</td>
                <td>{label(g)}</td>
                <td>{g.action}</td>
                <td>
                  <Button variant="ghost" size="icon" onClick={() => remove.mutate(g.id)} aria-label="Revoke grant">
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

// --- Grants matrix and effective permissions --------------------------------------------------------

const LEVELS: AccessLevel[] = ["none", "read", "contribute", "manage"];

function Matrix() {
  const inv = useInvalidate();
  const roles = useRoles();
  const containers = useContainers();
  const tools = useTools();
  const grants = useQuery({ queryKey: ["grants", "all"], queryFn: () => api<GrantRow[]>("/api/admin/grants") });
  const [error, setError] = useState("");
  const levelOf = (roleId: string, containerId: string): AccessLevel => {
    const actions = (grants.data ?? [])
      .filter((g) => g.role_id === roleId && g.resource_type === "container" && g.resource_id === containerId)
      .map((g) => g.action);
    return actions.includes("manage") ? "manage" : actions.includes("contribute") ? "contribute" : actions.includes("read") ? "read" : "none";
  };
  const toolGrant = (roleId: string, toolId: string) =>
    grants.data?.find((g) => g.role_id === roleId && g.resource_type === "tool" && g.resource_id === toolId);
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    inv("grants", "roles");
  };
  return (
    <Card className="p-4">
      <h3 className="mb-1 text-sm font-semibold">Grants matrix</h3>
      <p className="mb-3 text-xs text-muted-foreground">
        contribute = read + upload/replace; manage = contribute + delete and restricted flag. Grants and classification stay with the admin.
      </p>
      <ErrorLine error={error} />
      <Table>
        <thead>
          <tr>
            <Th>Resource</Th>
            {roles.data?.map((r) => (
              <Th key={r.id}>{r.name}</Th>
            ))}
          </tr>
        </thead>
        <tbody>
          {containers.data?.map((c) => (
            <tr key={c.id} className="border-t border-border" data-matrix-container={c.name}>
              <td className="whitespace-nowrap">
                {c.name} <ClassBadge value={c.classification} />
              </td>
              {roles.data?.map((r) => (
                <td key={r.id}>
                  <Select
                    value={levelOf(r.id, c.id)}
                    aria-label={`${r.name} on ${c.name}`}
                    onChange={(e) =>
                      run(() => api(`/api/admin/roles/${r.id}/containers/${c.id}`, { method: "PUT", body: json({ level: e.target.value }) }))
                    }
                    className="w-32"
                  >
                    {LEVELS.map((l) => (
                      <option key={l}>{l}</option>
                    ))}
                  </Select>
                </td>
              ))}
            </tr>
          ))}
          {tools.data?.map((t) => (
            <tr key={t.id} className="border-t border-border">
              <td className="font-mono text-xs">{t.model_name}</td>
              {roles.data?.map((r) => {
                const g = toolGrant(r.id, t.id);
                return (
                  <td key={r.id}>
                    <input
                      type="checkbox"
                      checked={!!g}
                      aria-label={`${r.name} executes ${t.model_name}`}
                      onChange={() =>
                        run(() =>
                          g
                            ? api(`/api/admin/grants/${g.id}`, { method: "DELETE" })
                            : api("/api/admin/grants", {
                                method: "POST",
                                body: json({ role_id: r.id, resource_type: "tool", resource_id: t.id, action: "execute" }),
                              }),
                        )
                      }
                    />
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function EffectivePreview() {
  const users = useQuery({ queryKey: ["users"], queryFn: () => api<UserRow[]>("/api/admin/users") });
  const [userId, setUserId] = useState("");
  const eff = useQuery({
    queryKey: ["effective", userId],
    queryFn: () => api<EffectivePermissions>(`/api/admin/users/${userId}/effective-permissions`),
    enabled: !!userId,
  });
  return (
    <Card className="p-4">
      <h3 className="mb-2 text-sm font-semibold">Effective permissions preview</h3>
      <Select value={userId} onChange={(e) => setUserId(e.target.value)} className="mb-3 max-w-sm" aria-label="preview user">
        <option value="">select a user...</option>
        {users.data?.map((u) => (
          <option key={u.id} value={u.id}>
            {u.email}
          </option>
        ))}
      </Select>
      {eff.data && (
        <div className="space-y-2 text-sm" data-testid="effective">
          <div>
            Roles: {eff.data.roles.join(", ") || "none"} {eff.data.is_admin && "(admin: management only, no data access)"}
            {!eff.data.is_active && " (inactive: no access)"}
          </div>
          <ul>
            {eff.data.containers.map((c) => (
              <li key={c.id}>
                {c.name} <ClassBadge value={c.classification} />: <strong>{c.level}</strong>
              </li>
            ))}
            {eff.data.containers.length === 0 && <li className="text-muted-foreground">no containers</li>}
          </ul>
          <div>Restricted documents: {eff.data.restricted_documents.map((d) => `${d.title} (${d.container})`).join(", ") || "none"}</div>
          <div>Tools: {eff.data.tools.join(", ") || "none"}</div>
        </div>
      )}
    </Card>
  );
}

// --- Tools, audit, jobs --------------------------------------------------------------------------

function Tools() {
  const inv = useInvalidate();
  const tools = useTools();
  const sync = useMutation({ mutationFn: () => api("/api/admin/tools/sync", { method: "POST" }), onSuccess: () => inv("tools") });
  const patch = useMutation({
    mutationFn: ({ id, body }: { id: string; body: object }) =>
      api(`/api/admin/tools/${id}`, { method: "PATCH", body: json(body) }),
    onSuccess: () => inv("tools"),
  });
  return (
    <Card className="p-4">
      <Button size="sm" variant="outline" onClick={() => sync.mutate()} disabled={sync.isPending}>
        <RefreshCw className="h-4 w-4" /> Sync tools from MCP servers
      </Button>
      <ErrorLine error={sync.error} />
      <Table className="mt-3">
        <thead>
          <tr>
            <Th>Tool</Th>
            <Th>Description</Th>
            <Th>Result classification (floor)</Th>
            <Th>Enabled</Th>
          </tr>
        </thead>
        <tbody>
          {tools.data?.map((t) => (
            <tr key={t.id} className="border-t border-border">
              <td className="font-mono text-xs">{t.model_name}</td>
              <td className="text-xs">{t.description}</td>
              <td>
                <Select
                  value={t.classification}
                  onChange={(e) => patch.mutate({ id: t.id, body: { classification: e.target.value } })}
                  className="w-40"
                >
                  {CLASSES.map((c) => (
                    <option key={c}>{c}</option>
                  ))}
                </Select>
              </td>
              <td>
                <input
                  type="checkbox"
                  checked={t.enabled}
                  onChange={(e) => patch.mutate({ id: t.id, body: { enabled: e.target.checked } })}
                  aria-label="enabled"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function Secrets() {
  const inv = useInvalidate();
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretRow[]>("/api/admin/secrets") });
  const [form, setForm] = useState({ name: "", value: "", description: "" });
  const save = useMutation({
    mutationFn: () => api("/api/admin/secrets", { method: "PUT", body: json(form) }),
    onSuccess: () => {
      setForm({ name: "", value: "", description: "" });
      inv("secrets");
    },
  });
  const remove = useMutation({
    mutationFn: (name: string) => api(`/api/admin/secrets/${encodeURIComponent(name)}`, { method: "DELETE" }),
    onSuccess: () => inv("secrets"),
  });
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <h3 className="mb-1 text-sm font-semibold">Add or replace a secret</h3>
        <p className="mb-3 text-xs text-muted-foreground">
          Encrypted at rest (AES-256-GCM) with the master key outside the database. Values are write-only: they are never shown again.
        </p>
        <div className="grid grid-cols-4 gap-2">
          <Input placeholder="name (e.g. erp_token)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input
            placeholder="value"
            type="password"
            autoComplete="new-password"
            value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
          />
          <Input placeholder="description" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} />
          <Button size="sm" onClick={() => save.mutate()} disabled={!form.name || !form.value}>
            Save secret
          </Button>
        </div>
        <ErrorLine error={save.error} />
      </Card>
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Version</Th>
              <Th>Updated</Th>
              <Th>Last used</Th>
              <Th>Used by</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {secrets.data?.map((x) => (
              <tr key={x.name} className="border-t border-border">
                <td className="font-mono text-xs">
                  {x.name}
                  <div className="font-sans text-muted-foreground">{x.description}</div>
                </td>
                <td>{x.version}</td>
                <td className="text-xs">{new Date(x.updated_at).toLocaleString()}</td>
                <td className="text-xs">{x.last_used_at ? `${new Date(x.last_used_at).toLocaleString()} (${x.last_used_by})` : "never"}</td>
                <td className="text-xs">{x.used_by.join(", ") || "-"}</td>
                <td>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() => confirm(`Delete secret ${x.name}?`) && remove.mutate(x.name)}
                    aria-label={`Delete secret ${x.name}`}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
        <div className="px-3 pb-2">
          <ErrorLine error={remove.error} />
        </div>
      </Card>
    </div>
  );
}

function Audit() {
  const [action, setAction] = useState("");
  const audit = useQuery({
    queryKey: ["audit", action],
    queryFn: () => api<AuditRow[]>(`/api/admin/audit?limit=200${action ? `&action=${encodeURIComponent(action)}` : ""}`),
    refetchInterval: 5000,
  });
  return (
    <Card className="p-4">
      <Input
        placeholder="filter by action prefix, e.g. chat, tool, llm, admin"
        value={action}
        onChange={(e) => setAction(e.target.value)}
        className="mb-3 max-w-sm"
      />
      <Table>
        <thead>
          <tr>
            <Th>Time</Th>
            <Th>Action</Th>
            <Th>Decision</Th>
            <Th>Resource</Th>
            <Th>Details</Th>
          </tr>
        </thead>
        <tbody>
          {audit.data?.map((a) => (
            <tr key={a.id} className="border-t border-border align-top">
              <td className="whitespace-nowrap text-xs">{new Date(a.ts).toLocaleString()}</td>
              <td className="font-mono text-xs">{a.action}</td>
              <td>
                <span className={cn("text-xs", a.decision === "deny" && "font-semibold text-red-600")}>{a.decision}</span>
              </td>
              <td className="font-mono text-xs">{a.resource}</td>
              <td className="max-w-md truncate font-mono text-[11px]" title={JSON.stringify(a.details)}>
                {JSON.stringify(a.details)}
              </td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function Jobs() {
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api<JobRow[]>("/api/admin/jobs"), refetchInterval: 3000 });
  return (
    <Card>
      <Table>
        <thead>
          <tr>
            <Th>Created</Th>
            <Th>Version</Th>
            <Th>State</Th>
            <Th>Attempts</Th>
            <Th>Error</Th>
          </tr>
        </thead>
        <tbody>
          {jobs.data?.map((j) => (
            <tr key={j.id} className="border-t border-border">
              <td className="text-xs">{new Date(j.created_at).toLocaleString()}</td>
              <td className="font-mono text-xs">{j.version_id.slice(0, 8)}</td>
              <td>{j.state}</td>
              <td>{j.attempts}</td>
              <td className="text-xs text-red-600">{j.error}</td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

export default function AdminPage() {
  const [tab, setTab] = useState<Tab>("Users");
  const containers = useContainers();
  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mb-4 flex gap-1 border-b border-border">
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              "-mb-px border-b-2 px-3 py-2 text-sm",
              t === tab ? "border-brand font-medium" : "border-transparent text-muted-foreground",
            )}
          >
            {t}
          </button>
        ))}
        <div className="ml-auto flex items-center gap-3 pb-1 text-xs text-muted-foreground" aria-label="Containers by classification">
          {CLASSIFICATIONS.map((k) => (
            <span key={k} className="flex items-center gap-1">
              <ClassBadge value={k} /> {containers.data?.filter((c) => c.classification === k).length ?? 0}
            </span>
          ))}
        </div>
      </div>
      {tab === "Users" && <Users />}
      {tab === "Roles" && <Roles />}
      {tab === "Containers" && <Containers />}
      {tab === "Grants" && (
        <div className="space-y-4">
          <Matrix />
          <EffectivePreview />
          <Grants />
        </div>
      )}
      {tab === "Tools" && <Tools />}
      {tab === "Connectors" && <Connectors containers={containers.data ?? []} />}
      {tab === "MCP registry" && <Registry />}
      {tab === "Secrets" && <Secrets />}
      {tab === "Providers" && <Providers />}
      {tab === "Audit" && <Audit />}
      {tab === "Jobs" && <Jobs />}
    </div>
  );
}
