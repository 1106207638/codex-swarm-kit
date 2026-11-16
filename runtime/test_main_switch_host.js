const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const bridge = vm.runInNewContext(readFileSync(join(__dirname, "main_switch_host.js"), "utf8"));
const entrypoint = "mcp__codex_app__send_message_to_thread";
const toolsMetadata = [
  { name: entrypoint, description: "Models and supported reasoning efforts on the calling host: gpt-6-astra (Frontier; supported reasoning efforts: high, max)" },
  { name: "mcp__codex_app__read_thread" },
];

function fixture({ race = false, rejected = false, attention = false, lateInput = false, goal = false, goalFault = null } = {}) {
  const trace = [];
  const record = {
    request_id: "request", thread_id: "chat", host_id: "local", before_turn_id: "old",
    status: "prepared", mode: "desktop_self_next_turn", target: { model: "gpt-6-astra", effort: "high" },
    expires_at: new Date(Date.now() + 30_000).toISOString(),
  };
  if (goal) {
    record.next_source = "goal";
    record.goal_binding = { thread_id: "chat", created_at: 12345, objective_sha256: "checked-by-python" };
  }
  const nativeArgs = { threadId: "chat", hostId: "local", model: "gpt-6-astra", thinking: "high", prompt: "continue" };
  const thread = { id: "chat", hostId: "local", status: { type: "active", activeFlags: attention ? ["waitingOnUserInput"] : [] } };
  const turn = { id: "old", status: "inProgress", error: null };
  const wrapped = value => ({ content: [{ type: "text", text: JSON.stringify(value) }] });
  let goalReads = 0;
  const tools = {
    async get_goal() {
      trace.push("native goal");
      goalReads += 1;
      const value = { threadId: "chat", createdAt: 12345, status: "active", objective: "original scoped goal" };
      if (goalReads >= 2 && goalFault === "paused") value.status = "paused";
      if (goalReads >= 2 && goalFault === "recreated") value.createdAt += 1;
      if (goalReads >= 2 && goalFault === "objective") value.objective = "another goal";
      if (goalReads >= 2 && goalFault === "thread") value.threadId = "other-chat";
      if (goalReads >= 2 && goalFault === "read_error") throw new Error("native read unavailable");
      return { goal: value };
    },
    async exec_command({ cmd }) {
      assert.match(cmd, /^[\x00-\x7f]+$/);
      const [, input] = cmd.match(/ConvertFrom-Json -InputObject '((?:[^']|'')*)';/);
      const wire = JSON.parse(input.replaceAll("''", "'"));
      assert.equal(wire.script, "C:\\test path with ’ apostrophe\\routing.py");
      const request = JSON.parse(wire.request_json);
      trace.push(request.op);
      if (goal && ["main_switch_claim", "main_switch_check", "main_switch_confirm"].includes(request.op)) {
        assert.equal(request.goal_state.threadId, "chat");
        assert.equal(request.goal_state.objective, "original scoped goal");
      }
      let result = record;
      if (request.op === "main_switch_claim") result = { native_args: nativeArgs, send_permit: "one-permit" };
      if (request.op === "main_switch_check") result = { can_invoke: !lateInput };
      if (request.op === "main_switch_receipt") assert.equal(request.send_permit, "one-permit");
      if (request.op === "main_switch_receipt" && request.outcome !== "accepted") result = { status: "unknown" };
      if (request.op === "main_switch_confirm") result = { status: "staged" };
      if (request.op === "main_switch_cancel") result = { status: "cancelled", outcome: request.outcome };
      return { exit_code: 0, output: JSON.stringify({ ok: true, result }) };
    },
    async mcp__codex_app__read_thread() {
      trace.push("last read");
      return wrapped({ thread, turns: [race ? { ...turn, id: "human-turn", status: "inProgress" } : turn] });
    },
    async [entrypoint](args) {
      trace.push("send");
      assert.deepEqual({ ...args }, nativeArgs);
      if (rejected) throw new Error("timeout after possible side effect");
      return wrapped({ threadId: "chat" });
    },
  };
  const args = { script: "C:\\test path with ’ apostrophe\\routing.py", state_dir: "ledger", owner: "chat", epoch: 1,
                 request_id: "request", attempt_id: "attempt", runtime: {platform: 'windows', python: 'C:\\Python311\\python.exe'} };
  return { trace, tools, args, record };
}

test("Goal path reads native state before claim, before send and after receipt", async () => {
  const { tools, args, trace } = fixture({ goal: true });
  assert.equal((await bridge.stage(tools, toolsMetadata, args)).status, "staged");
  assert.equal(trace.filter(x => x === "native goal").length, 3);
  assert.equal(trace.filter(x => x === "send").length, 1);
  assert.ok(trace.indexOf("native goal") < trace.indexOf("main_switch_claim"));
  assert.ok(trace.lastIndexOf("native goal") > trace.indexOf("main_switch_receipt"));
});

