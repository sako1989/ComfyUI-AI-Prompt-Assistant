// AI Prompt Assistant — frontend: live log console, backend-aware model list, Stop button.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const NODE_TYPE = "AIPromptAssistant";
const EVENT = "ai_prompt_assistant.log";

const COLORS = {
  status: "#8fa3b8",
  thinking: "#b39ddb",
  text: "#e0e0e0",
  tool: "#80cbc4",
  tool_result: "#7f9a96",
  search: "#ffcc80",
  warn: "#ffb74d",
  error: "#ef5350",
  result: "#a5d6a7",
  done: "#66bb6a",
};

function injectStyles() {
  if (document.getElementById("aipa-styles")) return;
  const style = document.createElement("style");
  style.id = "aipa-styles";
  style.textContent = `
    .aipa-root { display:flex; flex-direction:column; width:100%; height:100%; min-height:160px;
      box-sizing:border-box; background:#161616; border:1px solid #333; border-radius:6px; overflow:hidden;
      font-family: ui-monospace, Consolas, "Cascadia Mono", monospace; font-size:11px; color:#ddd; }
    .aipa-head { display:flex; align-items:center; gap:8px; padding:4px 8px; background:#1f1f1f;
      border-bottom:1px solid #333; flex:0 0 auto; }
    .aipa-dot { width:8px; height:8px; border-radius:50%; background:#555; flex:0 0 auto; }
    .aipa-dot.running { background:#ffb74d; box-shadow:0 0 6px #ffb74d; animation: aipa-pulse 1s infinite; }
    .aipa-dot.ok { background:#66bb6a; } .aipa-dot.err { background:#ef5350; }
    @keyframes aipa-pulse { 0%,100% { opacity:1 } 50% { opacity:.35 } }
    .aipa-title { flex:1 1 auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#aaa; }
    .aipa-btn { background:#2a2a2a; color:#ddd; border:1px solid #444; border-radius:4px; padding:2px 8px;
      cursor:pointer; font-size:11px; }
    .aipa-btn:hover { background:#3a3a3a; } .aipa-btn.stop { color:#ef9a9a; }
    .aipa-btn.run { color:#a5d6a7; border-color:#3d5a3f; } .aipa-btn.run:hover { background:#24352a; }
    .aipa-btn:disabled { opacity:.45; cursor:default; }
    .aipa-body { flex:1 1 auto; overflow:auto; padding:6px 8px; white-space:pre-wrap; word-break:break-word;
      line-height:1.45; }
    .aipa-line { margin:0 0 3px 0; }
    .aipa-line .k { opacity:.7; margin-right:6px; }
    .aipa-line.thinking { font-style:italic; opacity:.85; }
    .aipa-line.result { background:#1e2b1e; border-left:3px solid #66bb6a; padding:4px 6px; border-radius:3px;
      font-family: system-ui, sans-serif; font-size:12px; user-select:text; }
    .aipa-empty { color:#666; font-style:italic; }
    .aipa-usage { flex:0 0 auto; padding:5px 8px; color:#9fb9ce; border-bottom:1px solid #333;
      white-space:normal; overflow-wrap:anywhere; line-height:1.5; }
  `;
  document.head.appendChild(style);
}

function findNode(nodeId) {
  // ComfyUI sends the flattened execution id; inside subgraphs it is a path like "12:34"
  // (subgraph-node id, then the inner node id). Walk that path instead of guessing.
  const parts = String(nodeId).split(":");
  let graph = app.graph;
  let node = null;
  for (let i = 0; i < parts.length; i++) {
    node = graph?.getNodeById?.(Number(parts[i])) || null;
    if (!node) return null;
    if (i < parts.length - 1) graph = node.subgraph || null;
  }
  return node;
}

// ---------------------------------------------------------------------------------------------
// "Run" button: execute ONLY this node (plus whatever feeds it), not the whole workflow.
// The node itself is not an output node, so we queue the full graph with a temporary PreviewAny
// sink wired to the assistant's `prompt` output and make that sink the single partial-execution
// target. Sending the full graph keeps every other node's cache intact; nothing is added to the
// user's canvas or saved workflow.
const SINK_TYPE = "PreviewAny";

