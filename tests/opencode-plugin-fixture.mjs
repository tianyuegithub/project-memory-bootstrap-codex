// Minimal API fixture based on official @opencode/plugin/schema 2.2.5 types.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import vm from "node:vm";

const source = await readFile(process.argv[2], "utf8");
const payloads = [], warnings = [], hooks = new Map();
let driverFailure = false, subscribed, wake;
const events = [];
const module = new vm.SourceTextModule(source, { context: vm.createContext({
  AbortController, console: { error: message => warnings.push(message) },
}) });
const processModule = new vm.SyntheticModule(["spawn"], function () {
  this.setExport("spawn", (command, args) => {
    assert.equal(command, "/fixture/python");
    assert.deepEqual(Array.from(args), ["-I", "-m", "fixture.cli", "hook-memory", "--tool", "opencode"]);
    const child = new EventEmitter();
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.end = input => {
      payloads.push(JSON.parse(input));
      queueMicrotask(() => {
        if (driverFailure) { child.emit("close", 1); return; }
        child.stdout.emit("data", JSON.stringify({ hookSpecificOutput: {
          additionalContext: "[shared-project-memory]\nprotocol fixture",
        } }));
        child.emit("close", 0);
      });
    };
    return child;
  });
}, { context: module.context });
await module.link(name => {
  assert.equal(name, "node:child_process");
  return processModule;
});
await module.evaluate();
const plugin = module.namespace.default;
assert.equal(plugin.id, "project-memory-bootstrap-codex");
assert.equal(plugin.server, undefined);
const cleanup = await plugin.setup({
  location: { directory: "/wrong-plugin-workspace" },
  session: {
    hook: async (name, callback) => { hooks.set(name, callback); },
    get: async ({ sessionID }) => ({ id: sessionID, location: { directory: "/actual-session-workspace" } }),
  },
  event: {
    subscribe: options => {
      subscribed = options;
      options.signal.addEventListener("abort", () => wake?.());
      return (async function* () {
        while (!options.signal.aborted) {
          if (events.length) yield events.shift();
          else await new Promise(resolve => { wake = resolve; });
        }
      })();
    },
  },
});
assert.deepEqual([...hooks.keys()], ["context"]);
assert.deepEqual(Array.from(subscribed.types), ["session.execution.started", "session.step.ended", "session.step.failed",
  "session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"]);
const forbidden = () => { throw new Error("transcript/reasoning access forbidden"); };
const context = { sessionID: "ses_fixture", system: [{ type: "text", text: "existing instructions" }],
  get messages() { return forbidden(); }, get options() { return forbidden(); } };
await hooks.get("context")(context);
await hooks.get("context")(context);
assert.equal(context.system.length, 2);
assert.equal(context.system[1].text, "[shared-project-memory]\nprotocol fixture");
assert.equal(payloads[0].cwd, "/actual-session-workspace");
assert.equal(payloads[0].session_id, "ses_fixture");
const tick = () => new Promise(resolve => setImmediate(resolve));
async function emit(type, data, location) {
  events.push({ type, data, created: 123456789, location });
  wake?.();
  await tick();
}
for (const [terminal, step] of [["succeeded", "ended"], ["failed", "failed"], ["interrupted", "ended"]]) {
  await emit("session.execution.started", { sessionID: "ses_fixture" });
  await emit(`session.step.${step}`, { sessionID: "ses_fixture", assistantMessageID: "msg_fixture",
    get native() { return forbidden(); }, get error() { return forbidden(); }, get content() { return forbidden(); } });
  await emit(`session.execution.${terminal}`, { sessionID: "ses_fixture", get error() { return forbidden(); } },
    { directory: "/event-workspace" });
  assert.deepEqual(payloads.at(-1), { hook_event_name: "Stop", cwd: "/event-workspace", session_id: "ses_fixture",
    message_id: "msg_fixture", native_completed_at: 123456789, native_status: terminal,
    native_event: `session.execution.${terminal}`, stop_hook_active: false });
}
const count = payloads.length;
await emit("session.execution.succeeded", { sessionID: "ses_unobserved" });
assert.equal(payloads.length, count);
// New execution clears stale step metadata instead of guessing a message ID.
await emit("session.step.ended", { sessionID: "ses_fixture", assistantMessageID: "msg_stale" });
await emit("session.execution.started", { sessionID: "ses_fixture" });
await emit("session.execution.succeeded", { sessionID: "ses_fixture" });
assert.equal(payloads.length, count);
driverFailure = true;
await hooks.get("context")(context);
assert.equal(context.system.length, 2);
await emit("session.step.ended", { sessionID: "ses_fixture", assistantMessageID: "msg_failed_driver" });
await emit("session.execution.succeeded", { sessionID: "ses_fixture" });
assert.ok(warnings.includes("[shared-memory] startup context unavailable"));
assert.ok(warnings.includes("[shared-memory] completion event was not recorded"));
cleanup();
await tick();
assert.equal(subscribed.signal.aborted, true);
console.log("OpenCode V2 API fixture passed; real host unverified");
