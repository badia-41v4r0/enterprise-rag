import { useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderUp, LogOut, MessageSquare, Shield, UserCog } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, setCsrf, type Me } from "@/lib/api";
import { StepUpDialog } from "@/components/StepUp";
import AccountPage from "@/pages/Account";
import AdminPage from "@/pages/Admin";
import ChatPage from "@/pages/Chat";
import ContentPage from "@/pages/Content";
import LoginPage from "@/pages/Login";

export default function App() {
  const qc = useQueryClient();
  const [view, setView] = useState<"chat" | "content" | "admin" | "account">("chat");
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

  if (me.isLoading) return <div className="p-8 text-sm text-muted-foreground">Loading...</div>;
  if (!me.data) return <LoginPage onLogin={(m) => qc.setQueryData(["me"], m)} />;

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

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-border px-4 py-2">
        <span className="font-semibold">EnterpriseRAG</span>
        <span className="text-xs text-muted-foreground">ACME Corp demo</span>
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
      <main className="min-h-0 flex-1">
        {view === "chat" ? (
          <ChatPage />
        ) : view === "content" ? (
          <ContentPage />
        ) : view === "account" ? (
          <AccountPage me={me.data} />
        ) : (
          <AdminPage />
        )}
      </main>
      <StepUpDialog mfa={me.data.mfa_enabled} />
    </div>
  );
}