function findExecId(output, node) {
  // graphToPrompt flattens subgraphs into ids like "12:34"; map this LiteGraph node to its key.
  const id = String(node.id);
  const isMine = (k) => output[k]?.class_type === NODE_TYPE;
  const inRoot = !node.graph || node.graph === app.graph || node.graph.isRootGraph;
  if (inRoot && isMine(id)) return id;
  const nested = Object.keys(output).filter((k) => k !== id && isMine(k) && k.split(":").pop() === id);
  if (nested.length) return nested[0];
  return isMine(id) ? id : null;
}

function attachSink(output, execId) {
  // temporary output node wired to the assistant's `prompt` output; lives only in this one request
  let n = 900000 + (Number(String(execId).split(":").pop()) || 0);
  while (output[String(n)]) n++;
  const sinkId = String(n);
  output[sinkId] = {
    class_type: SINK_TYPE,
    inputs: { source: [String(execId), 0] },
    _meta: { title: "AI Prompt Assistant (run alone)" },
  };
  return sinkId;
}

// Native "Execute to selected output nodes" (the blue play button in the selection toolbox).
// The frontend offers it because we flag the node def as output_node on the client only; the server
// still sees a normal node, so full-workflow queues behave exactly as before. When the native command
// queues with this node as a partial-execution target, swap it for the temporary sink (the server
// only accepts real output nodes as targets).
function patchQueuePrompt() {
  if (api.__aipaQueuePatched) return;
  api.__aipaQueuePatched = true;
  const orig = api.queuePrompt;
  api.queuePrompt = async function (number, data, options) {
    const marked = [];
    try {
      const targets = options?.partialExecutionTargets;
      const output = data?.output;
      if (Array.isArray(targets) && targets.length && output &&
          window.LiteGraph?.registered_node_types?.[SINK_TYPE]) {
        const swapped = targets.map((t) => {
          if (output[t]?.class_type !== NODE_TYPE) return t;
          const node = findNode(t);
          if (node) {
            node.aipaSolo = { promptId: null };
            node.aipaLog?.append("status", "Queued the path to this node only - the rest of the workflow is skipped");
            marked.push(node);
          }
          return attachSink(output, t);
        });
        options = { ...options, partialExecutionTargets: swapped };
      }
    } catch (e) {
      console.warn("[AI Prompt Assistant] could not prepare partial execution", e);
    }
    let res;
    try {
      res = await orig.call(this, number, data, options);
    } catch (e) {
      for (const node of marked) node.aipaSolo = null;
      throw e;
    }
    for (const node of marked) if (node.aipaSolo) node.aipaSolo.promptId = res?.prompt_id ?? null;
    return res;
  };
}

function describeQueueError(err) {
  const resp = err?.response;
  const parts = [];
  if (resp?.error?.message) parts.push(resp.error.message + (resp.error.details ? ` (${resp.error.details})` : ""));
  for (const [nid, ne] of Object.entries(resp?.node_errors || {})) {
    for (const e of ne?.errors || []) parts.push(`node ${nid} ${ne.class_type || ""}: ${e.message}${e.details ? " - " + e.details : ""}`);
  }
  return parts.length ? parts.join(" | ") : String(err?.message || err);
}

async function runSolo(node, log, opts = {}) {
  try {
    if (!window.LiteGraph?.registered_node_types?.[SINK_TYPE]) {
      log.append("error", `Cannot run alone: core node "${SINK_TYPE}" is not available in this ComfyUI.`);
      return;
    }
    if (opts.reroll) {
      // Shift+click: new seed => guaranteed fresh run. (Like ComfyUI's own partial execution, a plain
      // Run leaves the seed alone, so a later full queue reuses this exact result from the cache.)
      const seedW = node.widgets?.find((w) => w.name === "seed");
      if (seedW) {
        const v = Math.floor(Math.random() * 0x4000000000000);
        seedW.value = v;
        seedW.callback?.(v);
      }
    }
    for (const w of node.widgets || []) w.beforeQueued?.({ isPartialExecution: true });
    const p = await app.graphToPrompt();
    const execId = findExecId(p.output, node);
    if (!execId) {
      log.append("error", "Cannot run alone: this node is muted/bypassed or not part of the executable graph.");
      return;
    }
    const sinkId = attachSink(p.output, execId);
    log.append("status", "Queued this node only - the rest of the workflow is skipped");
    // remember that this queue came from the Run button, so only such runs report "cached"
    node.aipaSolo = { promptId: null };
    const res = await api.queuePrompt(0, p, { partialExecutionTargets: [sinkId] });
    if (node.aipaSolo) node.aipaSolo.promptId = res?.prompt_id ?? null;
    // same seed handling a normal queue would apply (randomize / increment / fixed)
    for (const w of node.widgets || []) w.afterQueued?.({ isPartialExecution: true });
    node.setDirtyCanvas?.(true, false);
  } catch (err) {
    node.aipaSolo = null;
    log.append("error", "Could not queue: " + describeQueueError(err));
  }
}

