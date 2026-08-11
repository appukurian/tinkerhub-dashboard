/**
 * TinkerHub Dashboard — Manual Resolutions Worker
 * ─────────────────────────────────────────────────
 * Cloudflare Worker that stores manually-resolved thread IDs in KV.
 * Paste this into the Cloudflare Worker editor and bind a KV namespace
 * called RESOLUTIONS to it (see deployment instructions below).
 *
 * API:
 *   GET  /resolutions          → { mailbox: [...], discord: [...] }
 *   POST /resolve              ← { id, type: "mailbox"|"discord" }
 *   POST /reopen               ← { id, type: "mailbox"|"discord" }
 *
 * No auth required — this is an internal team dashboard with non-sensitive data.
 * CORS is open so the static GitHub Pages site can call it freely.
 */

const KV_KEY = "resolutions";

// ── CORS headers added to every response ─────────────────────────────────────
const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

// ── Load current state from KV ────────────────────────────────────────────────
async function loadState(env) {
  const raw = await env.RESOLUTIONS.get(KV_KEY);
  if (!raw) return { mailbox: [], discord: [] };
  try {
    return JSON.parse(raw);
  } catch {
    return { mailbox: [], discord: [] };
  }
}

// ── Save state to KV ──────────────────────────────────────────────────────────
async function saveState(env, state) {
  await env.RESOLUTIONS.put(KV_KEY, JSON.stringify(state));
}

// ── Route handler ─────────────────────────────────────────────────────────────
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const method = request.method.toUpperCase();

    // Pre-flight CORS
    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // GET /resolutions — return current state
    if (method === "GET" && url.pathname === "/resolutions") {
      const state = await loadState(env);
      return json(state);
    }

    // POST /resolve — add an ID
    if (method === "POST" && url.pathname === "/resolve") {
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "Invalid JSON body" }, 400);
      }

      const { id, type } = body;
      if (!id || !["mailbox", "discord"].includes(type)) {
        return json({ error: "Missing or invalid id / type" }, 400);
      }

      const state = await loadState(env);
      if (!state[type].includes(id)) {
        state[type].push(id);
        await saveState(env, state);
      }
      return json({ ok: true, state });
    }

    // POST /reopen — remove an ID
    if (method === "POST" && url.pathname === "/reopen") {
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: "Invalid JSON body" }, 400);
      }

      const { id, type } = body;
      if (!id || !["mailbox", "discord"].includes(type)) {
        return json({ error: "Missing or invalid id / type" }, 400);
      }

      const state = await loadState(env);
      state[type] = state[type].filter((x) => x !== id);
      await saveState(env, state);
      return json({ ok: true, state });
    }

    return json({ error: "Not found" }, 404);
  },
};
