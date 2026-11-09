import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, json, setCsrf, type Info, type Me } from "@/lib/api";

const PERSONAS = ["hr", "finance", "engineer", "newhire", "exec", "admin"];

export default function LoginPage({ onLogin, info }: { onLogin: (m: Me) => void; info?: Info }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [mfaStep, setMfaStep] = useState(false);
  const [code, setCode] = useState("");

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      if (mfaStep) {
        const body = /^\d{6}$/.test(code.trim()) ? { code: code.trim() } : { recovery_code: code.trim() };
        const me = await api<Me>("/api/auth/mfa/verify", { method: "POST", body: json(body) });
        setCsrf(me.csrf_token);
        onLogin(me);
        return;
      }
      const me = await api<Me>("/api/auth/login", { method: "POST", body: json({ email, password }) });
      setCsrf(me.csrf_token);
      if (me.mfa_required) {
        setMfaStep(true);
        return;
      }
      onLogin(me);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center bg-muted">
      <Card className="w-full max-w-sm p-6">
        <h1 className="text-lg font-semibold">EnterpriseRAG{info?.company_name ? ` - ${info.company_name}` : ""}</h1>
        <p className="mb-4 text-sm text-muted-foreground">Permission-aware RAG + MCP. Sign in.</p>
        <form onSubmit={submit} className="space-y-3">
          {mfaStep ? (
            <div>
              <Label htmlFor="code">Authenticator code (or a recovery code)</Label>
              <Input id="code" value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" autoFocus />
            </div>
          ) : (
          <>
          <div>
            <Label htmlFor="email">Email</Label>
            <Input id="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" />
          </div>
          <div>
            <Label htmlFor="password">Password</Label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          </>
          )}
          {error && <p className="text-sm text-red-600">{error}</p>}
          <Button type="submit" className="w-full" disabled={busy || (mfaStep ? !code : !email || !password)}>
            {busy ? "Signing in..." : mfaStep ? "Verify" : "Sign in"}
          </Button>
        </form>
        {info?.demo && (
          <p className="mt-4 text-xs text-muted-foreground">
            Demo personas: {PERSONAS.map((p) => `${p}@acme.test`).join(", ")}
          </p>
        )}
      </Card>
    </div>
  );
}
