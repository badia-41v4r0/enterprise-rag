import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Table, Th } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { api, json } from "@/lib/api";

// Admin > Retrieval (R14): ranking settings and the state of the local models. Filters and routing never
// depend on these values; they only change which authorized chunks rank first.

type Settings = Record<string, string | number>;
interface ModelRow {
  name: string;
  kind: string;
  size_mb: number;
  license: string;
  description: string;
  installed: boolean;
  status?: { loaded: boolean; error: string | null };
}
interface RetrievalView {
  settings: Settings;
  defaults: Settings;
  models: ModelRow[];
  warnings: string[];
}

const NUMBERS: [string, string][] = [
  ["final_k", "Chunks given to the model"],
  ["vector_k", "Vector candidates"],
  ["text_k", "Full-text candidates"],
  ["rrf_k", "Fusion constant (RRF k)"],
  ["vector_weight", "Vector weight"],
  ["text_weight", "Full-text weight"],
  ["rerank_candidates", "Candidates reranked"],
  ["rerank_min_score", "Reranker relevance floor"],
  ["vector_min_score", "Cosine floor without reranker"],
  ["text_min_rank", "Full-text floor without reranker"],
];

export default function Retrieval() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["retrieval"], queryFn: () => api<RetrievalView>("/api/admin/retrieval") });
  const [draft, setDraft] = useState<Settings>({});
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (q.data) setDraft(q.data.settings);
  }, [q.data]);
  const save = useMutation({
    mutationFn: (body: Settings) => api<RetrievalView>("/api/admin/retrieval", { method: "PUT", body: json(body) }),
    onSuccess: (d) => {
      setError(null);
      qc.setQueryData(["retrieval"], d);
    },
    onError: (e: Error) => setError(e.message),
  });
  if (!q.data) return <p className="text-sm text-muted-foreground">Loading...</p>;
  const rerankers = q.data.models.filter((m) => m.kind === "reranker");
  const set = (k: string, v: string | number) => setDraft((d) => ({ ...d, [k]: v }));

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <h2 className="text-base font-semibold">Retrieval</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Vector search and full-text search both apply the user's permissions; their results are fused, re-checked
          against the current permissions, then reranked inside this server. These settings change ranking only.
          Measure a change with <code>erag eval run --retrieval key=value</code> before applying it.
        </p>
        {q.data.warnings.map((w) => (
          <p key={w} className="mt-2 rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900">
            {w}
          </p>
        ))}
        <div className="mt-4 grid grid-cols-1 gap-3 md:grid-cols-3">
          <div>
            <Label htmlFor="mode">Mode</Label>
            <Select id="mode" value={String(draft.mode ?? "")} onChange={(e) => set("mode", e.target.value)}>
              <option value="hybrid">Hybrid (vector + full text)</option>
              <option value="vector">Vector only</option>
            </Select>
          </div>
          <div>
            <Label htmlFor="reranker">Reranker</Label>
            <Select id="reranker" value={String(draft.reranker ?? "")} onChange={(e) => set("reranker", e.target.value)}>
              <option value="">None</option>
              {rerankers.map((m) => (
                <option key={m.name} value={m.name}>
                  {m.name}
                  {m.installed ? "" : " (not installed)"}
                </option>
              ))}
            </Select>
          </div>
          {NUMBERS.map(([k, label]) => (
            <div key={k}>
              <Label htmlFor={k}>{label}</Label>
              <Input
                id={k}
                type="number"
                step="any"
                value={String(draft[k] ?? "")}
                onChange={(e) => set(k, e.target.value === "" ? "" : Number(e.target.value))}
              />
              <p className="mt-0.5 text-[11px] text-muted-foreground">default {String(q.data.defaults[k])}</p>
            </div>
          ))}
        </div>
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
        <div className="mt-4 flex gap-2">
          <Button onClick={() => save.mutate(draft)} disabled={save.isPending}>
            Save
          </Button>
          <Button variant="outline" onClick={() => setDraft(q.data.defaults)}>
            Reset to defaults
          </Button>
        </div>
      </Card>
      <Card className="p-4">
        <h3 className="text-sm font-semibold">Local models</h3>
        <p className="mt-1 text-sm text-muted-foreground">
          Installed on the server with <code>erag models install &lt;name&gt;</code> (pinned files, verified by hash).
          Nothing is downloaded from here.
        </p>
        <Table className="mt-2">
          <thead>
            <tr>
              <Th>Model</Th>
              <Th>Kind</Th>
              <Th>Size</Th>
              <Th>License</Th>
              <Th>State</Th>
            </tr>
          </thead>
          <tbody>
            {q.data.models.map((m) => (
              <tr key={m.name} className="border-t border-border">
                <td>
                  <div className="font-medium">{m.name}</div>
                  <div className="text-xs text-muted-foreground">{m.description}</div>
                </td>
                <td>{m.kind}</td>
                <td>{m.size_mb} MB</td>
                <td className="text-xs">{m.license}</td>
                <td>
                  {!m.installed ? (
                    <Badge className="border-border text-muted-foreground">not installed</Badge>
                  ) : m.status?.error ? (
                    <Badge className="border-red-300 bg-red-50 text-red-800">failed to load</Badge>
                  ) : (
                    <Badge className="border-emerald-300 bg-emerald-50 text-emerald-800">
                      {m.status?.loaded ? "loaded" : "installed"}
                    </Badge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </div>
  );
}
