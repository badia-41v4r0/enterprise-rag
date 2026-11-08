import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Plus, Send, Trash2, Wrench, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge, Card, ClassBadge } from "@/components/ui/card";
import { Select, Textarea } from "@/components/ui/input";
import {
  api,
  streamChat,
  type ChatEvent,
  type ChunkView,
  type Citation,
  type Classification,
  type Conversation,
  type MessageRow,
  type ModelInfo,
} from "@/lib/api";
import { cn } from "@/lib/utils";

interface ToolStep {
  name: string;
  args?: string;
  ok?: boolean;
  classification?: Classification;
}

interface UiMessage {
  key: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  model?: string | null;
  classification?: Classification;
  tools: ToolStep[];
  refusal?: string;
  error?: string;
  pending?: boolean;
}

function fromRow(m: MessageRow): UiMessage {
  return {
    key: m.id,
    role: m.role,
    content: m.content,
    citations: m.citations.items ?? [],
    model: m.model_used,
    classification: m.role === "assistant" ? m.classification : undefined,
    tools: (m.citations.tools ?? []).map((t) => ({ name: t.name, ok: t.ok })),
  };
}

/** Render [n] markers as clickable citation chips. */
function AnswerText({ text, citations, onCite }: { text: string; citations: Citation[]; onCite: (c: Citation) => void }) {
  const parts = text.split(/(\[\d+\])/g);
  return (
    <p className="whitespace-pre-wrap text-sm leading-relaxed">
      {parts.map((p, i) => {
        const m = /^\[(\d+)\]$/.exec(p);
        const c = m ? citations.find((x) => x.n === Number(m[1])) : undefined;
        return c ? (
          <button
            key={i}
            onClick={() => onCite(c)}
            className="mx-0.5 rounded bg-brand/10 px-1 text-xs font-semibold text-brand hover:bg-brand/20"
            title={c.title}
          >
            {c.n}
          </button>
        ) : (
          <span key={i}>{p}</span>
        );
      })}
    </p>
  );
}

