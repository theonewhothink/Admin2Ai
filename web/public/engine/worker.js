/*
 * Runs the real back-office engine (backend/src/backoffice) in the browser.
 *
 * Loaded as a module worker by web/lib/engine.ts. It boots Pyodide from
 * ../pyodide/ (self-hosted next to this file), installs pydantic, unpacks
 * backoffice.zip, builds BackOfficeService.demo() and answers requests with
 * BackOfficeService.dispatch(method, path, body).
 *
 * Messages in:
 *   { type: "init", version, journal: [{ method, path, body }] }
 *   { type: "call", id, method, path, body }
 * Messages out:
 *   { type: "ready", python, pyodide, ms, replayed }
 *   { type: "failed", message }
 *   { type: "reply", id, status, body }
 */
import { loadPyodide } from "../pyodide/pyodide.mjs";

const here = new URL(".", self.location.href);

const BOOT = `
import json, sys
sys.path.insert(0, "/engine")
from backoffice.service import BackOfficeService

_service = BackOfficeService.demo()

def _call(method, path, body=None):
    if not isinstance(body, str):
        body = None  # JS null/undefined arrive as jsnull; bodies are sent as JSON text
    status, payload = _service.dispatch(method, path, body)
    return json.dumps({"status": status, "body": payload}, default=str)

sys.version.split()[0]
`;

let call = null;
let booting = null;
let failed = false;
const waiting = [];

function reply(id, status, body) {
  self.postMessage({ type: "reply", id, status, body });
}

function run(method, path, body) {
  const text = body === undefined || body === null ? call(method, path) : call(method, path, JSON.stringify(body));
  return JSON.parse(text);
}

async function boot(version, journal) {
  const started = performance.now();
  const pyodide = await loadPyodide({ indexURL: new URL("../pyodide/", here).href });
  await pyodide.loadPackage(["pydantic"], { messageCallback: () => {} });
  const zip = await fetch(new URL(`backoffice.zip?v=${encodeURIComponent(version || "")}`, here));
  if (!zip.ok) throw new Error(`engine bundle: HTTP ${zip.status}`);
  pyodide.unpackArchive(await zip.arrayBuffer(), "zip", { extractDir: "/engine" });
  const python = pyodide.runPython(BOOT);
  call = pyodide.globals.get("_call");

  // Replay what this visitor already did in this tab, so a reload keeps their answers.
  let replayed = 0;
  for (const entry of Array.isArray(journal) ? journal : []) {
    try {
      const { status } = run(entry.method, entry.path, entry.body);
      if (status === 200) replayed += 1;
    } catch {
      // A step that no longer applies is skipped; the rest still replays.
    }
  }
  return { python, pyodide: pyodide.version, ms: Math.round(performance.now() - started), replayed };
}

self.onmessage = (event) => {
  const msg = event.data || {};
  if (msg.type === "init") {
    if (booting) return;
    booting = boot(msg.version, msg.journal).then(
      (info) => {
        self.postMessage({ type: "ready", ...info });
        for (const next of waiting.splice(0)) self.onmessage({ data: next });
      },
      (err) => {
        failed = true;
        self.postMessage({ type: "failed", message: String((err && err.message) || err) });
        for (const next of waiting.splice(0)) reply(next.id, 503, { error: "unavailable", message: "I couldn’t get ready. Reload the page to try again." });
      },
    );
    return;
  }
  if (msg.type !== "call") return;
  if (failed) {
    reply(msg.id, 503, { error: "unavailable", message: "I couldn’t get ready. Reload the page to try again." });
    return;
  }
  if (!call) {
    waiting.push(msg);
    return;
  }
  try {
    const { status, body } = run(msg.method, msg.path, msg.body);
    reply(msg.id, status, body);
  } catch (err) {
    console.error("[engine]", err);
    reply(msg.id, 500, { error: "engine_error", message: "Something went wrong on my side. Try again." });
  }
};
