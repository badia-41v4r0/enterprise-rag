import { useQuery, useQueryClient } from "@tanstack/react-query";
import { LogOut, MessageSquare, Shield } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { api, setCsrf, type Me } from "@/lib/api";
import AdminPage from "@/pages/Admin";
import ChatPage from "@/pages/Chat";
import LoginPage from "@/pages/Login";

export default function App() {
  const qc = useQueryClient();
  const [view, setView] = useState<"chat" | "admin">("chat");
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
    await api("/api/auth/logout", { method: "POST" });
    setCsrf("");
    qc.clear();
    qc.setQueryData(["me"], null);
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
          <Button variant="ghost" size="icon" onClick={logout} title="Log out" aria-label="Log out">
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </header>
      <main className="min-h-0 flex-1">{view === "chat" ? <ChatPage /> : <AdminPage />}</main>
    </div>
  );
}
