import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, Table, Th } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { api, json } from "@/lib/api";

// Admin > Evaluation (R15, ADR 0040): company question sets, runs against this installation as real users, the
// comparison of two runs, applying a better retrieval configuration, and the review queue of user feedback.

interface SetRow {
  id: string;
  name: string;
  source: string;
  classification: string;
  items: number;
}
interface RunRow {
  id: string;
  set: string;
  label: string;
  status: string;
  started_at: string;
  metrics: Record<string, number>;
  retrieval: Record<string, string | number>;
}
interface Compare {
  metrics: Record<string, { a: number; b: number; delta: number }>;
  by_tag: Record<string, { a: number; b: number }>;
  better: { item: string; persona: string; metric: string; a: number; b: number }[];
  worse: { item: string; persona: string; metric: string; a: number; b: number }[];
}
interface FeedbackRow {
  id: string;
  rating: number;
  created_at: string;
  asker: string | null;
  reviewable: boolean;
  question?: string;
  answer?: string;
  comment?: string;
  cited_documents?: string[];
}

const fmt = (v: number | undefined) => (v === undefined ? "-" : v.toFixed(3));

export default function Evaluation() {
  return (
    <div className="space-y-4">
      <Runs />
      <ReviewQueue />
      <Upload />
    </div>
  );
}