function buildLogWidget(node) {
  injectStyles();
  const root = document.createElement("div");
  root.className = "aipa-root";

  const head = document.createElement("div");
  head.className = "aipa-head";
  const dot = document.createElement("span");
  dot.className = "aipa-dot";
  const title = document.createElement("span");
  title.className = "aipa-title";
  title.textContent = "idle — queue the prompt to run the assistant";
  const runBtn = document.createElement("button");
  runBtn.className = "aipa-btn run";
  runBtn.textContent = "▶ Run";
  runBtn.title = "Run only this node (and the nodes feeding it) without executing the rest of the workflow.\n" +
    "Shift+click: roll a new seed first to force a fresh result.";
  const copyBtn = document.createElement("button");
  copyBtn.className = "aipa-btn";
  copyBtn.textContent = "Copy prompt";
  copyBtn.title = "Copy the last result prompt to the clipboard";
  const copyNegativeBtn = document.createElement("button");
  copyNegativeBtn.className = "aipa-btn";
  copyNegativeBtn.textContent = "Copy negative";
  copyNegativeBtn.title = "Copy the last negative prompt to the clipboard";
  copyNegativeBtn.disabled = true;
  const clearBtn = document.createElement("button");
  clearBtn.className = "aipa-btn";
  clearBtn.textContent = "Clear";
  const stopBtn = document.createElement("button");
  stopBtn.className = "aipa-btn stop";
  stopBtn.textContent = "Stop";
  stopBtn.style.display = "none";
  head.append(dot, title, runBtn, copyBtn, copyNegativeBtn, clearBtn, stopBtn);

  const body = document.createElement("div");
  body.className = "aipa-body";
  const empty = document.createElement("div");
  empty.className = "aipa-empty";
  empty.textContent = "Log output will appear here.";
  body.appendChild(empty);

  const usageBar = document.createElement("div");
  usageBar.className = "aipa-usage";
  usageBar.textContent = "Tokens: — | Cost: unavailable";
  usageBar.title = "This run, including research and retries. Updated after each CLI call. " +
    "Cost is the CLI-reported USD estimate, not subscription credits or your bill. Remaining credits are unavailable.";
  root.append(head, usageBar, body);

  const state = { lastPrompt: "", lastNegativePrompt: "", running: false, stick: true };
  // follow the tail unless the user scrolled up on purpose
  body.addEventListener("scroll", () => {
    state.stick = body.scrollHeight - body.scrollTop - body.clientHeight < 12;
  });

  function setRunning(flag) {
    state.running = flag;
    dot.className = "aipa-dot" + (flag ? " running" : "");
    stopBtn.style.display = flag ? "" : "none";
    runBtn.style.display = flag ? "none" : "";
  }

  function onCached() {
    // ComfyUI skipped the node because nothing changed; say so instead of looking dead
    if (state.running) return;
    append("status", "Inputs unchanged - ComfyUI reused the cached result, no new CLI run. " +
      "Shift+click Run (or change the seed / any input) for a fresh result.");
    dot.className = "aipa-dot ok";
    title.textContent = "cached result reused";
    usageBar.textContent = "Cached result — no new tokens or cost for this execution";
  }

  function append(kind, text) {
    if (empty.parentNode) empty.remove();
    const line = document.createElement("div");
    line.className = `aipa-line ${kind}`;
    line.style.color = COLORS[kind] || "#ddd";
    if (kind !== "result") {
      const k = document.createElement("span");
      k.className = "k";
      k.textContent = `[${kind}]`;
      line.appendChild(k);
    }
    line.appendChild(document.createTextNode(text));
    body.appendChild(line);
    // keep the console bounded
    while (body.children.length > 600) body.removeChild(body.firstChild);
    if (state.stick) body.scrollTop = body.scrollHeight;
  }

  function clear() {
    body.replaceChildren(empty);
    state.stick = true;
  }

  function onEvent(d) {
    const kind = d.kind || "text";
    const text = d.text ?? "";
    if (d.node_id != null) node.aipaRunId = String(d.node_id); // execution id, used by Stop
    if (kind === "status" && text === "started") {
      node.aipaSolo = null; // it is really executing, so this is not a cache hit
      clear();
      state.lastPrompt = "";
      state.lastNegativePrompt = "";
      copyNegativeBtn.disabled = true;
      usageBar.textContent = "Tokens: awaiting CLI report… | Cost: unavailable";
      setRunning(true);
      title.textContent = "running…";
      append("status", "Run started");
      return;
    }
    if (kind === "status") title.textContent = text.slice(0, 140);
    if (kind === "usage") {
      try {
        const usage = JSON.parse(text);
        const number = (value) => typeof value === "number" ? value.toLocaleString() : "—";
        const cost = typeof usage.cost_usd === "number"
          ? `$${usage.cost_usd.toFixed(4)}${usage.cost_partial ? " (partial)" : ""}` : "unavailable";
        usageBar.textContent = `Tokens: ${number(usage.total_tokens)}${usage.tokens_partial ? " (partial)" : ""}` +
          ` | In: ${number(usage.input_tokens)} | Out: ${number(usage.output_tokens)}` +
          ` | Cached input: ${number(usage.cache_read)} | Calls: ${number(usage.calls)} | Cost: ${cost}`;
        append("status", usageBar.textContent);
      } catch (_) {
        append("warn", "Could not read the CLI usage report");
      }
      return;
    }
    if (kind === "result" && !text.startsWith("Negative: ")) state.lastPrompt = text;
    if (kind === "result" && text.startsWith("Negative: ")) {
      state.lastNegativePrompt = text.slice("Negative: ".length);
      copyNegativeBtn.disabled = !state.lastNegativePrompt.trim();
    }
    if (kind === "done") {
      setRunning(false);
      dot.className = "aipa-dot ok";
      title.textContent = "done";
      return;
    }
    if (kind === "error") {
      setRunning(false);
      dot.className = "aipa-dot err";
      title.textContent = text.slice(0, 140);
    }
    append(kind, text);
  }

  runBtn.onclick = async (e) => {
    e.stopPropagation();
    if (state.running || runBtn.disabled) return;
    runBtn.disabled = true;
    try {
      await runSolo(node, { append }, { reroll: e.shiftKey });
    } finally {
      runBtn.disabled = false;
    }
  };
  copyBtn.onclick = async (e) => {
    e.stopPropagation();
    if (!state.lastPrompt) return;
    try {
      await navigator.clipboard.writeText(state.lastPrompt);
      copyBtn.textContent = "Copied";
      setTimeout(() => (copyBtn.textContent = "Copy prompt"), 1200);
    } catch (_) {}
  };
  copyNegativeBtn.onclick = async (e) => {
    e.stopPropagation();
    if (!state.lastNegativePrompt.trim()) return;
    try {
      await navigator.clipboard.writeText(state.lastNegativePrompt);
      copyNegativeBtn.textContent = "Copied";
      setTimeout(() => (copyNegativeBtn.textContent = "Copy negative"), 1200);
    } catch (_) {}
  };
  clearBtn.onclick = (e) => {
    e.stopPropagation();
    clear();
  };
  stopBtn.onclick = async (e) => {
    e.stopPropagation();
    try {
      await api.fetchApi("/ai_prompt_assistant/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: node.aipaRunId || String(node.id) }),
      }).then((r) => r.json()).then((j) => {
        append("warn", j?.cancelled ? "Stop requested…" : "Nothing running for this node");
      });
    } catch (err) {
      append("error", "Stop failed: " + err);
    }
  };
  // don't let wheel/drag on the console pan the canvas
  for (const evName of ["wheel", "pointerdown", "mousedown"]) {
    body.addEventListener(evName, (e) => e.stopPropagation(), { passive: true });
  }

  const widget = node.addDOMWidget("assistant_log", "AIPA_LOG", root, {
    serialize: false,
    hideOnZoom: false,
    getMinHeight: () => 260,
  });
  // The properties side panel draws unknown widget types through a legacy canvas renderer that
  // assigns `widget.width` (the panel's width) and never resets it; the DOM widget container then
  // uses that instead of the node width and the log shrinks for good. Keep width tied to the node.
  try {
    Object.defineProperty(widget, "width", {
      configurable: true,
      enumerable: false,
      get() { return undefined; },
      set(_v) {},
    });
  } catch (_) {}
  widget.aipa = { onEvent, onCached, append, clear, setRunning };
  node.aipaLog = widget.aipa;
  return widget;
}

