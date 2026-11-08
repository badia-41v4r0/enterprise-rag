// Admin: connectors that sync a folder or web pages into one container (ADR 0020).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Play, Search, Trash2, Zap } from "lucide-react";
import { Fragment, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, Table, Th } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import {
  api,
  json,
  type ContainerRow,
  type ConnectorRow,
  type PreviewResult,
  type SecretRow,
  type SyncRunRow,
} from "@/lib/api";

function Runs({ id }: { id: string }) {
  const runs = useQuery({
    queryKey: ["runs", id],
    queryFn: () => api<SyncRunRow[]>(`/api/admin/connectors/${id}/runs`),
    refetchInterval: 3000,
  });
  return (
    <div className="space-y-1 text-xs" data-testid="runs">
      {runs.data?.length === 0 && <div className="text-muted-foreground">No runs yet.</div>}
      {runs.data?.map((r) => (
        <div key={r.id}>
          <span className="font-medium">{r.status}</span> {new Date(r.created_at).toLocaleString()} ({r.triggered_by}
          {r.duration_ms != null ? `, ${r.duration_ms} ms` : ""}):{" "}
          {Object.entries(r.counts)
            .map(([k, v]) => `${k} ${v}`)
            .join(", ")}
          {r.errors.fatal && <div className="text-red-700">error: {r.errors.fatal}</div>}
          {(r.errors.items ?? []).slice(0, 10).map((e, i) => (
            <div key={i} className="text-amber-800">
              {e.item}: {e.error}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

export default function Connectors({ containers }: { containers: ContainerRow[] }) {
  const qc = useQueryClient();
  const inv = () => qc.invalidateQueries({ queryKey: ["connectors"] });
  const connectors = useQuery({
    queryKey: ["connectors"],
    queryFn: () => api<ConnectorRow[]>("/api/admin/connectors"),
    refetchInterval: 4000,
  });
  const kinds = useQuery({
    queryKey: ["connector-kinds"],
    queryFn: () => api<{ roots: string[]; warning: string }>("/api/admin/connector-kinds"),
  });
  const secrets = useQuery({ queryKey: ["secrets"], queryFn: () => api<SecretRow[]>("/api/admin/secrets") });
  const [form, setForm] = useState({
    name: "",
    kind: "folder" as "folder" | "web",
    container_id: "",
    path: "",
    urls: "",
    max_pages: "50",
    max_depth: "1",
    bearer_secret: "",
    schedule: "",
  });
  const [open, setOpen] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ id: string; data: PreviewResult } | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const create = useMutation({
    mutationFn: () => {
      const config =
        form.kind === "folder"
          ? { path: form.path }
          : {
              urls: form.urls.split(/\s+/).filter(Boolean),
              max_pages: Number(form.max_pages),
              max_depth: Number(form.max_depth),
              ...(form.bearer_secret ? { bearer_secret: form.bearer_secret } : {}),
            };
      return api("/api/admin/connectors", {
        method: "POST",
        body: json({
          name: form.name,
          kind: form.kind,
          container_id: form.container_id,
          config,
          secret_names: form.bearer_secret ? [form.bearer_secret] : [],
          schedule_minutes: form.schedule ? Number(form.schedule) : null,
        }),
      });
    },
    onSuccess: () => {
      setForm({ ...form, name: "", path: "", urls: "" });
      inv();
    },
  });
  const run = async (fn: () => Promise<unknown>, ok?: string) => {
    setError("");
    setMessage("");
    try {
      await fn();
      if (ok) setMessage(ok);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    inv();
  };
  const container = containers.find((c) => c.id === form.container_id);

  return (
    <div className="space-y-4">
      <Card className="space-y-3 p-4">
        <h3 className="text-sm font-semibold">New connector</h3>
        <div className="flex items-start gap-2 rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-900">
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span>{kinds.data?.warning}</span>
        </div>
        <div className="grid grid-cols-4 gap-2">
          <Input placeholder="connector name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          <Select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as "folder" | "web" })} aria-label="kind">
            <option value="folder">folder</option>
            <option value="web">web</option>
          </Select>
          <Select value={form.container_id} onChange={(e) => setForm({ ...form, container_id: e.target.value })} aria-label="target container">
            <option value="">target container...</option>
            {containers.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.classification})
              </option>
            ))}
          </Select>
          <Input
            placeholder="sync every N minutes (empty = manual)"
            value={form.schedule}
            onChange={(e) => setForm({ ...form, schedule: e.target.value })}
          />
        </div>
        {form.kind === "folder" ? (
          <div>
            <Label>Folder (below {kinds.data?.roots.join(", ") || "no root configured: set ERAG_CONNECTOR_ROOTS"})</Label>
            <Input placeholder="e.g. acme/engineering-wiki" value={form.path} onChange={(e) => setForm({ ...form, path: e.target.value })} />
          </div>
        ) : (
          <div className="grid grid-cols-4 gap-2">
            <div className="col-span-4">
              <Label>URLs (one per line). Private and loopback addresses are blocked unless allowlisted by the operator.</Label>
              <Textarea value={form.urls} onChange={(e) => setForm({ ...form, urls: e.target.value })} rows={3} />
            </div>
            <Input placeholder="max pages" value={form.max_pages} onChange={(e) => setForm({ ...form, max_pages: e.target.value })} />
            <Input placeholder="max depth" value={form.max_depth} onChange={(e) => setForm({ ...form, max_depth: e.target.value })} />
            <Select value={form.bearer_secret} onChange={(e) => setForm({ ...form, bearer_secret: e.target.value })} aria-label="bearer secret">
              <option value="">no authentication</option>
              {secrets.data?.map((s) => (
                <option key={s.name} value={s.name}>
                  Bearer token from secret {s.name}
                </option>
              ))}
            </Select>
          </div>
        )}
        {container && (
          <p className="text-xs">
            Documents will be readable by every role that can read <strong>{container.name}</strong> ({container.classification}).
          </p>
        )}
        <Button size="sm" onClick={() => create.mutate()} disabled={!form.name || !form.container_id}>
          Create connector
        </Button>
        {create.error && <p className="text-sm text-red-600">{create.error.message}</p>}
      </Card>
      {(message || error) && <p className={error ? "text-sm text-red-600" : "text-sm text-green-700"}>{error || message}</p>}
      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Kind</Th>
              <Th>Container</Th>
              <Th>Status</Th>
              <Th>Items</Th>
              <Th>Enabled</Th>
              <Th>Actions</Th>
            </tr>
          </thead>
          <tbody>
            {connectors.data?.map((c) => (
              <Fragment key={c.id}>
                <tr className="border-t border-border align-top" data-connector={c.name}>
                  <td>
                    {c.name}
                    <div className="font-mono text-[11px] text-muted-foreground">
                      {c.kind === "folder" ? String(c.config.path) : ((c.config.urls as string[]) ?? []).join(" ")}
                    </div>
                  </td>
                  <td>{c.kind}</td>
                  <td>{c.container_name}</td>
                  <td>
                    <span data-testid="connector-status">{c.status}</span>
                    {c.last_sync_at && <div className="text-[11px] text-muted-foreground">{new Date(c.last_sync_at).toLocaleString()}</div>}
                    {c.last_error && <div className="text-[11px] text-red-700">{c.last_error}</div>}
                  </td>
                  <td>{c.item_count}</td>
                  <td>
                    <input
                      type="checkbox"
                      checked={c.enabled}
                      aria-label="enabled"
                      onChange={(e) => run(() => api(`/api/admin/connectors/${c.id}`, { method: "PATCH", body: json({ enabled: e.target.checked }) }))}
                    />
                  </td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          run(async () => {
                            const r = await api<{ ok: boolean; message: string }>(`/api/admin/connectors/${c.id}/test`, { method: "POST" });
                            if (!r.ok) throw new Error(r.message);
                            setMessage(r.message);
                          })
                        }
                      >
                        <Zap className="h-3 w-3" /> Test
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() =>
                          run(async () => setPreview({ id: c.id, data: await api<PreviewResult>(`/api/admin/connectors/${c.id}/preview`, { method: "POST" }) }))
                        }
                      >
                        <Search className="h-3 w-3" /> Preview
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => run(() => api(`/api/admin/connectors/${c.id}/sync`, { method: "POST" }), "Sync queued.")}>
                        <Play className="h-3 w-3" /> Sync now
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setOpen(open === c.id ? null : c.id)}>
                        Runs
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        aria-label={`Delete connector ${c.name}`}
                        onClick={() => confirm(`Delete ${c.name} and the documents it imported?`) && run(() => api(`/api/admin/connectors/${c.id}`, { method: "DELETE" }))}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </td>
                </tr>
                {preview?.id === c.id && (
                  <tr>
                    <td colSpan={7} className="bg-muted/50 text-xs" data-testid="preview">
                      <div className="mb-1 font-medium">
                        Dry run (nothing imported):{" "}
                        {Object.entries(preview.data.counts)
                          .map(([k, v]) => `${k} ${v}`)
                          .join(", ")}
                      </div>
                      {preview.data.fatal && <div className="text-red-700">{preview.data.fatal}</div>}
                      {preview.data.items.slice(0, 50).map((i) => (
                        <div key={i.item}>
                          {i.action}: <span className="font-mono">{i.item}</span>
                        </div>
                      ))}
                      {preview.data.errors.slice(0, 20).map((e, k) => (
                        <div key={k} className="text-amber-800">
                          skipped {e.item}: {e.error}
                        </div>
                      ))}
                    </td>
                  </tr>
                )}
                {open === c.id && (
                  <tr>
                    <td colSpan={7} className="bg-muted/50">
                      <Runs id={c.id} />
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