function Runs() {
  const qc = useQueryClient();
  const sets = useQuery({ queryKey: ["eval-sets"], queryFn: () => api<SetRow[]>("/api/admin/eval/sets") });
  const runs = useQuery({
    queryKey: ["eval-runs"],
    queryFn: () => api<RunRow[]>("/api/admin/eval/runs"),
    refetchInterval: (q) => (q.state.data?.some((r) => r.status === "running") ? 3000 : false),
  });
  const [set, setSet] = useState("company");
  const [label, setLabel] = useState("");
  const [overrides, setOverrides] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [pick, setPick] = useState<string[]>([]);
  const start = useMutation({
    mutationFn: () => {
      const o: Record<string, string> = {};
      overrides
        .split(/[\s,]+/)
        .filter(Boolean)
        .forEach((kv) => {
          const [k, ...v] = kv.split("=");
          o[k] = v.join("=");
        });
      return api("/api/admin/eval/runs", { method: "POST", body: json({ set, label, overrides: o }) });
    },
    onSuccess: () => {
      setMsg("Run started; results appear below when it finishes.");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["eval-runs"] }), 1000);
    },
    onError: (e: Error) => setMsg(e.message),
  });
  const apply = useMutation({
    mutationFn: (id: string) => api(`/api/admin/eval/runs/${id}/apply`, { method: "POST" }),
    onSuccess: () => {
      setMsg("Applied: the chat now uses the settings this run was measured with.");
      qc.invalidateQueries({ queryKey: ["retrieval"] });
    },
    onError: (e: Error) => setMsg(e.message),
  });
  const cmp = useQuery({
    queryKey: ["eval-compare", ...pick],
    queryFn: () => api<Compare>(`/api/admin/eval/compare?a=${pick[0]}&b=${pick[1]}`),
    enabled: pick.length === 2,
  });
  const dbSets = (sets.data ?? []).filter((s) => s.source === "db");

  return (
    <Card className="p-4">
      <h2 className="text-base font-semibold">Evaluation runs</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        A run asks every question of a set as the people listed in it, with their real permissions, through the same
        retrieval as the chat. Any document a person may not see makes the run fail. Try a candidate configuration
        with overrides such as <code>reranker=bge-reranker-v2-m3 rerank_min_score=0.05</code>, compare it with the
        current one, and apply it if it is better. The <code>acme</code> set runs with <code>erag eval</code>.
      </p>
      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-4">
        <div>
          <Label htmlFor="eval-set">Set</Label>
          <Select id="eval-set" value={set} onChange={(e) => setSet(e.target.value)}>
            {dbSets.length === 0 && <option value="company">company (empty)</option>}
            {dbSets.map((s) => (
              <option key={s.id} value={s.name}>
                {s.name} ({s.items} questions, {s.classification})
              </option>
            ))}
          </Select>
        </div>
        <div>
          <Label htmlFor="eval-label">Label</Label>
          <Input id="eval-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="candidate" />
        </div>
        <div className="md:col-span-2">
          <Label htmlFor="eval-over">Retrieval overrides (key=value)</Label>
          <Input id="eval-over" value={overrides} onChange={(e) => setOverrides(e.target.value)} />
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button onClick={() => start.mutate()} disabled={start.isPending}>
          Run
        </Button>
        {msg && <span className="text-sm text-muted-foreground">{msg}</span>}
      </div>
      <Table className="mt-3">
        <thead>
          <tr>
            <Th>Compare</Th>
            <Th>Started</Th>
            <Th>Set</Th>
            <Th>Label</Th>
            <Th>recall@8</Th>
            <Th>MRR</Th>
            <Th>Abstention</Th>
            <Th>Leaks</Th>
            <Th />
          </tr>
        </thead>
        <tbody>
          {(runs.data ?? []).map((r) => (
            <tr key={r.id} className="border-t border-border">
              <td>
                <input
                  type="checkbox"
                  aria-label={`select run ${r.label || r.id}`}
                  checked={pick.includes(r.id)}
                  onChange={(e) =>
                    setPick((p) => (e.target.checked ? [...p.slice(-1), r.id] : p.filter((x) => x !== r.id)))
                  }
                />
              </td>
              <td className="text-xs">{new Date(r.started_at).toLocaleString()}</td>
              <td>{r.set}</td>
              <td>{r.label}</td>
              {r.status === "done" ? (
                <>
                  <td>{fmt(r.metrics["recall@8"])}</td>
                  <td>{fmt(r.metrics.mrr)}</td>
                  <td>{fmt(r.metrics.abstention)}</td>
                  <td>
                    {r.metrics.leaks ? (
                      <Badge className="border-red-300 bg-red-50 text-red-800">{r.metrics.leaks}</Badge>
                    ) : (
                      "0"
                    )}
                  </td>
                  <td>
                    <Button size="sm" variant="outline" onClick={() => apply.mutate(r.id)} disabled={!!r.metrics.leaks}>
                      Apply settings
                    </Button>
                  </td>
                </>
              ) : (
                <td colSpan={5} className="text-xs text-muted-foreground">
                  {r.status}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </Table>
      {cmp.data && (
        <div className="mt-4">
          <h3 className="text-sm font-semibold">Comparison (A = older selection, B = newer)</h3>
          <Table className="mt-1">
            <thead>
              <tr>
                <Th>Metric</Th>
                <Th>A</Th>
                <Th>B</Th>
                <Th>Change</Th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(cmp.data.metrics).map(([k, v]) => (
                <tr key={k} className="border-t border-border">
                  <td>{k}</td>
                  <td>{fmt(v.a)}</td>
                  <td>{fmt(v.b)}</td>
                  <td className={v.delta > 0 ? "text-emerald-700" : v.delta < 0 ? "text-red-700" : ""}>
                    {v.delta > 0 ? "+" : ""}
                    {v.delta.toFixed(3)}
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
          <p className="mt-2 text-xs text-muted-foreground">
            Better on {cmp.data.better.length} question(s), worse on {cmp.data.worse.length}:{" "}
            {cmp.data.worse
              .slice(0, 10)
              .map((w) => `${w.item} (${w.persona})`)
              .join(", ")}
          </p>
        </div>
      )}
    </Card>
  );
}

function ReviewQueue() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["eval-feedback"], queryFn: () => api<FeedbackRow[]>("/api/admin/eval/feedback") });
  const [docs, setDocs] = useState<Record<string, string[]>>({});
  const [err, setErr] = useState<string | null>(null);
  const act = useMutation({
    mutationFn: ({ id, action, body }: { id: string; action: "accept" | "reject"; body?: unknown }) =>
      api(`/api/admin/eval/feedback/${id}/${action}`, { method: "POST", body: body ? json(body) : undefined }),
    onSuccess: () => {
      setErr(null);
      qc.invalidateQueries({ queryKey: ["eval-feedback"] });
      qc.invalidateQueries({ queryKey: ["eval-sets"] });
    },
    onError: (e: Error) => setErr(e.message),
  });
  return (
    <Card className="p-4">
      <h2 className="text-base font-semibold">Review queue</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Answers people marked as unhelpful and proposed as test questions. You see the question and the sources only
        if you can read every source; write down which documents should have answered it, and it joins the{" "}
        <code>company</code> set (classified confidential).
      </p>
      {err && <p className="mt-2 text-sm text-red-700">{err}</p>}
      {(q.data ?? []).length === 0 && <p className="mt-2 text-sm text-muted-foreground">Nothing to review.</p>}
      <div className="mt-2 space-y-3">
        {(q.data ?? []).map((f) => (
          <div key={f.id} className="rounded border border-border p-3 text-sm">
            <div className="text-xs text-muted-foreground">
              {f.asker} · {new Date(f.created_at).toLocaleString()}
            </div>
            {!f.reviewable ? (
              <p className="mt-1 text-muted-foreground">You cannot read every source of this answer, so you cannot review it.</p>
            ) : (
              <>
                <p className="mt-1">
                  <span className="font-medium">Question:</span> {f.question}
                </p>
                <p className="mt-1 whitespace-pre-wrap text-muted-foreground">{f.answer}</p>
                {f.comment && <p className="mt-1 italic">"{f.comment}"</p>}
                <Label className="mt-2 block">Documents that should answer it (container/filename, one per line)</Label>
                <Textarea
                  rows={2}
                  defaultValue={(f.cited_documents ?? []).join("\n")}
                  onChange={(e) => setDocs((d) => ({ ...d, [f.id]: e.target.value.split("\n").map((x) => x.trim()).filter(Boolean) }))}
                />
                <div className="mt-2 flex gap-2">
                  <Button
                    size="sm"
                    onClick={() =>
                      act.mutate({
                        id: f.id,
                        action: "accept",
                        body: { documents: docs[f.id] ?? f.cited_documents ?? [], answerable: (docs[f.id] ?? f.cited_documents ?? []).length > 0 },
                      })
                    }
                  >
                    Add to the company set
                  </Button>
                  <Button size="sm" variant="outline" onClick={() => act.mutate({ id: f.id, action: "reject" })}>
                    Reject
                  </Button>
                </div>
              </>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

function Upload() {
  const qc = useQueryClient();
  const [name, setName] = useState("company");
  const [text, setText] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const up = useMutation({
    mutationFn: () => api<{ items: number }>(`/api/admin/eval/sets/${encodeURIComponent(name)}/items`, { method: "POST", body: json({ yaml_text: text }) }),
    onSuccess: (r) => {
      setMsg(`${r.items} question(s) stored.`);
      qc.invalidateQueries({ queryKey: ["eval-sets"] });
    },
    onError: (e: Error) => setMsg(e.message),
  });
  return (
    <Card className="p-4">
      <h2 className="text-base font-semibold">Add questions</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        Same YAML format as <code>eval/sets/acme.yaml</code>; personas are real accounts and documents are{" "}
        <code>container/filename</code>. Items with an existing id are replaced.
      </p>
      <div className="mt-2 grid grid-cols-1 gap-2 md:grid-cols-4">
        <div>
          <Label htmlFor="up-name">Set</Label>
          <Input id="up-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
      </div>
      <Textarea className="mt-2 font-mono text-xs" rows={8} value={text} onChange={(e) => setText(e.target.value)} placeholder="items:\n  - id: q1\n    question: ..." />
      <div className="mt-2 flex items-center gap-3">
        <Button onClick={() => up.mutate()} disabled={!text.trim()}>
          Validate and store
        </Button>
        {msg && <span className="whitespace-pre-wrap text-sm text-muted-foreground">{msg}</span>}
      </div>
    </Card>
  );
}