test("Goal change or unavailable last read cancels without native send", async () => {
  for (const goalFault of ["paused", "recreated", "objective", "thread", "read_error"]) {
    const { tools, args, trace } = fixture({ goal: true, goalFault });
    assert.equal((await bridge.stage(tools, toolsMetadata, args)).status, "cancelled", goalFault);
    assert.equal(trace.filter(x => x === "send").length, 0, goalFault);
  }
});

test("missing native Goal tool does not claim or send", async () => {
  const { tools, args, trace } = fixture({ goal: true });
  delete tools.get_goal;
  assert.equal((await bridge.stage(tools, toolsMetadata, args)).status, "cancelled");
  assert.equal(trace.includes("main_switch_claim"), false);
  assert.equal(trace.includes("send"), false);
});

test("Goal confirmation reads native state without sending", async () => {
  const { tools, args, trace, record } = fixture({ goal: true });
  record.status = "staged";
  assert.equal((await bridge.confirm(tools, args)).status, "staged");
  assert.deepEqual(trace, ["main_switch_get", "native goal", "main_switch_confirm"]);
});

test("Goal native send error remains unknown and is not retried", async () => {
  const { tools, args, trace } = fixture({ goal: true, rejected: true });
  assert.equal((await bridge.stage(tools, toolsMetadata, args)).status, "unknown");
  assert.equal(trace.filter(x => x === "send").length, 1);
  assert.equal(trace.includes("main_switch_confirm"), false);
});

test("saves settings once from the active main turn, then checks the staging evidence", async () => {
  const { tools, args, trace } = fixture();
  const result = await bridge.stage(tools, toolsMetadata, args);
  assert.equal(result.status, "staged");
  assert.deepEqual(trace, ["main_switch_get", "main_switch_claim",
                           "last read", "main_switch_check", "send", "main_switch_receipt", "main_switch_confirm"]);
});

test("a new user turn at the last read cancels the claim before invoking the tool", async () => {
  const { tools, args, trace } = fixture({ race: true });
  const result = await bridge.stage(tools, toolsMetadata, args);
  assert.equal(result.status, "cancelled");
  assert.ok(!trace.includes("send"));
  assert.equal(trace.at(-1), "main_switch_cancel");
});

test("a native send error remains unknown, without a second call or confirmation", async () => {
  const { tools, args, trace } = fixture({ rejected: true });
  const result = await bridge.stage(tools, toolsMetadata, args);
  assert.equal(result.status, "unknown");
  assert.equal(trace.filter(step => step === "send").length, 1);
  assert.equal(trace.at(-1), "main_switch_receipt");
});

test("a chat that needs human attention is left alone", async () => {
  const { tools, args, trace } = fixture({ attention: true });
  const result = await bridge.stage(tools, toolsMetadata, args);
  assert.equal(result.status, "cancelled");
  assert.ok(!trace.includes("send"));
});

test("a late input detected by the final rollout check leaves the permit unsent", async () => {
  const { tools, args, trace } = fixture({ lateInput: true });
  const result = await bridge.stage(tools, toolsMetadata, args);
  assert.equal(result.status, "cancelled");
  assert.equal(trace.at(-2), "main_switch_check");
  assert.ok(!trace.includes("send"));
});

test("the main-thread bridge rechecks actual catalog availability before claiming", async () => {
  const { tools, args, trace } = fixture();
  const metadata = toolsMetadata.map(tool => ({ ...tool }));
  metadata[0].description = metadata[0].description.replace("high, max", "max");
  await assert.rejects(bridge.stage(tools, metadata, args), /absent from the live native tool catalog/);
  assert.deepEqual(trace, ["main_switch_get"]);
});


test("missing runtime fails before a tool call", async () => {
  await assert.rejects(bridge.cli({}, "routing.py", {}), /Explicit platform/);
});

test("POSIX bridge quotes hostile literal data and gathers streamed output", async () => {
  let received;
  const tools = {
    async exec_command(args) {received = args; return {session_id: 1, output: '{"ok":true,'};},
    async write_stdin() {return {exit_code:0,output:'"result":{"valid":true}}'};}
  };
  const value=await bridge.cli(tools, "/tmp/space 中文 ' $(touch nope)/routing.py", {value:"'$(touch nope)"}, {platform:'posix',python:'/usr/bin/python3'});
  assert.equal(value.valid,true);
  assert.equal(received.shell,'/bin/sh');
  assert.equal(received.login,false);
  assert.match(received.cmd, /^printf '%s' /);
  assert.ok(received.cmd.includes("'\"'\"'"));
});
