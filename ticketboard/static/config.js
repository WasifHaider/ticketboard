// API base URL configuration for deployments where the frontend and
// backend live on different origins (e.g. this frontend deployed to
// Vercel, backend running on your own machine behind a Tailscale Funnel /
// Cloudflare Tunnel / etc).
//
// Default: empty string = same-origin requests (e.g. "/tickets"). This is
// correct when FastAPI serves this frontend itself (the default local
// setup, `uvicorn ticketboard.main:app`) — no edit needed.
//
// For a split deployment (e.g. Vercel), set DEFAULT_API_BASE below to your
// backend's public URL, no trailing slash, e.g.:
//   const DEFAULT_API_BASE = "https://ticketboard.your-tailnet.ts.net";
// then commit + redeploy. There is no build step, so this is a plain edit.
const DEFAULT_API_BASE = "https://desktop-k9d8avr.tail90665c.ts.net";

// Can also be overridden at runtime without redeploying, for testing
// against a different backend: open the app with ?api=<url> once — it's
// saved to localStorage and reused on future visits until cleared with
// ?api= (empty).
(function () {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get("api");
  if (fromQuery !== null) {
    if (fromQuery === "") {
      localStorage.removeItem("ticketboard_api_base");
    } else {
      localStorage.setItem("ticketboard_api_base", fromQuery.replace(/\/$/, ""));
    }
  }
  const stored = localStorage.getItem("ticketboard_api_base");

  window.TICKETBOARD_API_BASE = stored || DEFAULT_API_BASE;
})();

