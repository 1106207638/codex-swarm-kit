// Run from the target main thread in Codex functions.exec.
// Saves settings for an explicitly selected human/Goal successor. No retry.
(() => {
  "use strict";
  const entrypoint = "mcp__codex_app__send_message_to_thread";
  const efforts = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);

  function catalog(metadata) {
    const desc = metadata.find(tool => tool.name === entrypoint)?.description ?? "";
    const line = desc.split("\n").find(s => s.includes("Models and supported reasoning efforts on the calling host:")) ?? "";
    const pattern = /\b([a-z][a-z0-9.-]*) \((?:[^()]|\([^()]*\))*?; supported reasoning efforts: ([a-z, ]+)\)/g;
    const models = [...line.matchAll(pattern)].map(([, id, list]) => ({
      id, reasoning_efforts: list.split(",").map(effort => effort.trim()),
    }));
    if (!models.length || new Set(models.map(m => m.id)).size !== models.length ||
        models.some(m => m.reasoning_efforts.some(effort => !efforts.has(effort)))) {
      throw new Error("No unambiguous model catalog on the native send tool");
    }
    return { catalog: { source: "runtime-tool", models },
             catalog_provenance: { source: "runtime-tool", entrypoint, captured_at: new Date().toISOString() } };
  }

  function decode(result) {
    if (!result || result.isError) throw new Error("Native Codex tool returned an error");
    if (result.structuredContent) return result.structuredContent;
    const block = result.content?.find(item => item.type === "text");
    return JSON.parse(block?.text ?? "");
  }

  const quote = value => "'" + value.replaceAll("'", "''") + "'";

  async function cli(tools, script, request, runtime) {
    if (!runtime || !['windows', 'posix'].includes(runtime.platform) ||
        typeof runtime.python !== 'string' || /[\r\n\0]/.test(runtime.python) ||
        !(runtime.platform === 'windows' ? /^[A-Za-z]:[\\/]/.test(runtime.python) : runtime.python.startsWith('/'))) {
      throw new Error('Explicit platform and absolute Python interpreter required');
    }
    const wire = JSON.stringify({ script, python: runtime.python, request_json: JSON.stringify(request) })
      .replace(/[^\x00-\x7f]/g, char => "\\u" + char.charCodeAt(0).toString(16).padStart(4, "0"));
    const posixQuote = value => "'" + value.replaceAll("'", "'\"'\"'") + "'";
    const cmd = runtime.platform === 'windows' ? "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); " +
      "[Console]::OutputEncoding = $OutputEncoding; " +
      "$codexMainSwitchCall = ConvertFrom-Json -InputObject " + quote(wire) + "; " +
      "$codexMainSwitchCall.request_json | & $codexMainSwitchCall.python -B -X utf8 -- $codexMainSwitchCall.script" :
      "printf '%s' " + posixQuote(JSON.stringify(request)) + " | " + posixQuote(runtime.python) + " -B -X utf8 -- " + posixQuote(script);
    let result = await tools.exec_command({ cmd, shell: runtime.platform === 'windows' ? 'powershell' : '/bin/sh', login: false, max_output_tokens: 5000 });
    let output = result.output || '';
    while (result.session_id) {
      result = await tools.write_stdin({ session_id: result.session_id, chars: "", yield_time_ms: 5000, max_output_tokens: 5000 });
      output += result.output || '';
    }
    const envelope = JSON.parse(output);
    if (result.exit_code !== 0 || !envelope.ok) throw new Error(envelope.error || "Routing CLI failed");
    return envelope.result;
  }

  function isCurrentActive(thread, turn, record) {
    return thread?.id === record.thread_id && thread.hostId === record.host_id &&
      thread.status?.type === "active" && !thread.status.activeFlags?.length &&
      turn?.id === record.before_turn_id && turn.status === "inProgress" && !turn.error;
  }

  async function goalState(tools, record) {
    if (record.next_source !== "goal") return {};
    if (typeof tools.get_goal !== "function") throw new Error("Native get_goal is unavailable");
    const response = await tools.get_goal({});
    const goal = response && Object.hasOwn(response, "goal") ? response.goal : decode(response).goal;
    if (!goal || goal.threadId !== record.thread_id || goal.status !== "active" ||
        goal.createdAt !== record.goal_binding?.created_at || typeof goal.objective !== "string") {
      throw new Error("The bound native Goal is no longer active in this chat");
    }
    return { goal_state: goal };
  }

  async function confirm(tools, args) {
    const { script, state_dir, owner, epoch, request_id } = args;
    const call = (op, fields = {}) => cli(tools, script, { op, state_dir, owner, epoch, request_id, ...fields }, args.runtime);
    const record = await call("main_switch_get");
    const goal = ["accepted", "staged"].includes(record.status) ? await goalState(tools, record) : {};
    return call("main_switch_confirm", goal);
  }

  async function stage(tools, metadata, args) {
    const { script, state_dir, owner, epoch, request_id } = args;
    const call = (op, fields = {}) => cli(tools, script, { op, state_dir, owner, epoch, request_id, ...fields }, args.runtime);
    const record = await call("main_switch_get");
    if (record.mode !== "desktop_self_next_turn" || record.status !== "prepared" ||
        ![entrypoint, "mcp__codex_app__read_thread"].every(
          name => metadata.some(tool => tool.name === name))) {
      throw new Error("No prepared main-thread settings request or native desktop tools are missing");
    }
    const match = catalog(metadata).catalog.models.find(model => model.id === record.target.model);
    if (!match?.reasoning_efforts.includes(record.target.effort)) {
      throw new Error("The prepared model and effort are absent from the live native tool catalog");
    }
    const target = { threadId: record.thread_id, hostId: record.host_id };
    const cancel = (outcome, send_permit) => call("main_switch_cancel", { outcome, send_permit });
    const expiry = Date.parse(record.expires_at);

    // Commit the unique send intent before any possible invocation. A CLI
    // failure here leaves its state intact for recovery and never triggers send.
    let claim;
    let initialGoal;
    try {
      initialGoal = await goalState(tools, record);
    } catch {
      return cancel("goal_state_unavailable_or_changed_no_send");
    }
    try {
      claim = await call("main_switch_claim", initialGoal);
    } catch {
      // A lost or duplicate response does not entitle this runner to release
      // a claim. Leave it for the coordinator to reconcile from host evidence.
      return call("main_switch_get");
    }
    let latest;
    try {
      latest = decode(await tools.mcp__codex_app__read_thread({
        ...target, turnLimit: 1, includeOutputs: false, maxOutputCharsPerItem: 128,
      }));
    } catch {
      return cancel("last_read_failed_no_send", claim.send_permit);
    }
    if (!isCurrentActive(latest.thread, latest.turns?.[0], record) || Date.now() >= expiry) {
      return cancel("old_main_turn_is_no_longer_active_no_send", claim.send_permit);
    }
    try {
      const finalGoal = await goalState(tools, record);
      if (record.next_source === "goal" && finalGoal.goal_state.objective !== initialGoal.goal_state.objective) {
        return cancel("goal_objective_changed_no_send", claim.send_permit);
      }
      const check = await call("main_switch_check", { send_permit: claim.send_permit, ...finalGoal });
      if (!check.can_invoke) return cancel("last_check_refused_no_send", claim.send_permit);
    } catch {
      return cancel("chat_input_or_settings_changed_no_send", claim.send_permit);
    }
    if (Date.now() >= expiry) return cancel("last_check_expired_no_send", claim.send_permit);

    // The running main turn calls itself. The host saves the settings and
    // the neutral marker is attached here. The selected source starts the next turn.
    let reply;
    try {
      reply = decode(await tools[entrypoint](claim.native_args));
    } catch {
      return call("main_switch_receipt", { outcome: "native_send_error_or_unknown", send_permit: claim.send_permit });
    }
    if (reply.threadId !== record.thread_id) {
      return call("main_switch_receipt", { outcome: "native_receipt_thread_missing_or_mismatch", send_permit: claim.send_permit });
    }
    await call("main_switch_receipt", { outcome: "accepted", receipt_thread_id: reply.threadId, send_permit: claim.send_permit });
    // Re-read Goal after the side effect, too. A failed read leaves the accepted
    // receipt persisted; it never grants permission to send again.
    return call("main_switch_confirm", await goalState(tools, record));
  }

  return { catalog, cli, stage, confirm };
})()
