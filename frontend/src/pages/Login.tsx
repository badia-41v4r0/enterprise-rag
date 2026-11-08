import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { api, json, setCsrf, type Me } from "@/lib/api";

const PERSONAS = ["hr", "finance", "engineer", "newhire", "exec", "admin"];

export default function LoginPage({ onLogin }: { onLogin: (m: Me) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const me = await api<Me>("/api/auth/login", { method: "POST", body: json({ email, password }) });
      setCsrf(me.csrf_token);
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
        <h1 className="text-lg font-semibold">EnterpriseRAG</h1>
        <p className="mb-4 text-sm text-muted-foreground">Permission-aware RAG + MCP. Sign in.</p>
        <form onSubmit={submit} className="space-y-3">
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
          {error && <p className="text-sm text-red-600">{error}</p>}
          <Button type="submit" className="w-full" disabled={busy || !email || !password}>
            {busy ? "Signing in..." : "Sign in"}
          </Button>
        </form>
        <p className="mt-4 text-xs text-muted-foreground">
          Demo personas: {PERSONAS.map((p) => `${p}@acme.test`).join(", ")}
        </p>
      </Card>
    </div>
  );
}
