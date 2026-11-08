// Admin: MCP registry. Register remote servers, review discovered tools, set clearance (ADR 0021).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { api, json, type Classification, type GrantRow, type RegistryServer, type RegistryTool, type RoleRow, type SecretRow } from "@/lib/api";

const CLASSES: Classification[] = ["public", "internal", "confidential"];

function ToolRow({ server, tool, roles, grants, run }: {
  server: RegistryServer;
  tool: RegistryTool;
  roles: RoleRow[];
  grants: GrantRow[];
  run: (fn: () => Promise<unknown>) => void;
}) {
  const [constraints, setConstraints] = useState(JSON.stringify(tool.arg_constraints ?? {}, null, 0));
  const patch = (body: object) => run(() => api(`/api/admin/registry/tools/${tool.id}`, { method: "PATCH", body: json(body) }));
  const grantFor = (roleId: string) => grants.find((g) => g.role_id === roleId && g.resource_type === "tool" && g.resource_id === tool.id);
  return (
    <tr className="border-t border-border align-top" data-tool={tool.name}>
      <td className="font-mono text-xs">
        {tool.name}
        {tool.needs_review && (
          <div className="mt-1 flex items-center gap-1 font-sans text-amber-800">
            <AlertTriangle className="h-3 w-3" /> review: {tool.review_reason}
          </div>
        )}
      </td>
      <td className="max-w-xs text-xs">
        {tool.description}
        {!server.builtin && tool.description_for_model !== tool.description && (
          <div className="mt-1 text-muted-foreground">model sees: {tool.description_for_model}</div>
        )}
      </td>
      <td>
        <Select value={tool.classification} onChange={(e) => patch({ classification: e.target.value })} className="w-32" aria-label={`label of ${tool.name}`}>
          {CLASSES.map((c) => (
            <option key={c}>{c}</option>
          ))}
        </Select>
      </td>
      <td>
        <input
          type="checkbox"
          checked={tool.enabled}
          disabled={!!tool.cannot_enable && !tool.enabled}
          title={tool.cannot_enable ?? (tool.enabled ? "enabled (approved definition)" : "enable = approve the current definition")}
          aria-label={`enable ${tool.name}`}
          onChange={(e) => patch({ enabled: e.target.checked })}
        />
      </td>
      <td className="text-xs">
        {roles.map((r) => {
          const g = grantFor(r.id);
          return (
            <label key={r.id} className="mr-2 inline-flex items-center gap-1">
              <input
                type="checkbox"
                checked={!!g}
                aria-label={`${r.name} may execute ${tool.name}`}
                onChange={() =>
                  run(() =>
                    g
                      ? api(`/api/admin/grants/${g.id}`, { method: "DELETE" })
                      : api("/api/admin/grants", {
                          method: "POST",
                          body: json({ role_id: r.id, resource_type: "tool", resource_id: tool.id, action: "execute" }),
                        }),
                  )
                }
              />
              {r.name}
            </label>
          );
        })}
      </td>
      <td>
        <Textarea value={constraints} onChange={(e) => setConstraints(e.target.value)} rows={2} className="w-56 font-mono text-[11px]" aria-label={`constraints of ${tool.name}`} />
        <Button
          size="sm"
          variant="ghost"
          onClick={() =>
            run(async () => {
              await patch({ arg_constraints: JSON.parse(constraints || "{}") });
            })
          }
        >
          Save constraints
        </Button>
      </td>
    </tr>
  );
}