function hookModelFilter(node) {
  const backendW = node.widgets?.find((w) => w.name === "backend");
  const modelW = node.widgets?.find((w) => w.name === "model");
  if (!backendW || !modelW) return;
  const allModels = Array.isArray(modelW.options?.values) ? [...modelW.options.values] : [];
  if (!allModels.length) return;
  node.aipaAllModels = allModels;

  const apply = () => {
    const prefix = backendW.value === "codex" ? "codex/" : "claude/";
    const filtered = allModels.filter((v) => v.startsWith(prefix));
    modelW.options.values = filtered;
    if (!filtered.includes(modelW.value)) {
      modelW.value = filtered[0];
    }
    node.setDirtyCanvas?.(true, true);
  };

  const origCb = backendW.callback;
  backendW.callback = function (...args) {
    const r = origCb?.apply(this, args);
    apply();
    return r;
  };
  node.aipaApplyModelFilter = apply;
  apply();
}

function hookSkills(node) {
  const widget = node.widgets?.find((w) => w.name === "skill");
  if (!widget) return;
  const resetSkillAction = () => {
    const action = node.widgets?.find((w) => w.name === "skill_action");
    if (action) {
      action.value = "auto";
      action.callback?.(action.value);
    }
    node.setDirtyCanvas?.(true, true);
  };
  const originalCallback = widget.callback;
  widget.callback = function (...args) {
    const result = originalCallback?.apply(this, args);
    resetSkillAction();
    return result;
  };
  node.aipaRefreshSkills = async () => {
    try {
      const response = await api.fetchApi("/ai_prompt_assistant/skills");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      // Keep a missing selection visible so validation reports it instead of silently changing it.
      widget.options.values = [...new Set([...data.skills, widget.value].filter(Boolean))];
      node.setDirtyCanvas?.(true, true);
    } catch (e) {
      node.aipaLog?.append("warn", `Could not refresh skills: ${e.message}`);
    }
  };
  const button = node.addWidget("button", "Refresh skills list", null, () => {
    resetSkillAction();
    return node.aipaRefreshSkills();
  });
  button.serialize = false;
  node.aipaRefreshSkills();
}

