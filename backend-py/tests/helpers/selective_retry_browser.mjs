// Test-only composition of the REAL login, auth context and assistant component.
// Vite and the API bind only to loopback and never use the workspace API proxy.
import { createServer } from "vite";

const entry = `
import React from "react";
import { createRoot } from "react-dom/client";
import { AuthProvider, useAuth } from "/src/context/auth.tsx";
import Login from "/src/pages/login.tsx";
import { ScrapePromptAssistant } from "/src/components/scrape-prompt-assistant.tsx";
import "/src/index.css";
function Canary() {
  const { user, loading } = useAuth();
  if (loading) return React.createElement("p", null, "Loading session");
  return user ? React.createElement(ScrapePromptAssistant, { jobId: "source" }) : React.createElement(Login);
}
createRoot(document.getElementById("root")).render(React.createElement(AuthProvider, null, React.createElement(Canary)));
`;
const server = await createServer({
  server: {
    host: "127.0.0.1", port: Number(process.argv[1]), strictPort: true,
    proxy: { "/api": { target: `http://127.0.0.1:${process.argv[2]}` } },
  },
  plugins: [{
    name: "isolated-retry-browser",
    resolveId(id) { if (id === "virtual:retry-canary") return "\0retry-canary.tsx"; },
    load(id) { if (id === "\0retry-canary.tsx") return entry; },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (req.url !== "/__retry_canary" && req.url !== "/") return next();
        const html = await server.transformIndexHtml("/__retry_canary", `<!doctype html>
          <html><head><title>Isolated selected retry canary</title></head>
          <body><div id="root"></div><script type="module" src="/@id/__x00__retry-canary.tsx"></script></body></html>`);
        res.setHeader("Content-Type", "text/html"); res.end(html);
      });
    },
  }],
});
await server.listen();
console.log("RETRY_CANARY_READY");
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, async () => { await server.close(); process.exit(0); });
}