export default function ChatPage() {
  const qc = useQueryClient();
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState<ChunkView | null>(null);
  const [sourceError, setSourceError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  const conversations = useQuery({ queryKey: ["conversations"], queryFn: () => api<Conversation[]>("/api/conversations") });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api<ModelInfo[]>("/api/models") });
  const defaultModel = models.data?.find((m) => m.is_default);
  const tools = useQuery({
    queryKey: ["my-tools"],
    queryFn: () => api<{ name: string; description: string }[]>("/api/tools"),
  });

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const openConversation = async (id: string) => {
    setConversationId(id);
    const rows = await api<MessageRow[]>(`/api/conversations/${id}/messages`);
    setMessages(rows.map(fromRow));
  };

  const newConversation = () => {
    setConversationId(null);
    setMessages([]);
  };

  const removeConversation = async (id: string) => {
    await api(`/api/conversations/${id}`, { method: "DELETE" });
    if (id === conversationId) newConversation();
    qc.invalidateQueries({ queryKey: ["conversations"] });
  };

  const openCitation = async (c: Citation) => {
    setSourceError("");
    try {
      setSource(await api<ChunkView>(`/api/chunks/${c.chunk_id}`));
    } catch {
      setSource(null);
      setSourceError("This source is no longer accessible to you.");
    }
  };

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);
    const key = crypto.randomUUID();
    setMessages((m) => [
      ...m,
      { key: key + "-u", role: "user", content: text, citations: [], tools: [] },
      { key, role: "assistant", content: "", citations: [], tools: [], pending: true },
    ]);
    const patch = (fn: (m: UiMessage) => UiMessage) =>
      setMessages((all) => all.map((m) => (m.key === key ? fn(m) : m)));

    const onEvent = (e: ChatEvent) => {
      switch (e.type) {
        case "meta":
          setConversationId(e.conversation_id);
          break;
        case "citations":
          patch((m) => ({ ...m, citations: e.items }));
          break;
        case "route":
          patch((m) => ({ ...m, model: e.model, classification: e.classification }));
          break;
        case "delta":
          patch((m) => ({ ...m, content: m.content + e.text }));
          break;
        case "tool_call":
          patch((m) => ({ ...m, tools: [...m.tools, { name: e.name, args: e.arguments }] }));
          break;
        case "tool_result":
          patch((m) => ({
            ...m,
            tools: m.tools.map((t, i) =>
              i === m.tools.length - 1 ? { ...t, ok: e.ok, classification: e.classification } : t,
            ),
          }));
          break;
        case "refusal":
          patch((m) => ({ ...m, refusal: e.reason, classification: e.classification }));
          break;
        case "error":
          patch((m) => ({ ...m, error: e.message }));
          break;
        case "done":
          patch((m) => ({ ...m, content: e.answer, model: e.model, classification: e.classification, pending: false }));
          break;
      }
    };

    try {
      await streamChat({ message: text, conversation_id: conversationId, model: model || null }, onEvent);
    } catch (err) {
      patch((m) => ({ ...m, error: err instanceof Error ? err.message : "Request failed" }));
    } finally {
      patch((m) => ({ ...m, pending: false }));
      setBusy(false);
      qc.invalidateQueries({ queryKey: ["conversations"] });
    }
  };

  return (
    <div className="flex h-full">
      <aside className="flex w-64 flex-col border-r border-border bg-muted/50">
        <div className="p-3">
          <Button variant="outline" className="w-full" onClick={newConversation}>
            <Plus className="h-4 w-4" /> New conversation
          </Button>
        </div>
        <div className="flex-1 overflow-y-auto px-2">
          {conversations.data?.map((c) => (
            <div
              key={c.id}
              className={cn(
                "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-white",
                c.id === conversationId && "bg-white font-medium",
              )}
            >
              <button className="flex-1 truncate text-left" onClick={() => openConversation(c.id)} title={c.title}>
                {c.title}
              </button>
              <button
                className="invisible text-muted-foreground group-hover:visible"
                onClick={() => removeConversation(c.id)}
                aria-label="Delete conversation"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
        <div className="border-t border-border p-3 text-xs text-muted-foreground">
          <div className="mb-1 flex items-center gap-1 font-medium">
            <Wrench className="h-3.5 w-3.5" /> Tools the model can use for you
          </div>
          {tools.data?.length ? tools.data.map((t) => <div key={t.name}>{t.name}</div>) : <div>none</div>}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col">
        <div className="flex-1 space-y-4 overflow-y-auto p-6">
          {messages.length === 0 && (
            <div className="mx-auto mt-16 max-w-md text-center text-sm text-muted-foreground">
              Ask about ACME policies, salaries, budgets, deploys or invoices. Answers only use sources you are
              allowed to see; confidential sources are only processed by local models.
            </div>
          )}
          {messages.map((m) =>
            m.role === "user" ? (
              <div key={m.key} className="ml-auto max-w-2xl rounded-lg bg-brand px-4 py-2 text-sm text-brand-foreground">
                {m.content}
              </div>
            ) : (
              <Card key={m.key} className="max-w-3xl p-4">
                <div className="mb-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                  {m.model ? (
                    <Badge className="border-border">
                      <Cpu className="mr-1 h-3 w-3" />
                      {m.model}
                    </Badge>
                  ) : (
                    !m.pending && <Badge className="border-border">no model called</Badge>
                  )}
                  {m.classification && <ClassBadge value={m.classification} />}
                  {m.pending && <span className="animate-pulse">thinking...</span>}
                </div>
                {m.tools.map((t, i) => (
                  <div key={i} className="mb-2 rounded border border-border bg-muted px-2 py-1 font-mono text-xs">
                    tool {t.name} {t.args ?? ""}{" "}
                    {t.ok === undefined ? "..." : t.ok ? "ok" : "refused"}
                    {t.classification && <span className="ml-2">[{t.classification}]</span>}
                  </div>
                ))}
                {m.refusal ? (
                  <p className="text-sm text-confidential">{m.refusal}</p>
                ) : (
                  <AnswerText text={m.content} citations={m.citations} onCite={openCitation} />
                )}
                {m.error && <p className="mt-2 text-sm text-red-600">{m.error}</p>}
                {m.citations.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1 border-t border-border pt-2">
                    {m.citations.map((c) => (
                      <button
                        key={c.chunk_id}
                        onClick={() => openCitation(c)}
                        className="rounded border border-border px-2 py-0.5 text-xs hover:bg-muted"
                      >
                        [{c.n}] {c.title} <span className="text-muted-foreground">({c.container})</span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            ),
          )}
          <div ref={bottom} />
        </div>

        <div className="border-t border-border p-4">
          <div className="mx-auto flex max-w-3xl items-end gap-2">
            <Textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              placeholder="Ask a question (Enter to send)"
              rows={2}
            />
            <div className="flex flex-col gap-2">
              <Select value={model} onChange={(e) => setModel(e.target.value)} className="w-56" aria-label="Model">
                <option value="">
                  Model: automatic{defaultModel ? ` (default: ${defaultModel.id})` : " (local first)"}
                </option>
                {models.data?.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.is_default ? "★ " : ""}
                    {m.id} ({m.locality}, up to {m.max_classification}){m.is_default ? " - default" : ""}
                  </option>
                ))}
              </Select>
              <Button onClick={() => void send()} disabled={busy || !input.trim()}>
                <Send className="h-4 w-4" /> Send
              </Button>
            </div>
          </div>
        </div>
      </section>

      {(source || sourceError) && (
        <aside className="w-96 overflow-y-auto border-l border-border p-4">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-sm font-semibold">Source</span>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => {
                setSource(null);
                setSourceError("");
              }}
              aria-label="Close source"
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
          {sourceError && <p className="text-sm text-red-600">{sourceError}</p>}
          {source && (
            <>
              <p className="text-sm font-medium">{source.document_title}</p>
              <p className="mb-3 text-xs text-muted-foreground">
                {source.container_name} · chunk {source.ordinal + 1}
              </p>
              <pre className="whitespace-pre-wrap font-sans text-sm">{source.text}</pre>
            </>
          )}
        </aside>
      )}
    </div>
  );
}
