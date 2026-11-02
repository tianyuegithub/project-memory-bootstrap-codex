// Managed adapter for project-memory-bootstrap-codex. No transcript text is copied.
// OpenCode V2 only: @opencode/plugin and @opencode/schema 2.2.5 contracts.
import { spawn } from "node:child_process";

const driver = __PROJECT_MEMORY_DRIVER__;

const invoke = (payload) => new Promise((resolve, reject) => {
  const child = spawn(driver[0], [...driver.slice(1), "hook-memory", "--tool", "opencode"], {
    stdio: ["pipe", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", chunk => { output += chunk; });
  child.stderr.on("data", () => {}); // Do not echo provider data or driver diagnostics.
  child.on("error", () => reject(new Error("SHARED_MEMORY_DRIVER_UNAVAILABLE")));
  child.stdin.on("error", () => reject(new Error("SHARED_MEMORY_DRIVER_INPUT_FAILED")));
  child.on("close", code => {
    if (code !== 0) return reject(new Error("SHARED_MEMORY_DRIVER_FAILED"));
    try { resolve(JSON.parse(output || "{}")); }
    catch { reject(new Error("SHARED_MEMORY_OUTPUT_INVALID")); }
  });
  child.stdin.end(JSON.stringify(payload));
});

export default {
  id: "project-memory-bootstrap-codex",
  async setup(ctx) {
    const controller = new AbortController();
    const steps = new Map();
    try {
      await ctx.session.hook("context", async event => {
        try {
          // Plugin location is not necessarily this session's location. Read metadata only.
          const session = await ctx.session.get({ sessionID: event.sessionID });
          const result = await invoke({ hook_event_name: "SessionStart", cwd: session.location.directory,
            session_id: event.sessionID, source: "model_context", native_event: "session.context" });
          const context = result.hookSpecificOutput?.additionalContext;
          if (typeof context === "string") {
            event.system = event.system.filter(part =>
              !(part.type === "text" && part.text.startsWith("[shared-project-memory]")));
            event.system.push({ type: "text", text: context });
          }
          if (result.systemMessage) console.error("[shared-memory] startup callback reported a failure");
        } catch { console.error("[shared-memory] startup context unavailable"); }
      });
      void (async () => {
        try {
          for await (const event of ctx.event.subscribe({ signal: controller.signal, types: [
            "session.execution.started", "session.step.ended", "session.step.failed",
            "session.execution.succeeded", "session.execution.failed", "session.execution.interrupted",
          ] })) {
            const sessionID = event.data.sessionID;
            if (event.type === "session.execution.started") {
              steps.delete(sessionID);
              continue;
            }
            if (event.type === "session.step.ended" || event.type === "session.step.failed") {
              // Never inspect content, native replay fields, error bodies, or reasoning events.
              steps.set(sessionID, event.data.assistantMessageID);
              continue;
            }
            const messageID = steps.get(sessionID);
            steps.delete(sessionID);
            // Control-only executions and executions observed mid-stream may have no step ID.
            if (!messageID) continue;
            try {
              const session = await ctx.session.get({ sessionID });
              const result = await invoke({ hook_event_name: "Stop", cwd: event.location?.directory ?? session.location.directory,
                session_id: sessionID, message_id: messageID, native_completed_at: event.created,
                native_status: event.type.slice("session.execution.".length), native_event: event.type,
                stop_hook_active: false });
              if (result.systemMessage) console.error("[shared-memory] completion callback reported a failure");
            } catch { console.error("[shared-memory] completion event was not recorded"); }
          }
        } catch {
          if (!controller.signal.aborted) console.error("[shared-memory] event subscription unavailable");
        }
      })();
    } catch { console.error("[shared-memory] plugin registration unavailable"); }
    return () => { controller.abort(); steps.clear(); };
  },
};
