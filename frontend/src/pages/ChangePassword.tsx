// Forced password change at the first login after a temporary password (PLAN-v0.4 decision 2, ADR 0037).
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, json, setCsrf, type Me } from "@/lib/api";
import { strength, T } from "@/lib/texts";
import { cn } from "@/lib/utils";

export default function ChangePasswordPage({ me, onChanged, onLogout }: { me: Me; onChanged: (m: Me) => void; onLogout: () => void }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const s = strength(next);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (next !== repeat) {
      setError(T.password.mismatch);
      return;
    }
    setBusy(true);
    try {
      const m = await api<Me>("/api/auth/change-password", { method: "POST", body: json({ current_password: current, new_password: next }) });
      setCsrf(m.csrf_token);
      onChanged(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="flex h-full items-center justify-center bg-muted p-4">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold">{T.password.title}</h1>
        <p className="mb-4 text-sm text-muted-foreground">{T.password.why}</p>
        <p className="mb-3 text-sm">{me.email}</p>
        <form onSubmit={submit} className="space-y-3">
          <div>
            <Label htmlFor="pw-current">{T.password.current}</Label>
            <Input id="pw-current" type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" />
          </div>
          <div>
            <Label htmlFor="pw-new">{T.password.next}</Label>
            <Input id="pw-new" type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" />
            <div className="mt-1 flex gap-1" aria-label="password strength">
              {[1, 2, 3, 4].map((i) => (
                <span key={i} className={cn("h-1.5 w-8 rounded", i <= s ? (s >= 3 ? "bg-green-600" : "bg-amber-500") : "bg-slate-200")} />
              ))}
            </div>
          </div>
          <div>
            <Label htmlFor="pw-repeat">{T.password.repeat}</Label>
            <Input id="pw-repeat" type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} autoComplete="new-password" />
          </div>
          {error && <p className="text-sm text-red-600">{error}</p>}
          <Button type="submit" className="w-full" disabled={busy || !current || next.length < 12}>
            {T.password.save}
          </Button>
          <Button type="button" variant="ghost" className="w-full" onClick={onLogout}>
            Log out
          </Button>
        </form>
      </Card>
    </div>
  );
}