app.registerExtension({
  name: "ai_prompt_assistant",

  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== NODE_TYPE) return;

    // Client-side only: lets the selection toolbox show the native blue play button
    // ("Execute to selected output nodes") for this node. See patchQueuePrompt().
    nodeData.output_node = true;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onNodeCreated?.apply(this, arguments);
      try {
        buildLogWidget(this);
        hookModelFilter(this);
        hookSkills(this);
        const sz = this.computeSize();
        this.setSize([Math.max(this.size[0], 480), Math.max(this.size[1], sz[1] + 80)]);
      } catch (e) {
        console.warn("[AI Prompt Assistant] widget init failed", e);
      }
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      // widget values are applied after configure; re-filter on next frame
      requestAnimationFrame(() => this.aipaApplyModelFilter?.());
      return r;
    };
  },

  setup() {
    patchQueuePrompt();
    api.addEventListener(EVENT, (ev) => {
      const d = ev.detail || {};
      const node = findNode(d.node_id);
      if (!node?.aipaLog) return;
      node.aipaLog.onEvent(d);
      if (d.kind === "done" || (d.kind === "status" && d.text?.startsWith("Saved skill:"))) {
        node.aipaRefreshSkills?.();
      }
    });
    // Only for runs started with the node's own Run button: if ComfyUI served the node from its
    // cache, say so (otherwise the click would look like it did nothing). Normal queues are untouched.
    api.addEventListener("execution_cached", (ev) => {
      const pid = ev.detail?.prompt_id;
      for (const id of ev.detail?.nodes || []) {
        const node = findNode(id);
        const solo = node?.aipaSolo;
        if (!solo || !node.aipaLog) continue;
        if (solo.promptId && pid && solo.promptId !== pid) continue;
        node.aipaSolo = null;
        node.aipaLog.onCached();
      }
    });
  },
});