export default function Registry() {
  const qc = useQueryClient();
  const servers = useQuery({ queryKey: ["registry"], queryFn: () => api<RegistryServer[]>("/api/admin/registry/servers") });
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => api<RoleRow[]>("/api/admin/roles") });
  const grants = useQuery({ queryKey: ["grants", "all"], queryFn: () => api<GrantRow[]>("/api/admin/grants") });
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretRow[]>("/api/admin/secrets") });
  const [form, setForm] = useState({
    name: "",
    url: "",
    auth_secret: "",
    max_classification: "public" as Classification,
    result_classification: "confidential" as Classification,
  });
  const [error, setError] = useState("");
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    qc.invalidateQueries({ queryKey: ["registry"] });
    qc.invalidateQueries({ queryKey: ["grants"] });
    qc.invalidateQueries({ queryKey: ["tools"] });
  };
  const register = () =>
    run(() =>
      api("/api/admin/registry/servers", {
        method: "POST",
        body: json({ ...form, auth_secret: form.auth_secret || null }),
      }),
    );

  return (
    <div className="space-y-4">
      <Card className="space-y-3 p-4">
        <h3 className="text-sm font-semibold">Register a remote MCP server (streamable HTTP)</h3>
        <p className="text-xs text-muted-foreground">
          It starts as <strong>pending</strong>: its tools are discovered but disabled, and nothing is offered to a model until you approve the server,
          enable tools one by one and grant them to roles. Clearance: the highest classification of conversation data that may be sent to it.
          Everything it returns is labeled with the result classification.
        </p>
        <div className="grid grid-cols-3 gap-2">
          <Input placeholder="name (e.g. acme-office)" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Input placeholder="https://mcp.example.com/mcp" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} />
          <Select value={form.auth_secret} onChange={(e) => setForm({ ...form, auth_secret: e.target.value })} aria-label="auth secret">
            <option value="">no authentication</option>
            {secrets.data?.map((s) => (
              <option key={s.name} value={s.name}>
                Bearer token from secret {s.name}
              </option>
            ))}
          </Select>
          <div>
            <Label>Clearance (max data sent)</Label>
            <Select value={form.max_classification} onChange={(e) => setForm({ ...form, max_classification: e.target.value as Classification })} aria-label="clearance">
              {CLASSES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </div>
          <div>
            <Label>Result classification (label on what it returns)</Label>
            <Select
              value={form.result_classification}
              onChange={(e) => setForm({ ...form, result_classification: e.target.value as Classification })}
              aria-label="result classification"
            >
              {CLASSES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
          </div>
          <div className="flex items-end">
            <Button size="sm" onClick={register} disabled={!form.name || !form.url}>
              Register and discover tools
            </Button>
          </div>
        </div>
      </Card>
      {error && <p className="text-sm text-red-600">{error}</p>}
      {servers.data?.map((s) => (
        <Card key={s.id} className="p-4" data-server={s.name}>
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{s.name}</h3>
            <span className="text-xs text-muted-foreground">
              {s.builtin ? "built-in (stdio, signed permission context)" : `${s.transport} ${s.url ?? ""}`}
            </span>
            <Select
              value={s.status}
              aria-label={`status of ${s.name}`}
              onChange={(e) => run(() => api(`/api/admin/registry/servers/${s.id}`, { method: "PATCH", body: json({ status: e.target.value }) }))}
              className="w-32"
            >
              <option value="pending">pending</option>
              <option value="approved">approved</option>
              <option value="disabled">disabled</option>
            </Select>
            <span className="text-xs">clearance</span>
            <Select
              value={s.max_classification}
              aria-label={`clearance of ${s.name}`}
              onChange={(e) =>
                run(() => api(`/api/admin/registry/servers/${s.id}`, { method: "PATCH", body: json({ max_classification: e.target.value }) }))
              }
              className="w-32"
            >
              {CLASSES.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </Select>
            {!s.builtin && (
              <>
                <span className="text-xs">results labeled</span>
                <ClassBadge value={s.result_classification} />
                <Button size="sm" variant="outline" onClick={() => run(() => api(`/api/admin/registry/servers/${s.id}/discover`, { method: "POST" }))}>
                  <RefreshCw className="h-3 w-3" /> Rediscover
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`Delete server ${s.name}`}
                  onClick={() => confirm(`Delete ${s.name}?`) && run(() => api(`/api/admin/registry/servers/${s.id}`, { method: "DELETE" }))}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </>
            )}
          </div>
          <Table>
            <thead>
              <tr>
                <Th>Tool</Th>
                <Th>Description</Th>
                <Th>Result label</Th>
                <Th>Enabled</Th>
                <Th>Roles (execute)</Th>
                <Th>Argument constraints (JSON)</Th>
              </tr>
            </thead>
            <tbody>
              {s.tools.map((t) => (
                <ToolRow key={t.id} server={s} tool={t} roles={roles.data ?? []} grants={grants.data ?? []} run={run} />
              ))}
            </tbody>
          </Table>
        </Card>
      ))}
    </div>
  );
}
