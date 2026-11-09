// Admin > Setup assistant: re-open any setup step with the normal admin session (PLAN-v0.4 section 6), and the
// setup checklist card shown at the top of the admin area until every item is done or dismissed.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Circle } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type ChecklistItem, type Me } from "@/lib/api";
import { T } from "@/lib/texts";
import { cn } from "@/lib/utils";
import {
  AccessStep,
  CheckStep,
  EnvironmentStep,
  InstallTypeStep,
  KnowledgeStep,
  ModelsStep,
  OrganizationStep,
  PeopleStep,
  ReviewStep,
  ToolsStep,
  type StepProps,
} from "@/pages/Setup";

const BASE = "/api/admin/setup";

const STEPS: { key: string; index: number; C: (p: StepProps) => React.ReactNode }[] = [
  { key: "environment", index: 1, C: EnvironmentStep },
  { key: "install_type", index: 3, C: InstallTypeStep },
  { key: "organization", index: 4, C: OrganizationStep },
  { key: "models", index: 5, C: ModelsStep },
  { key: "access", index: 6, C: AccessStep },
  { key: "people", index: 7, C: PeopleStep },
  { key: "knowledge", index: 8, C: KnowledgeStep },
  { key: "check", index: 9, C: CheckStep },
  { key: "tools", index: 10, C: ToolsStep },
  { key: "finish", index: 11, C: ReviewStep },
];

export function SetupChecklist({ onOpen }: { onOpen: (where: string) => void }) {
  const qc = useQueryClient();
  const cl = useQuery({ queryKey: ["setup-checklist"], queryFn: () => api<{ items: ChecklistItem[]; visible: boolean }>(`${BASE}/checklist`) });
  if (!cl.data?.visible) return null;
  const dismiss = async (key: string) => {
    await api(`${BASE}/checklist/${key}/dismiss`, { method: "POST" });
    qc.invalidateQueries({ queryKey: ["setup-checklist"] });
  };
  return (
    <Card className="mb-4 p-4" data-testid="setup-checklist">
      <h3 className="mb-2 text-sm font-semibold">{T.checklist.title}</h3>
      <ul className="space-y-1 text-sm">
        {cl.data.items
          .filter((i) => !i.dismissed)
          .map((i) => (
            <li key={i.key} className="flex items-center gap-2" data-item={i.key} data-done={i.done}>
              {i.done ? <CheckCircle2 className="h-4 w-4 text-green-600" /> : <Circle className="h-4 w-4 text-amber-500" />}
              <span className={cn(i.done && "text-muted-foreground line-through")}>{i.label}</span>
              {!i.done && i.where && (
                <Button size="sm" variant="ghost" onClick={() => onOpen(i.where)}>
                  {T.checklist.open}
                </Button>
              )}
              {!i.done && i.dismissable && (
                <Button size="sm" variant="ghost" onClick={() => dismiss(i.key)} aria-label={`dismiss ${i.key}`}>
                  {T.checklist.dismiss}
                </Button>
              )}
            </li>
          ))}
      </ul>
    </Card>
  );
}

export default function SetupAssistant({ me }: { me?: Me | null }) {
  const [open, setOpen] = useState("environment");
  const st = useQuery({
    queryKey: ["setup-state"],
    queryFn: () => api<{ completed: boolean; completed_at: string | null; completed_via: string | null; install_type: string | null; license_accepted_at: string | null; license_accepted_by: string | null }>(`${BASE}/state`),
  });
  const step = STEPS.find((s) => s.key === open) ?? STEPS[0];
  const meta = T.steps[step.index];
  return (
    <div className="space-y-4">
      <Card className="p-4 text-sm">
        <h3 className="font-semibold">{T.assistant.title}</h3>
        <p className="text-muted-foreground">{T.assistant.intro}</p>
        {st.data && (
          <p className="mt-1 text-xs text-muted-foreground" data-testid="setup-state">
            Setup {st.data.completed ? `completed ${st.data.completed_at ? new Date(st.data.completed_at).toLocaleString() : ""} (${st.data.completed_via})` : "in progress"}
            {st.data.install_type ? `; installation type: ${st.data.install_type}` : ""}
            {st.data.license_accepted_by ? `; license notice accepted by ${st.data.license_accepted_by}` : ""}.
          </p>
        )}
        <div className="mt-2 flex flex-wrap gap-1">
          {STEPS.map((s) => (
            <Button key={s.key} size="sm" variant={s.key === open ? "outline" : "ghost"} onClick={() => setOpen(s.key)}>
              {s.index}. {T.steps[s.index].title}
            </Button>
          ))}
        </div>
      </Card>
      <div>
        <h2 className="text-lg font-semibold">{meta.title}</h2>
        <p className="mb-3 max-w-3xl text-sm text-muted-foreground">{meta.why}</p>
        <step.C base={BASE} wizard={false} onDone={() => undefined} me={me} />
      </div>
    </div>
  );
}
