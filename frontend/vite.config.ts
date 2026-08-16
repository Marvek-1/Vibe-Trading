import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const PROXY_PATHS = [
  "/sessions",
  "/swarm/presets",
  "/swarm/runs",
  "/settings/llm",
  "/settings/data-sources",
  "/channels",
  "/mandate",
  "/live",
  "/upload",
  "/shadow-reports",
  "/backtest",
  "/paper-sessions",
];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_URL || "http://127.0.0.1:8899";
  // The backend trusts loopback callers without a key, but on the VPS this
  // proxy's outbound connection to the backend can present as the Docker
  // bridge gateway address instead of 127.0.0.1 (observed as the mismatch
  // between plain-curl and proxied requests -- see api_server.py
  // _is_local_client), which fails the backend's loopback check. Forward the
  // dashboard's own API key so the backend's non-local auth path succeeds
  // regardless of how the loopback check resolves.
  const internalApiKey = process.env.API_AUTH_KEY || "";
  function withAuth(target: string, extra: Record<string, unknown> = {}) {
    return {
      target,
      changeOrigin: true,
      // Preserve the client address for the API's Docker-proxy authentication
      // boundary. The VPS Nginx proxy overwrites inbound X-Forwarded-For, so the
      // client cannot spoof this value from the public internet.
      xfwd: true,
      ...extra,
      configure(proxy: { on: (event: string, cb: (proxyReq: { setHeader: (k: string, v: string) => void }) => void) => void }) {
        proxy.on("proxyReq", (proxyReq) => {
          if (internalApiKey) proxyReq.setHeader("Authorization", `Bearer ${internalApiKey}`);
        });
      },
    };
  }
  const apiProxy = withAuth(apiTarget);
  const apiProxyWithHtmlFallback = {
    ...apiProxy,
    bypass(req: { headers: { accept?: string } }) {
      if (req.headers.accept?.includes("text/html")) {
        return "/index.html";
      }
    },
  };

  return {
    plugins: [react()],
    resolve: {
      alias: { "@": path.resolve(__dirname, "./src") },
    },
    server: {
      port: 5899,
      // The production dashboard is reached through the VPS TLS SNI proxy.
      // Keep Vite's DNS-rebinding protection enabled, with only this hostname
      // admitted instead of the unsafe `allowedHosts: true` escape hatch.
      allowedHosts: ["trading.mostarindustries.com"],
      proxy: {
        ...Object.fromEntries(PROXY_PATHS.map((p) => [p, apiProxy])),
        // SPA RunDetail page — only the two-segment ``/runs/{id}``
        // form should fall back to ``index.html`` on browser navigation.
        // ``/runs/{id}/code`` and ``/runs/{id}/pine`` are API-only and
        // must keep proxying to the backend even when Accept is text/html.
        "^/runs/[^/]+/?$": apiProxyWithHtmlFallback,
        "/runs": apiProxy,
        "/correlation": apiProxyWithHtmlFallback,
        // Also a router.tsx page route (AutopilotRuns) -- a direct
        // navigation/refresh must fall back to index.html the same way
        // /correlation and /runs/{id} do, or it serves raw API JSON
        // instead of the app shell.
        "/autopilot-runs": apiProxyWithHtmlFallback,
        "^/alpha(?:/|$)": apiProxy,
        "^/api(/|$)": withAuth("http://127.0.0.1:8787"),
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            "vendor-react": ["react", "react-dom", "react-router-dom"],
            "vendor-charts": ["echarts"],
          },
        },
      },
    },
  };
});
