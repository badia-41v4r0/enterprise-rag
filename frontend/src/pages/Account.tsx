// Profile: optional TOTP (MFA) enrolment with QR code and recovery codes.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, json, type Me, type MyUsage } from "@/lib/api";
import { UsageSummary } from "@/pages/Quotas";

export default function AccountPage({ me }: { me: Me }) {
  return (
    <div className="h-full overflow-y-auto p-6">
      <h2 className="mb-4 text-lg font-semibold">Account</h2>
      <MfaCard me={me} />
      <MyUsageCard />
    </div>
  );
}

// Also used by the setup wizard (step 2) to enroll the administrator right away.
export function MfaCard({ me }: { me: Me }) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["mfa"],
    queryFn: () => api<{ enabled: boolean; recovery_codes_left: number }>("/api/auth/mfa"),
  });
  const [setup, setSetup] = useState<{ secret: string; qr_svg: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[]>([]);
  const [error, setError] = useState("");
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["mfa"] });
    qc.invalidateQueries({ queryKey: ["me"] });
  };
  const run = async (fn: () => Promise<void>) => {
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const start = () => run(async () => setSetup(await api("/api/auth/mfa/setup", { method: "POST" })));
  const enable = () =>
    run(async () => {
      const r = await api<{ recovery_codes: string[] }>("/api/auth/mfa/enable", { method: "POST", body: json({ code }) });
      setCodes(r.recovery_codes);
      setSetup(null);
      setCode("");
      refresh();
    });
  const disable = () =>
    run(async () => {
      await api("/api/auth/mfa/disable", { method: "POST", body: json(/^\d{6}$/.test(code) ? { code } : { recovery_code: code }) });
      setCode("");
      setCodes([]);
      refresh();
    });
  const regenerate = () =>
    run(async () => {
      const r = await api<{ recovery_codes: string[] }>("/api/auth/mfa/recovery-codes", { method: "POST", body: json({ code }) });
      setCodes(r.recovery_codes);
      setCode("");
      refresh();
    });

  return (
      <Card className="max-w-xl space-y-3 p-4">
        <div className="text-sm">
          Signed in as <strong>{me.email}</strong>
        </div>
        <h3 className="text-sm font-semibold">Two-factor authentication (TOTP)</h3>
        {status.data?.enabled ? (
          <>
            <p className="text-sm" data-testid="mfa-status">
              Enabled. Recovery codes left: {status.data.recovery_codes_left}.
            </p>
            <Label htmlFor="mfa-code">Current code (or a recovery code to disable)</Label>
            <Input id="mfa-code" value={code} onChange={(e) => setCode(e.target.value)} className="max-w-xs" autoComplete="one-time-code" />
            <div className="flex gap-2">
              <Button size="sm" variant="outline" onClick={regenerate} disabled={!code}>
                New recovery codes
              </Button>
              <Button size="sm" variant="outline" onClick={disable} disabled={!code}>
                Disable MFA
              </Button>
            </div>
          </>
        ) : setup ? (
          <>
            <p className="text-sm">Scan this code with an authenticator app, or enter the key manually, then type the 6-digit code.</p>
            <img
              alt="TOTP QR code"
              className="h-48 w-48"
              src={`data:image/svg+xml;base64,${btoa(setup.qr_svg)}`}
            />
            <p className="font-mono text-xs" data-testid="mfa-secret">
              {setup.secret}
            </p>
            <Label htmlFor="mfa-code">Code</Label>
            <Input id="mfa-code" value={code} onChange={(e) => setCode(e.target.value)} className="max-w-xs" autoComplete="one-time-code" />
            <Button size="sm" onClick={enable} disabled={!/^\d{6}$/.test(code)}>
              Confirm and enable
            </Button>
          </>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">Optional. Adds a one-time code from an authenticator app to your login.</p>
            <Button size="sm" onClick={start}>
              Set up MFA
            </Button>
          </>
        )}
        {codes.length > 0 && (
          <div className="rounded border border-amber-300 bg-amber-50 p-3 text-sm">
            <p className="mb-2 font-medium">Recovery codes: each works once. Store them now; they are not shown again.</p>
            <ul className="grid grid-cols-2 gap-1 font-mono text-xs" data-testid="recovery-codes">
              {codes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </div>
        )}
        {error && <p className="text-sm text-red-600">{error}</p>}
      </Card>
  );
}

function MyUsageCard() {
  const q = useQuery({ queryKey: ["my-usage"], queryFn: () => api<MyUsage>("/api/usage/me") });
  return (
    <Card className="mt-4 max-w-3xl p-4">
      <h3 className="text-sm font-semibold">Your usage</h3>
      <p className="text-xs text-muted-foreground">
        Tokens (input plus output) and estimated cost of your questions. Daily allowances reset at 00:00 UTC, monthly ones on the 1st.
      </p>
      {q.data ? <UsageSummary data={q.data} /> : <p className="text-xs text-muted-foreground">Loading...</p>}
    </Card>
  );
}
