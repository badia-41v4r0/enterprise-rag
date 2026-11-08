// Content management: uploads with per-file status, versions, replace, retry and delete.
// Used by admins (all containers) and by users with a `contribute` or `manage` grant (their containers only).
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { History, RefreshCw, RotateCcw, Trash2, Upload } from "lucide-react";
import { Fragment, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, ClassBadge, Table, Th } from "@/components/ui/card";
import { api, json, type ContainerRow, type DocumentDetail, type DocumentRow } from "@/lib/api";
import { cn } from "@/lib/utils";

const ACCEPT = ".pdf,.docx,.xlsx,.html,.htm,.md,.markdown,.txt";

interface PendingFile {
  key: string;
  name: string;
  state: "uploading" | "queued" | "duplicate" | "error";
  message?: string;
}

function StatusPill({ status, error }: { status: string | null; error?: string | null }) {
  const color =
    status === "ready"
      ? "bg-green-100 text-green-800"
      : status === "failed" || status === "error"
        ? "bg-red-100 text-red-800"
        : "bg-amber-100 text-amber-800";
  return (
    <span className={cn("rounded px-1.5 py-0.5 text-xs", color)} title={error ?? undefined}>
      {status ?? "-"}
    </span>
  );
}

function Versions({ id }: { id: string }) {
  const detail = useQuery({
    queryKey: ["doc", id],
    queryFn: () => api<DocumentDetail>(`/api/documents/${id}`),
    refetchInterval: 3000,
  });
  if (!detail.data) return null;
  return (
    <div className="bg-muted/50 px-3 py-2 text-xs">
      <div className="mb-1 font-medium">Version history</div>
      <ul className="space-y-1">
        {[...detail.data.versions].reverse().map((v) => (
          <li key={v.id} className="flex flex-wrap items-center gap-2">
            <span>{new Date(v.created_at).toLocaleString()}</span>
            <StatusPill status={v.status} error={v.error} />
            {v.is_current && <span className="font-semibold">current</span>}
            <span className="font-mono text-muted-foreground">{v.sha256.slice(0, 12)}</span>
            <span>{(v.size_bytes / 1024).toFixed(1)} KB</span>
            {v.created_by && <span className="text-muted-foreground">by {v.created_by}</span>}
            {v.error && <span className="text-red-700">{v.error}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

function DocumentsTable({ container }: { container: ContainerRow }) {
  const qc = useQueryClient();
  const inv = () => qc.invalidateQueries({ queryKey: ["docs", container.id] });
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");
  const docs = useQuery({
    queryKey: ["docs", container.id],
    queryFn: () => api<DocumentRow[]>(`/api/containers/${container.id}/documents`),
    refetchInterval: 3000,
  });
  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
    inv();
  };
  const replace = (d: DocumentRow, file: File) =>
    run(() => {
      const fd = new FormData();
      fd.append("file", file);
      return api(`/api/documents/${d.id}/versions`, { method: "POST", body: fd });
    });
  const manage = !!container.can_manage;

  return (
    <>
      {error && <p className="px-3 py-1 text-sm text-red-600">{error}</p>}
      <Table>
        <thead>
          <tr>
            <Th>Title</Th>
            <Th>File</Th>
            <Th>Status</Th>
            <Th>Chunks</Th>
            <Th>Versions</Th>
            <Th>Restricted</Th>
            <Th>Actions</Th>
          </tr>
        </thead>
        <tbody>
          {docs.data?.map((d) => (
            <Fragment key={d.id}>
              <tr className="border-t border-border align-top" data-doc={d.filename}>
                <td>
                  {d.title}
                  {!d.can_read && (
                    <span className="ml-1 text-xs text-muted-foreground" title="You manage this document but cannot read it">
                      (no read access)
                    </span>
                  )}
                </td>
                <td className="font-mono text-xs">{d.filename}</td>
                <td>
                  <StatusPill status={d.latest_status} error={d.latest_error} />
                  {d.latest_status === "failed" && d.latest_error && (
                    <div className="mt-1 max-w-xs text-xs text-red-700">{d.latest_error}</div>
                  )}
                </td>
                <td>{d.chunk_count}</td>
                <td>{d.version_count}</td>
                <td>
                  <input
                    type="checkbox"
                    checked={d.restricted}
                    disabled={!manage}
                    onChange={(e) =>
                      run(() => api(`/api/documents/${d.id}`, { method: "PATCH", body: json({ restricted: e.target.checked }) }))
                    }
                    aria-label="restricted"
                  />
                </td>
                <td>
                  <div className="flex items-center gap-1">
                    <Button variant="ghost" size="icon" onClick={() => setOpen(open === d.id ? null : d.id)} aria-label="Versions" title="Version history">
                      <History className="h-4 w-4" />
                    </Button>
                    <label className="cursor-pointer rounded p-2 hover:bg-muted" title="Replace with a new version" aria-label="Replace">
                      <RefreshCw className="h-4 w-4" />
                      <input
                        type="file"
                        accept={ACCEPT}
                        className="hidden"
                        data-replace={d.filename}
                        onChange={(e) => {
                          const f = e.target.files?.[0];
                          if (f) replace(d, f);
                          e.target.value = "";
                        }}
                      />
                    </label>
                    {d.latest_status === "failed" && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => run(() => api(`/api/documents/${d.id}/retry`, { method: "POST" }))}
                        aria-label="Retry"
                        title="Retry ingestion"
                      >
                        <RotateCcw className="h-4 w-4" />
                      </Button>
                    )}
                    {manage && (
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => {
                          if (confirm(`Delete "${d.title}" and all its versions?`))
                            run(() => api(`/api/documents/${d.id}`, { method: "DELETE" }));
                        }}
                        aria-label="Delete document"
                        title="Delete"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
              {open === d.id && (
                <tr>
                  <td colSpan={7} className="!p-0">
                    <Versions id={d.id} />
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
          {docs.data?.length === 0 && (
            <tr>
              <td colSpan={7} className="text-sm text-muted-foreground">
                No documents yet.
              </td>
            </tr>
          )}
        </tbody>
      </Table>
    </>
  );
}

export function ContainerContent({ container }: { container: ContainerRow }) {
  const qc = useQueryClient();
  const [pending, setPending] = useState<PendingFile[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const uploadOne = async (file: File) => {
    const key = crypto.randomUUID();
    setPending((p) => [{ key, name: file.name, state: "uploading" }, ...p]);
    const set = (state: PendingFile["state"], message?: string) =>
      setPending((p) => p.map((x) => (x.key === key ? { ...x, state, message } : x)));
    try {
      const fd = new FormData();
      fd.append("file", file);
      if (restricted) fd.append("restricted", "true");
      const r = await api<{ status: string }>(`/api/containers/${container.id}/documents`, { method: "POST", body: fd });
      set(r.status === "duplicate" ? "duplicate" : "queued", r.status === "duplicate" ? "identical file already present" : undefined);
    } catch (e) {
      set("error", e instanceof Error ? e.message : String(e));
    }
    qc.invalidateQueries({ queryKey: ["docs", container.id] });
  };
  const uploadMany = (files: FileList | File[]) => Array.from(files).forEach((f) => void uploadOne(f));

  return (
    <Card className="p-4">
      <div className="mb-3 flex items-center gap-2">
        <h3 className="text-sm font-semibold">{container.name}</h3>
        <ClassBadge value={container.classification} />
        <span className="text-xs text-muted-foreground">
          {container.can_manage ? "you manage this container" : "you can upload and replace documents you can read"}
        </span>
      </div>
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          uploadMany(e.dataTransfer.files);
        }}
        className={cn(
          "mb-3 flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 text-sm",
          drag ? "border-brand bg-brand/5" : "border-border",
        )}
        data-testid="dropzone"
      >
        <Upload className="h-5 w-5 text-muted-foreground" />
        <span>Drop files here, or</span>
        <Button size="sm" variant="outline" onClick={() => input.current?.click()}>
          Choose files
        </Button>
        <input
          ref={input}
          type="file"
          multiple
          accept={ACCEPT}
          className="hidden"
          data-testid="upload-input"
          onChange={(e) => {
            if (e.target.files) uploadMany(e.target.files);
            e.target.value = "";
          }}
        />
        <span className="text-xs text-muted-foreground">PDF, DOCX, XLSX, HTML, Markdown, text</span>
        {container.can_manage && (
          <label className="flex items-center gap-1 text-xs">
            <input type="checkbox" checked={restricted} onChange={(e) => setRestricted(e.target.checked)} />
            upload as restricted (only roles with a document grant can read)
          </label>
        )}
      </div>
      {pending.length > 0 && (
        <ul className="mb-3 space-y-1 text-xs" aria-label="Upload queue">
          {pending.map((p) => (
            <li key={p.key} className="flex items-center gap-2">
              <span className="font-mono">{p.name}</span>
              <StatusPill status={p.state} />
              {p.message && <span className={p.state === "error" ? "text-red-700" : "text-muted-foreground"}>{p.message}</span>}
            </li>
          ))}
        </ul>
      )}
      <DocumentsTable container={container} />
    </Card>
  );
}

export default function ContentPage() {
  const containers = useQuery({ queryKey: ["my-containers"], queryFn: () => api<ContainerRow[]>("/api/containers") });
  const mine = (containers.data ?? []).filter((c) => c.can_contribute || c.can_manage);
  const [selected, setSelected] = useState<string | null>(null);
  const current = mine.find((c) => c.id === selected) ?? mine[0];
  return (
    <div className="h-full overflow-y-auto p-6">
      <h2 className="mb-1 text-lg font-semibold">Content</h2>
      <p className="mb-4 text-sm text-muted-foreground">
        Containers where you can upload documents. Access to them (who can read) is decided by the administrator.
      </p>
      {mine.length === 0 && <p className="text-sm text-muted-foreground">You cannot upload to any container.</p>}
      <div className="mb-4 flex flex-wrap gap-2">
        {mine.map((c) => (
          <Button key={c.id} size="sm" variant={c.id === current?.id ? "outline" : "ghost"} onClick={() => setSelected(c.id)}>
            {c.name}
          </Button>
        ))}
      </div>
      {current && <ContainerContent key={current.id} container={current} />}
    </div>
  );
}
