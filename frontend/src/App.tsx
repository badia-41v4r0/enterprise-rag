import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderUp, LogOut, MessageSquare, Shield, UserCog } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, setCsrf, type Info, type Me } from "@/lib/api";
import { T } from "@/lib/texts";
import { StepUpDialog } from "@/components/StepUp";
import AccountPage from "@/pages/Account";
import AdminPage from "@/pages/Admin";
import ChatPage from "@/pages/Chat";
import ContentPage from "@/pages/Content";
import ChangePasswordPage from "@/pages/ChangePassword";
import LoginPage from "@/pages/Login";
import SetupWizard from "@/pages/Setup";

export default function App() {
  const qc = useQueryClient();
  const [view, setView] = useState<"chat" | "content" | "admin" | "account">("chat");
  // Public: setup mode, company name, demo data present (PLAN-v0.4).
  const info = useQuery({ queryKey: ["info"], queryFn: () => api<Info>("/api/info") });
  const me = useQuery({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        const m = await api<Me>("/api/auth/me");
        setCsrf(m.csrf_token);
        return m;
      } catch {
        return null;
      }
    },
  });

  const logout = async () => {
    await qc.cancelQueries(); // an in-flight /me must not resurrect the session in the UI
    await api("/api/auth/logout", { method: "POST" });
    await qc.cancelQueries();
    setCsrf("");
    // Keep the "me" query object (App observes it) and drop every other cached response.
    qc.setQueryData(["me"], null);
    qc.removeQueries({ predicate: (q) => q.queryKey[0] !== "me" });
    setView("chat");
  };

  if (info.isLoading || me.isLoading) return <div className="p-8 text-sm text-muted-foreground">Loading...</div>;
  if (info.data?.setup_required)
    return (
      <SetupWizard
        info={info.data}
        onFinished={(m) => {
          qc.setQueryData(["me"], m);
          qc.invalidateQueries({ queryKey: ["info"] });
          setView("admin");
        }}
      />
    );
  if (!me.data) return <LoginPage info={info.data} onLogin={(m) => qc.setQueryData(["me"], m)} />;
  if (me.data.must_change_password)
    return <ChangePasswordPage me={me.data} onChanged={(m) => qc.setQueryData(["me"], m)} onLogout={logout} />;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-border px-4 py-2">
        <span className="font-semibold">EnterpriseRAG</span>
        {info.data?.company_name && <span className="text-xs text-muted-foreground" data-testid="company-name">{info.data.company_name}</span>}
        <nav className="ml-6 flex gap-1">
          <Button variant={view === "chat" ? "outline" : "ghost"} size="sm" onClick={() => setView("chat")}>
            <MessageSquare className="h-4 w-4" /> Chat
          </Button>
          {(me.data.can_manage_content || me.data.is_admin) && (
            <Button variant={view === "content" ? "outline" : "ghost"} size="sm" onClick={() => setView("content")}>
              <FolderUp className="h-4 w-4" /> Content
            </Button>
          )}
          {me.data.is_admin && (
            <Button variant={view === "admin" ? "outline" : "ghost"} size="sm" onClick={() => setView("admin")}>
              <Shield className="h-4 w-4" /> Admin
            </Button>
          )}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <span>
            {me.data.display_name || me.data.email}{" "}
            <span className="text-xs text-muted-foreground">({me.data.roles.join(", ") || "no roles"})</span>
          </span>
          <Button variant={view === "account" ? "outline" : "ghost"} size="icon" onClick={() => setView("account")} title="Account" aria-label="Account">
            <UserCog className="h-4 w-4" />
          </Button>
          <Button variant="ghost" size="icon" onClick={logout} title="Log out" aria-label="Log out">
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </header>
      {info.data?.demo && (
        <div className="bg-amber-100 px-4 py-1 text-center text-xs text-amber-900" role="status" data-testid="demo-banner">
          {T.demoBanner}
        </div>
      )}
      <main className="min-h-0 flex-1">
        {view === "chat" ? (
          <ChatPage />
        ) : view === "content" ? (
          <ContentPage />
        ) : view === "account" ? (
          <AccountPage me={me.data} />
        ) : (
          <AdminPage me={me.data} />
        )}
      </main>
      <StepUpDialog mfa={me.data.mfa_enabled} />
    </div>
  );
}
