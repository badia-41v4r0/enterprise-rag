import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vite";

// Dev: `npm run dev` proxies /api to the FastAPI app (same origin in production).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "src") } },
  server: { proxy: { "/api": process.env.ERAG_API ?? "http://127.0.0.1:8000" } },
});
