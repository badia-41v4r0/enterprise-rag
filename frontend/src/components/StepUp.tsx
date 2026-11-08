// Re-authentication prompt for sensitive admin actions (password, or a TOTP code when MFA is on).
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, json, setStepUpHandler } from "@/lib/api";

export function StepUpDialog({ mfa }: { mfa: boolean }) {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const resolver = useRef<((ok: boolean) => void) | null>(null);

  useEffect(() => {
    setStepUpHandler(
      () =>
        new Promise<boolean>((resolve) => {
          resolver.current = resolve;
          setValue("");
          setError("");
          setOpen(true);
        }),
    );
    return () => setStepUpHandler(null);
  }, []);

  const finish = (ok: boolean) => {
    setOpen(false);
    resolver.current?.(ok);
    resolver.current = null;
  };
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api("/api/auth/step-up", { method: "POST", body: json(mfa ? { code: value } : { password: value }) });
      finish(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30" role="dialog" aria-label="Confirm it is you">
      <Card className="w-full max-w-sm p-5">
        <h3 className="mb-1 font-semibold">Confirm it is you</h3>
        <p className="mb-3 text-sm text-muted-foreground">
          This action changes credentials, data access or code execution. Re-enter your {mfa ? "authenticator code" : "password"}.
        </p>
        <form onSubmit={submit} className="space-y-3">
          <Label htmlFor="stepup">{mfa ? "Code" : "Password"}</Label>
          <Input id="stepup" type={mfa ? "text" : "password"} value={value} onChange={(e) => setValue(e.target.value)} autoFocus />
          {error && <p className="text-sm text-red-600">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" size="sm" onClick={() => finish(false)}>
              Cancel
            </Button>
            <Button type="submit" size="sm" disabled={!value}>
              Confirm
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
