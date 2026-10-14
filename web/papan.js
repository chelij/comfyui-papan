import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const activeNodes = new Set();
const liveConnections = new Map();
const stylesheet = document.createElement("link");
stylesheet.rel = "stylesheet";
stylesheet.href = new URL("./papan.css", import.meta.url).href;
document.head.append(stylesheet);

async function request(path, options) {
  const response = await api.fetchApi(path, options);
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { error: text || `Papan request failed (${response.status}).` }; }
  if (!response.ok) throw Object.assign(new Error(data.error), { status: response.status });
  return data;
}

function entries(node) {
  return JSON.parse(node.widgets.find(widget => widget.name === "media_state").value || "[]");
}

function files(node) {
  return JSON.parse(node.properties.papan_files || "[]");
}

function saveEntries(node, value) {
  node.widgets.find(widget => widget.name === "media_state").value = JSON.stringify(value);
}

function viewURL(entry) {
  const slash = entry.filename.lastIndexOf("/");
  return api.apiURL(`/view?${new URLSearchParams({ filename: entry.filename.slice(slash + 1), subfolder: entry.filename.slice(0, slash), type: "input" })}`);
}

function canUse(node, entry) {
  const session = node.papanSessions.get(entry.boardId);
  return entry.connectionId ? !!session && !session.locked && entry.present !== false && entry.available !== false : !!entry.filename || !!session && entry.available !== false;
}

function liveMediaURL(entry) {
  return api.apiURL(`/papan/connections/${entry.connectionId}/media/${entry.pinId}/${entry.itemId}?variant=${entry.quality === "preview" ? "preview" : "original"}`);
}

function stopPreviews(node) {
  for (const video of node.papanPanel.querySelectorAll("video")) {
    video.pause(); video.removeAttribute("src"); video.load();
  }
}

function invalidateLiveConnection(id, message) {
  for (const node of activeNodes) {
    const descriptors = files(node).filter(file => file.source.connection_id === id);
    if (!descriptors.length) continue;
    ++node.papanRevision;
    for (const file of descriptors) {
      const session = node.papanSessions.get(file.id);
      node.papanSessions.delete(file.id);
      if (session) void request(`/papan/board/${session.token}`, { method: "DELETE" }).catch(() => {});
    }
    const state = entries(node);
    for (const entry of state) if (entry.connectionId === id) {
      entry.available = false; entry.present = false; delete entry.filename; delete entry.duration;
    }
    saveEntries(node, state); render(node);
    if (descriptors.some(file => file.key === node.properties.papan_selected)) showStatus(node, message, true);
  }
}

async function refreshLiveConnection(id, watcher) {
  if (watcher.refreshing) { watcher.again = true; return; }
  watcher.refreshing = true;
  try {
    do {
      watcher.again = false;
      for (const node of activeNodes) for (const file of files(node).filter(file => file.source.connection_id === id).sort((a, b) => Number(a.key === node.properties.papan_selected) - Number(b.key === node.properties.papan_selected))) {
        await openBoard(node, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(file.source) }, false, false);
      }
    } while (watcher.again && !watcher.controller?.signal.aborted);
  } finally { watcher.refreshing = false; }
}

async function watchLiveConnection(id, watcher) {
  const signal = watcher.controller.signal;
  let cursor = "", delay = 1000;
  while (!signal.aborted) {
    try {
      const response = await api.fetchApi(`/papan/connections/${id}/events`, { signal, headers: cursor ? { "Last-Event-ID": cursor } : {} });
      if (!response.ok) {
        const data = await response.json();
        throw Object.assign(new Error(data.error), { status: response.status });
      }
      const reader = response.body.getReader(), decoder = new TextDecoder();
      let buffer = "";
      try {
        while (!signal.aborted) {
          const { value, done } = await reader.read();
          if (done) throw new Error("Papan disconnected. Keep it open; this board will reconnect.");
          buffer += decoder.decode(value, { stream: true }).replace(/\r/g, "");
          if (buffer.length > 65536) throw new Error("Papan sent an invalid change notification.");
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
            const line = block.split("\n").find(line => line.startsWith("data: "));
            if (!line) continue;
            const event = JSON.parse(line.slice(6));
            if (typeof event.revision !== "string") throw new Error("Papan sent an invalid change notification.");
            delay = 1000;
            if ((cursor || watcher.disconnected) && (event.reset || cursor !== event.revision || watcher.disconnected)) {
              invalidateLiveConnection(id, "Refreshing from Papan…");
              void refreshLiveConnection(id, watcher);
            }
            watcher.disconnected = false;
            cursor = event.revision;
          }
        }
      } finally { await reader.cancel().catch(() => {}); }
    } catch (error) {
      if (signal.aborted) break;
      watcher.disconnected = true;
      invalidateLiveConnection(id, error.message || "Papan disconnected. Keep it open; this board will reconnect.");
      if ([401, 404, 409].includes(error.status)) break;
      await new Promise(resolve => {
        const finish = () => { clearTimeout(timer); signal.removeEventListener("abort", finish); resolve(); };
        const timer = setTimeout(finish, delay); signal.addEventListener("abort", finish, { once: true });
      });
      delay = Math.min(delay * 2, 30000);
    }
  }
}

function syncLiveConnections(restart) {
  const wanted = new Set([...activeNodes].flatMap(node => files(node).map(file => file.source.connection_id).filter(Boolean)));
  for (const [id, watcher] of liveConnections) if (!wanted.has(id) || id === restart) {
    watcher.controller.abort(); liveConnections.delete(id);
  }
  for (const id of wanted) if (!liveConnections.has(id)) {
    const watcher = { controller: new AbortController() }; liveConnections.set(id, watcher);
    void watchLiveConnection(id, watcher);
  }
}

async function connectionPopup(node) {
  const dialog = document.createElement("dialog"); dialog.className = "papan-password papan-connect-dialog";
  dialog.setAttribute("aria-label", "Connect Papan");
  dialog.innerHTML = `<h2>Connect Papan</h2><p>In Papan, open <strong>Connect apps</strong>, enable connections, choose the collections for ComfyUI, then copy the connection.</p><p>Papan and the ComfyUI server must run on the same computer. Unlock protected collections in Papan.</p><div class="papan-connections"></div><form><label>Connection copied from Papan<input type="password" aria-label="Connection copied from Papan" autocomplete="off" spellcheck="false" required></label><p class="papan-replacing" hidden></p><p class="papan-error" role="alert"></p><div class="papan-dialog-actions"><button type="button" class="papan-cancel">Close</button><button type="submit">Connect</button></div></form>`;
  const input = dialog.querySelector("input"), form = dialog.querySelector("form"), submit = form.querySelector('[type="submit"]'), error = dialog.querySelector('[role="alert"]'), list = dialog.querySelector(".papan-connections");
  let replacing = null;
  dialog.onclose = () => { input.value = ""; dialog.remove(); };
  dialog.querySelector(".papan-cancel").onclick = () => dialog.close();
  async function showConnection(connection, collections) {
    const row = document.createElement("section"); row.className = "papan-connection";
    const title = document.createElement("p"); title.textContent = `Papan · ${connection.endpoint}`; row.append(title);
    const select = document.createElement("select"); select.setAttribute("aria-label", `Collections from ${connection.endpoint}`);
    const open = document.createElement("button"); open.type = "button"; open.textContent = "Open collection";
    const status = document.createElement("p"); status.className = "papan-connection-status"; status.setAttribute("role", "status");
    try {
      collections ||= (await request(`/papan/connections/${connection.id}/collections`)).collections;
      for (const collection of collections) {
        const option = document.createElement("option"); option.value = collection.id; option.textContent = collection.name + (collection.locked ? " · locked in Papan" : ""); select.append(option);
      }
      if (!collections.length) status.textContent = "No collections are shared. Change access in Papan.";
    } catch (failure) { status.textContent = failure.message; }
    open.disabled = !select.options.length;
    open.onclick = async () => {
      open.disabled = true;
      const opened = await openBoard(node, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: connection.id, collection_id: select.value }) });
      if (opened) dialog.close(); else status.textContent = node.papanPanel.querySelector(".papan-status").textContent;
      open.disabled = !select.options.length;
    };
    const replace = document.createElement("button"); replace.type = "button"; replace.textContent = "Update connection";
    replace.onclick = () => {
      replacing = connection.id; input.value = "";
      const label = dialog.querySelector(".papan-replacing"); label.hidden = false; label.textContent = `Updating ${connection.endpoint}. Existing workflow connections will be kept.`;
      input.focus();
    };
    const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "Disconnect";
    remove.onclick = async () => {
      remove.disabled = true;
      try {
        await request(`/papan/connections/${connection.id}`, { method: "DELETE" }); row.remove();
        invalidateLiveConnection(connection.id, "Papan disconnected. Connect it again to use these references.");
        liveConnections.get(connection.id)?.controller.abort(); liveConnections.delete(connection.id);
        if (replacing === connection.id) { replacing = null; dialog.querySelector(".papan-replacing").hidden = true; }
      } catch (failure) { status.textContent = failure.message; remove.disabled = false; }
    };
    const actions = document.createElement("div"); actions.className = "papan-connection-actions"; actions.append(open, replace, remove);
    row.append(select, actions, status); list.append(row);
  }
  form.onsubmit = async event => {
    event.preventDefault(); submit.disabled = true; error.textContent = "";
    try {
      let connection;
      try { connection = JSON.parse(input.value); } catch { throw new Error("Paste the complete connection copied from Papan."); }
      input.value = "";
      const result = await request("/papan/connections", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection, id: replacing }) });
      replacing = null; dialog.querySelector(".papan-replacing").hidden = true;
      syncLiveConnections(result.connection.id);
      await refreshLiveConnection(result.connection.id, liveConnections.get(result.connection.id) || {});
      list.replaceChildren();
      const saved = await request("/papan/connections");
      for (const item of saved.connections) await showConnection(item, item.id === result.connection.id ? result.collections : null);
    } catch (failure) { error.textContent = failure.message; input.focus(); }
    finally { submit.disabled = false; }
  };
  document.body.append(dialog); dialog.showModal(); input.focus();
  try {
    const saved = await request("/papan/connections");
    for (const item of saved.connections) await showConnection(item);
  } catch (failure) { error.textContent = failure.message; }
}

function passwordPopup(entry) {
  return new Promise(resolve => {
    const dialog = document.createElement("dialog");
    dialog.className = "papan-password";
    dialog.setAttribute("aria-label", "Unlock Papan board");
    dialog.innerHTML = `<form><h2>Unlock Papan board</h2><p class="papan-password-file"></p><label>Password<input type="password" aria-label="Papan board password" autocomplete="current-password" maxlength="1024" required></label><p class="papan-error" role="alert"></p><div class="papan-dialog-actions"><button type="button">Cancel</button><button type="submit">Unlock</button></div></form>`;
    dialog.querySelector(".papan-password-file").textContent = entry.fileName;
    const input = dialog.querySelector("input"), submit = dialog.querySelector('[type="submit"]');
    let result = null;
    dialog.onclose = () => { input.value = ""; dialog.remove(); resolve(result); };
    dialog.querySelector('[type="button"]').onclick = () => dialog.close();
    dialog.querySelector("form").onsubmit = async event => {
      event.preventDefault();
      submit.disabled = true;
      submit.textContent = "Unlocking…";
      try {
        const password = input.value;
        input.value = "";
        const data = await request(`/papan/unlock/${entry.token}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }) });
        if (!dialog.isConnected) return;
        result = { ...entry, ...data };
        dialog.close();
      } catch (error) {
        if (!dialog.isConnected) return;
        dialog.querySelector('[role="alert"]').textContent = error.message;
        input.focus();
      } finally { submit.disabled = false; submit.textContent = "Unlock"; }
    };
    document.body.append(dialog);
    dialog.showModal();
    input.focus();
  });
}

function connected(node, index) {
  return node.outputs[index]?.links?.length || node.graph?.nodes.some(target =>
    target.properties?.minimax_h3_virtual_media_links?.some(link => Number(link.source_id) === Number(node.id) && Number(link.source_slot) === index));
}

function replaceVideoSource(node, slot) {
  const state = entries(node), previous = node.properties.papan_source_video_slot;
  const previousSeconds = state.findIndex(entry => entry.kind === "seconds" && entry.source_slot === previous);
  const seconds = state.findIndex(entry => entry.kind === "seconds" && entry.source_slot === slot);
  if (state[previous]?.kind !== "video" || state[slot]?.kind !== "video" || previousSeconds < 0 || seconds < 0) throw new Error("Choose a video with a duration output.");
  const graph = node.graph;
  graph.beforeChange();
  for (const [from, to] of [[previous, slot], [previousSeconds, seconds]]) {
    for (const id of [...(node.outputs[from]?.links || [])]) {
      const link = graph.links.get(id);
      node.connect(to, graph.getNodeById(link.target_id), link.target_slot);
    }
  }
  for (const target of graph.nodes) {
    for (const link of target.properties?.minimax_h3_virtual_media_links || []) {
      if (Number(link.source_id) === Number(node.id) && Number(link.source_slot) === previous) link.source_slot = slot;
    }
    for (const part of target.properties?.minimax_h3_prompt_reference_doc?.parts || []) {
      if (part.type === "mention" && Number(part.sourceId) === Number(node.id) && Number(part.sourceSlot) === previous) part.sourceSlot = slot;
    }
  }
  state[previous].selected = false; state[slot].selected = true;
  node.properties.papan_source_video_slot = slot;
  saveEntries(node, state);
  graph.afterChange();
  for (const video of node.papanPanel.querySelectorAll("video")) video.pause();
  node.papanPreviewSlot = null;
  render(node);
}

function positionOutputs(node) {
  const canvas = app.canvas, element = node.papanPanel;
  if (!element.isConnected || !element.getBoundingClientRect().width) return;
  const bounds = canvas.canvas.getBoundingClientRect(), panel = element.getBoundingClientRect(), scale = canvas.ds.scale;
  node.outputs.forEach((output, index) => {
    const dot = element.querySelector(`[data-slot="${index}"] .papan-port-dot`), rect = dot?.getBoundingClientRect();
    if (!rect) { output.pos = [-10000, -10000]; return; }
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    const pos = [(x - bounds.left) / scale - canvas.ds.offset[0] - node.pos[0], (y - bounds.top) / scale - canvas.ds.offset[1] - node.pos[1]];
    if (!output.pos || pos.some((value, axis) => Math.abs(value - output.pos[axis]) > 0.01)) canvas.setDirty(false, true);
    output.pos = pos;
    const anchor = node.papanAnchors[index];
    if (anchor) { anchor.style.left = `${(x - panel.left) / scale}px`; anchor.style.top = `${(y - panel.top) / scale}px`; }
  });
}

function renderOutputs(node) {
  const panel = node.papanPanel, list = panel.querySelector(".papan-outputs"), state = entries(node);
  const durations = new Map(state.map((entry, slot) => [entry.source_slot, slot]).filter(([, slot]) => state[slot].kind === "seconds"));
  const selected = state.map((entry, slot) => ({ entry, slot })).filter(({ entry, slot }) => entry.kind !== "seconds" && (entry.selected || connected(node, slot) || connected(node, durations.get(slot))));
  list.replaceChildren();
  for (const anchor of node.papanAnchors) anchor?.remove();
  node.papanAnchors = [];
  for (const { entry, slot } of selected) {
    const row = document.createElement("div"); row.className = `papan-output${entry.kind === "video" ? " papan-output-video" : ""}`;
    const title = document.createElement("span"); title.className = "papan-output-title"; title.textContent = entry.title; title.title = entry.title;
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "papan-remove"; remove.textContent = "×";
    remove.setAttribute("aria-label", `Remove ${entry.title} output`);
    remove.title = "Deselect media and remove its connections";
    remove.onclick = () => {
      node.graph?.beforeChange();
      const slots = [slot, durations.get(slot)].filter(index => index !== undefined);
      for (const index of slots) node.disconnectOutput(index);
      for (const target of node.graph?.nodes || []) {
        const links = target.properties?.minimax_h3_virtual_media_links;
        if (links?.some(link => Number(link.source_id) === Number(node.id) && slots.includes(Number(link.source_slot)))) {
          target.properties.minimax_h3_virtual_media_links = links.filter(link => Number(link.source_id) !== Number(node.id) || !slots.includes(Number(link.source_slot)));
        }
      }
      const current = entries(node); current[slot].selected = false; saveEntries(node, current);
      node.graph?.afterChange();
      const card = panel.querySelector(`[data-media-slot="${slot}"]`);
      card?.classList.remove("papan-selected");
      const select = entry.poseFor ? panel.querySelector(`[data-pose-slot="${slot}"]`) : card?.querySelector(".papan-select");
      if (select) {
        select.textContent = entry.poseFor ? "Select pose" : "Select"; select.setAttribute("aria-label", `Select ${entry.title}, ${entry.kind}`); select.setAttribute("aria-pressed", "false");
        select.disabled = !canUse(node, entry);
      }
      card?.querySelector("video")?.pause();
      if (node.papanPreviewSlot === slot) { node.papanPreviewSlot = null; card?.classList.remove("papan-highlighted"); }
      renderOutputs(node);
    };
    row.append(remove, title); list.append(row);
    for (const [index, kind] of [[slot, entry.kind], ...(entry.kind === "video" ? [[durations.get(slot), "seconds"]] : [])]) {
      const port = document.createElement("button"); port.type = "button"; port.className = `papan-port papan-${kind}`;
      port.textContent = kind === "seconds" && Number.isFinite(entry.duration) ? `SECONDS · ${entry.duration.toFixed(3)} s` : kind.toUpperCase(); port.dataset.slot = index;
      const dot = document.createElement("span"); dot.className = "papan-port-dot"; dot.setAttribute("aria-hidden", "true"); port.append(dot);
      port.title = kind === "seconds" ? "Video duration in seconds · drag to a FLOAT duration input" : "Drag this output to a media input";
      port.setAttribute("aria-label", `Connect ${entry.title}, ${kind}`);
      port.disabled = !canUse(node, entry);
      port.onpointerdown = event => {
        if (event.button !== 0) return;
        event.preventDefault(); event.stopPropagation(); positionOutputs(node);
        const canvas = app.canvas;
        canvas.adjustMouseEvent(event);
        canvas.pointer.down(event);
        canvas.pointer.isDown = true;
        canvas.mouse = [event.clientX, event.clientY];
        canvas.graph_mouse = [event.canvasX, event.canvasY];
        canvas.linkConnector.dragNewFromOutput(node.graph, node, node.outputs[index]);
        canvas._linkConnectorDrop();
        canvas.setDirty(true, true);
      };
      if (kind === "seconds") {
        const duration = document.createElement("div"); duration.className = "papan-duration";
        const label = document.createElement("span"); label.textContent = "Duration"; duration.append(label, port); row.append(duration);
      } else row.append(port);
      const anchor = document.createElement("span"); anchor.className = "papan-socket-anchor";
      anchor.dataset.slotKey = `${node.id}-out-${index}`; panel.append(anchor); node.papanAnchors[index] = anchor;
    }
  }
  if (!selected.length) {
    const empty = document.createElement("p"); empty.className = "papan-output-empty"; empty.textContent = "Select a preview to add an output here."; list.append(empty);
  }
  node.papanMinHeight = 360 + Math.max(0, selected.length - 1) * 44 + selected.filter(({ entry }) => entry.kind === "video").length * 44;
  panel.style.setProperty("--papan-min-height", `${node.papanMinHeight}px`);
  node.setSize([Math.max(360, node.size[0]), Math.max(node.papanMinHeight + 50, node.size[1])]);
  requestAnimationFrame(() => { positionOutputs(node); app.canvas.setDirty(true, true); });
}

function render(node) {
  const panel = node.papanPanel, grid = panel.querySelector(".papan-grid"), board = panel.querySelector(".papan-board");
  const saved = files(node), selected = saved.find(file => file.key === node.properties.papan_selected), state = entries(node);
  const durations = new Map(state.map((entry, slot) => [entry.source_slot, slot]).filter(([, slot]) => state[slot].kind === "seconds"));
  state.forEach((entry, slot) => {
    if (entry.kind === "video" && !durations.has(slot)) {
      durations.set(slot, state.length); state.push({ kind: "seconds", title: `${entry.title} · seconds`, source_slot: slot });
    }
  });
  saveEntries(node, state);
  board.replaceChildren();
  for (const file of saved) {
    const option = document.createElement("option");
    option.value = file.key; option.textContent = file.name; board.append(option);
  }
  board.value = node.properties.papan_selected || "";
  board.disabled = saved.length < 2;
  const session = selected && node.papanSessions.get(selected.id);
  panel.querySelector(".papan-file-name").textContent = selected?.fileName || "No Papan connected";
  panel.querySelector(".papan-reopen").hidden = !selected;
  panel.querySelector(".papan-reopen").textContent = selected?.live ? "Refresh board" : selected?.encrypted && !session ? "Unlock board" : "Reload board";
  panel.querySelector(".papan-lock").hidden = !session || selected?.live;
  panel.querySelector(".papan-lock").textContent = selected?.encrypted ? "Lock board" : "Close board";
  while (node.outputs.length > state.length) node.removeOutput(node.outputs.length - 1);
  state.forEach((entry, index) => {
    const type = entry.kind === "seconds" ? "FLOAT" : entry.kind === "video" ? "VIDEO" : "IMAGE";
    if (!node.outputs[index]) node.addOutput(entry.title, type);
    node.outputs[index].name = entry.kind === "seconds" ? `${state[entry.source_slot].title} · seconds` : entry.title;
    node.outputs[index].type = type;
    node.outputs[index].pos ||= [node.size[0] - 12, 120];
  });
  node.papanDurationObserver.disconnect();
  stopPreviews(node);
  grid.replaceChildren();
  const media = state.map((entry, slot) => ({ entry, slot })).filter(({ entry }) => entry.kind !== "seconds" && (!selected || entry.boardId === selected.id) && entry.present !== false);
  const poses = new Map(media.filter(({ entry }) => entry.poseFor).map(item => [`${item.entry.boardId}:${item.entry.pinId}:${item.entry.poseFor}`, item]));
  const visible = media.filter(({ entry }) => !entry.poseFor);
  for (const { entry, slot } of visible) {
    const isSelected = entry.selected || connected(node, slot) || connected(node, durations.get(slot));
    const card = document.createElement("article"); card.className = `papan-card${isSelected ? " papan-selected" : ""}${node.papanPreviewSlot === slot ? " papan-highlighted" : ""}`; card.dataset.mediaSlot = slot;
    const preview = document.createElement("img"); preview.alt = entry.title; preview.loading = "lazy";
    const sourceSession = node.papanSessions.get(entry.boardId);
    if (sourceSession && !sourceSession.locked && entry.pinId) preview.src = api.apiURL(`/papan/thumbnail/${sourceSession.token}/${entry.pinId}/${entry.itemId}`);
    else if (entry.filename && !entry.connectionId) preview.src = viewURL(entry);
    const fallback = document.createElement("span"); fallback.className = "papan-placeholder";
    fallback.textContent = selected?.live ? "Unlock this collection in Papan" : selected?.encrypted && !sourceSession ? "Unlock board to show preview" : "Preview unavailable";
    fallback.hidden = !!preview.src;
    preview.onerror = () => { preview.hidden = true; fallback.hidden = false; };
    const picture = document.createElement("div"); picture.className = "papan-picture"; picture.append(preview, fallback);
    picture.tabIndex = 0; picture.setAttribute("role", "button"); picture.setAttribute("aria-label", `Preview ${entry.title}, ${entry.kind}`);
    picture.onkeydown = event => { if (event.target === picture && ["Enter", " "].includes(event.key)) { event.preventDefault(); card.click(); } };
    let startPreview;
    const select = document.createElement("button"); select.type = "button"; select.className = "papan-select";
    select.textContent = isSelected ? "Selected" : "Select"; select.setAttribute("aria-label", `${isSelected ? "Deselect" : "Select"} ${entry.title}, ${entry.kind}`); select.setAttribute("aria-pressed", String(!!isSelected));
    select.disabled = !isSelected && !canUse(node, entry);
    const sourceVideo = entry.kind === "video" && Number.isInteger(node.properties.papan_source_video_slot);
    if (sourceVideo) {
      const current = node.properties.papan_source_video_slot === slot;
      select.textContent = current ? "Source" : "Use source";
      select.setAttribute("aria-label", `${current ? "Current source video" : "Use as source video"}: ${entry.title}`);
      select.setAttribute("aria-pressed", String(current));
      select.disabled ||= current;
    }
    select.onclick = () => {
      if (sourceVideo) {
        try { replaceVideoSource(node, slot); } catch (error) { showStatus(node, error.message, true); }
        return;
      }
      const current = entries(node);
      if (current[slot].selected || connected(node, slot) || connected(node, durations.get(slot))) {
        panel.querySelector(`[data-slot="${slot}"]`).closest(".papan-output").querySelector(".papan-remove").click();
        return;
      }
      node.graph?.beforeChange();
      current[slot].selected = true; saveEntries(node, current);
      node.graph?.afterChange();
      card.classList.add("papan-selected"); select.textContent = "Selected"; select.setAttribute("aria-label", `Deselect ${entry.title}, ${entry.kind}`); select.setAttribute("aria-pressed", "true");
      renderOutputs(node);
      card.click();
    };
    card.onclick = event => {
      if (event.target.closest("button")) return;
      node.papanPreviewSlot = slot;
      for (const other of grid.querySelectorAll(".papan-card")) other.classList.toggle("papan-highlighted", other === card);
      for (const video of grid.querySelectorAll("video")) if (!card.contains(video)) video.pause();
      if (startPreview && !event.target.closest("video")) void startPreview();
    };
    if (entry.kind === "video") {
      const play = document.createElement("button"); play.className = "papan-play"; play.type = "button"; play.textContent = "▶"; play.setAttribute("aria-label", `Play ${entry.title}`);
      play.disabled = !canUse(node, entry);
      startPreview = async () => {
        if (play.disabled) return;
        const existing = picture.querySelector("video");
        if (existing) { void existing.play().catch(() => {}); return; }
        play.disabled = true;
        try {
          let filename = entry.filename;
          if (!filename && !entry.connectionId) {
            const result = await request(`/papan/import/${sourceSession.token}/${entry.pinId}/${entry.itemId}`, { method: "POST" });
            if (!picture.isConnected) return;
            filename = `${result.subfolder}/${result.name}`;
            entry.filename = filename;
            const current = entries(node); current[slot].filename = filename; saveEntries(node, current);
          }
          if (!picture.isConnected || node.papanPreviewSlot !== slot) return;
          const video = document.createElement("video"); video.src = entry.connectionId ? liveMediaURL(entry) : viewURL({ filename }); video.controls = true; video.muted = true; video.loop = true;
          video.onerror = () => { if (video.isConnected) showStatus(node, "Video unavailable. Check that its collection is unlocked in Papan and refresh the board.", true); };
          video.onloadedmetadata = () => {
            if (!video.isConnected || !Number.isFinite(video.duration) || video.duration <= 0) return;
            const current = entries(node);
            if (current[slot].duration === video.duration) return;
            current[slot].duration = video.duration; saveEntries(node, current);
            card.querySelector(".papan-card-duration").textContent = `${video.duration.toFixed(3)} s`;
            if (current[slot].selected || connected(node, slot) || connected(node, durations.get(slot))) renderOutputs(node);
          };
          picture.replaceChildren(video); void video.play().catch(() => {});
        } catch (error) { if (picture.isConnected) showStatus(node, error.message, true); }
        finally { play.disabled = false; }
      };
      play.onclick = () => card.click();
      picture.append(play);
    }
    const footer = document.createElement("div"); footer.className = "papan-card-footer";
    const title = document.createElement("span"); title.className = "papan-card-title"; title.textContent = entry.title; title.title = entry.title;
    footer.append(title);
    if (entry.kind === "video") {
      const duration = document.createElement("span"); duration.className = "papan-card-duration";
      duration.textContent = Number.isFinite(entry.duration) ? `${entry.duration.toFixed(3)} s` : "— s";
      duration.title = "Video duration in seconds"; footer.append(duration);
      if (!Number.isFinite(entry.duration) && sourceSession && entry.available !== false) {
        card.papanLoadDuration = async () => {
          try {
            const result = await request(`/papan/duration/${sourceSession.token}/${entry.pinId}/${entry.itemId}`);
            if (!card.isConnected) return;
            const current = entries(node);
            if (current[slot].duration === result.seconds) return;
            current[slot].duration = result.seconds; saveEntries(node, current);
            duration.textContent = `${result.seconds.toFixed(3)} s`;
            if (current[slot].selected || connected(node, slot) || connected(node, durations.get(slot))) renderOutputs(node);
          } catch (error) { if (card.isConnected) duration.title = error.message; }
        };
        node.papanDurationObserver.observe(card);
      }
    }
    footer.append(select); card.append(picture, footer); grid.append(card);
    const pose = poses.get(`${entry.boardId}:${entry.pinId}:${entry.itemId}`);
    if (pose) {
      const row = document.createElement("div"); row.className = "papan-pose-row";
      const badge = document.createElement("span"); badge.className = "papan-pose-badge"; badge.textContent = pose.entry.available === false ? "Pose unavailable" : "Pose available";
      const poseSelect = document.createElement("button"); poseSelect.type = "button"; poseSelect.className = "papan-pose-select"; poseSelect.dataset.poseSlot = pose.slot;
      const poseSelected = pose.entry.selected || connected(node, pose.slot) || connected(node, durations.get(pose.slot));
      poseSelect.textContent = poseSelected ? "Pose selected" : "Select pose"; poseSelect.setAttribute("aria-label", `${poseSelected ? "Deselect" : "Select"} ${pose.entry.title}, video`); poseSelect.setAttribute("aria-pressed", String(!!poseSelected));
      poseSelect.disabled = !poseSelected && !canUse(node, pose.entry);
      poseSelect.onclick = () => {
        const current = entries(node);
        if (current[pose.slot].selected || connected(node, pose.slot) || connected(node, durations.get(pose.slot))) {
          panel.querySelector(`[data-slot="${pose.slot}"]`).closest(".papan-output").querySelector(".papan-remove").click();
          return;
        }
        node.graph?.beforeChange(); current[pose.slot].selected = true; saveEntries(node, current); node.graph?.afterChange();
        poseSelect.textContent = "Pose selected"; poseSelect.setAttribute("aria-label", `Deselect ${pose.entry.title}, video`); poseSelect.setAttribute("aria-pressed", "true");
        renderOutputs(node);
      };
      row.append(badge, poseSelect); card.append(row);
    }
  }
  if (!visible.length) {
    const empty = document.createElement("p"); empty.className = "papan-empty";
    empty.textContent = selected?.live && session?.locked ? "Unlock this collection in Papan. Its previews will appear here automatically." : selected?.live && !session ? "Keep Papan open and connect it to show this collection." : selected ? "This board has no image or video previews." : "Connect Papan to browse your collections, or open a saved Papan file.";
    grid.append(empty);
  }
  renderOutputs(node);
}

function showStatus(node, text, error = false) {
  const status = node.papanPanel.querySelector(".papan-status"); status.textContent = text; status.classList.toggle("papan-error", error);
}

async function openBoard(node, options, askPassword = true, selectBoard = true) {
  const revision = selectBoard ? ++node.papanRevision : node.papanRevision;
  const report = selectBoard || options.body === node.properties.papan_selected;
  if (report) showStatus(node, "Opening Papan board…");
  let entry;
  try {
    entry = await request("/papan/open", options);
    if (entry.locked && !entry.live) {
      if (!askPassword) {
        await request(`/papan/board/${entry.token}`, { method: "DELETE" });
        showStatus(node, "Unlock this board to show all previews. Existing imported references remain usable.");
        render(node); return;
      }
      const unlocked = await passwordPopup(entry);
      if (!unlocked) { await request(`/papan/board/${entry.token}`, { method: "DELETE" }); showStatus(node, "Opening cancelled."); return; }
      entry = unlocked;
    }
    const pins = [];
    let total = entry.locked ? 0 : Infinity;
    while (pins.length < total) {
      const page = await request(`/papan/pins/${entry.token}?offset=${pins.length}&limit=100`);
      pins.push(...page.pins); total = page.total;
      if (report) showStatus(node, `Loading all previews · ${pins.length} of ${total} pins…`);
      if (!page.pins.length) break;
    }
    if (revision !== node.papanRevision || !activeNodes.has(node)) {
      await request(`/papan/board/${entry.token}`, { method: "DELETE" }); return;
    }
    const old = node.papanSessions.get(entry.id);
    if (old) await request(`/papan/board/${old.token}`, { method: "DELETE" }).catch(() => {});
    node.papanSessions.set(entry.id, entry);
    const descriptors = files(node), key = JSON.stringify(entry.source);
    const previous = descriptors.findIndex(file => file.id === entry.id);
    const previousBoard = descriptors.find(file => file.id === entry.id);
    const changed = previousBoard?.revision && previousBoard.revision !== entry.revision;
    const descriptor = { id: entry.id, name: entry.name, revision: entry.revision, fileName: descriptors[previous]?.fileName || entry.fileName, encrypted: entry.encrypted, live: entry.live, source: entry.source, key };
    if (previous < 0) descriptors.push(descriptor); else descriptors[previous] = descriptor;
    node.properties.papan_files = JSON.stringify(descriptors);
    if (selectBoard || !node.properties.papan_selected) node.properties.papan_selected = key;
    const state = entries(node);
    const current = new Map(state.map((item, index) => [`${item.boardId}:${item.pinId}:${item.itemId}`, index]));
    for (const item of state) if (item.boardId === entry.id) {
      item.present = false;
      if (entry.live) { item.available = false; delete item.filename; delete item.duration; }
    }
    for (const pin of pins) for (const item of pin.items) {
      const data = { boardId: entry.id, pinId: pin.id, itemId: item.id, title: item.poseFor ? `${pin.title} · pose` : pin.title, kind: item.kind, poseFor: item.poseFor, available: item.available, present: true,
        ...(entry.live ? { connectionId: entry.source.connection_id, collectionId: entry.source.collection_id, quality: item.quality, ...(Number.isFinite(item.duration) ? { duration: item.duration } : {}) } : {}) };
      const index = current.get(`${entry.id}:${pin.id}:${item.id}`);
      if (index === undefined) state.push(data);
      else {
        if (state[index].kind !== data.kind) throw new Error("A connected preview changed media type. Open that board in a new Papan node.");
        if (changed) { delete state[index].filename; delete state[index].duration; }
        Object.assign(state[index], data);
      }
    }
    const top = node.papanPanel.querySelector(".papan-grid").scrollTop;
    node.graph?.beforeChange(); saveEntries(node, state); node.graph?.afterChange(); render(node);
    if (!selectBoard) node.papanPanel.querySelector(".papan-grid").scrollTop = top;
    syncLiveConnections();
    const previewCount = pins.reduce((count, pin) => count + pin.items.filter(item => !item.poseFor).length, 0), poseCount = pins.reduce((count, pin) => count + pin.items.filter(item => item.poseFor).length, 0);
    if (selectBoard || node.properties.papan_selected === key) showStatus(node, entry.locked ? "Unlock this collection in Papan. It will refresh automatically." : `${entry.live ? "Live · " : ""}${previewCount} previews${poseCount ? ` · ${poseCount} pose attachments` : ""} · select media, then drag its output below`);
    return true;
  } catch (error) {
    if (entry && ![...node.papanSessions.values()].some(session => session.token === entry.token)) await request(`/papan/board/${entry.token}`, { method: "DELETE" }).catch(() => {});
    if (entry?.live) invalidateLiveConnection(entry.source.connection_id, error.message);
    if (report) showStatus(node, error.message, true);
    return false;
  }
}

async function reopen(node, askPassword = true) {
  const descriptor = files(node).find(file => file.key === node.properties.papan_selected);
  if (!descriptor) return;
  const existing = node.papanSessions.get(descriptor.id);
  if (existing && !askPassword) { render(node); return; }
  await openBoard(node, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(descriptor.source) }, askPassword);
}

function prepareNode(node) {
  activeNodes.add(node); node.papanSessions = new Map(); node.papanRevision = 0;
  node.papanAnchors = []; node.papanHeight = 590; node.papanMinHeight = 360;
  node.title = "Papan Board";
  node.widgets_start_y = 8;
  node.getOutputPos = index => node.getConnectionPos(false, index);
  for (const name of ["media_state", "session_tokens"]) {
    const widget = node.widgets.find(widget => widget.name === name);
    widget.type = "hidden"; widget.hidden = true; widget.options.hidden = true; widget.draw = () => {}; widget.computeSize = () => [0, -4];
  }
  const panel = document.createElement("section"); panel.className = "papan-board-node"; node.papanPanel = panel;
  panel.innerHTML = `<div class="papan-toolbar"><button type="button" class="papan-connect">Connect Papan…</button><button type="button" class="papan-open">Open Papan file…</button><input type="file" class="papan-file" accept=".papan,.zip" multiple hidden><button type="button" class="papan-reopen" hidden>Reload board</button><button type="button" class="papan-lock" hidden>Close board</button></div>
    <div class="papan-file-name">No Papan connected</div><label class="papan-board-label">Board<select class="papan-board" aria-label="Papan board" disabled></select></label>
    <details class="papan-server"><summary>Open a file on the ComfyUI server</summary><div><input type="text" class="papan-server-path" aria-label="Papan server file path" placeholder="Full path to a .papan file"><button type="button" class="papan-server-open">Open</button></div></details>
    <p class="papan-status" role="status" aria-live="polite">Connect Papan or open a saved board.</p><div class="papan-grid"></div><section class="papan-output-section" aria-label="Selected media outputs"><p class="papan-output-heading">Outputs</p><div class="papan-outputs"></div></section>`;
  const file = panel.querySelector(".papan-file");
  panel.querySelector(".papan-connect").onclick = () => void connectionPopup(node);
  panel.querySelector(".papan-open").onclick = () => file.click();
  file.onchange = async () => {
    const chosen = [...file.files]; file.value = "";
    for (const item of chosen) { const body = new FormData(); body.append("file", item); await openBoard(node, { method: "POST", body }); }
  };
  panel.querySelector(".papan-server-open").onclick = () => void openBoard(node, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: panel.querySelector(".papan-server-path").value }) });
  panel.querySelector(".papan-reopen").onclick = () => void reopen(node);
  panel.querySelector(".papan-board").onchange = async event => { node.properties.papan_selected = event.target.value; render(node); await reopen(node, false); };
  panel.querySelector(".papan-lock").onclick = async () => {
    const descriptor = files(node).find(file => file.key === node.properties.papan_selected), session = descriptor && node.papanSessions.get(descriptor.id);
    if (!session) return;
    try {
      await request(`/papan/board/${session.token}`, { method: "DELETE" }); node.papanSessions.delete(descriptor.id); render(node);
      showStatus(node, descriptor.encrypted ? "Board locked. Unlock it to show all previews." : "Board closed. Reload it to show all previews.");
    } catch (error) { showStatus(node, error.message, true); }
  };
  const grid = panel.querySelector(".papan-grid");
  node.papanDurationObserver = new IntersectionObserver(changes => {
    for (const change of changes) if (change.isIntersecting) {
      node.papanDurationObserver.unobserve(change.target); void change.target.papanLoadDuration();
    }
  }, { root: grid });
  grid.tabIndex = 0; grid.dataset.captureWheel = "true";
  grid.setAttribute("aria-label", "Papan previews");
  grid.onpointerenter = () => grid.focus({ preventScroll: true });
  grid.onscroll = () => { positionOutputs(node); app.canvas.setDirty(true, true); };
  panel.addEventListener("wheel", event => event.stopPropagation());
  const widget = node.addDOMWidget("papan_board", "papan", panel, { serialize: false, hideOnZoom: false });
  const resizeObserver = new ResizeObserver(() => { positionOutputs(node); app.canvas.setDirty(true, true); });
  resizeObserver.observe(panel);
  resizeObserver.observe(panel.querySelector(".papan-grid"));
  widget.computeSize = width => [width || 620, node.papanHeight];
  node.computeSize = () => [360, node.papanMinHeight + 50];
  const resize = node.onResize;
  node.onResize = function (size) {
    this.papanHeight = Math.max(this.papanMinHeight, size[1] - 50);
    panel.style.setProperty("--papan-height", `${this.papanHeight}px`);
    resize?.apply(this, arguments);
    requestAnimationFrame(() => { positionOutputs(this); app.canvas.setDirty(true, true); });
  };
  node.drawSlots = () => {};
  const draw = node.onDrawForeground, removed = node.onRemoved, executed = node.onExecuted, connections = node.onConnectionsChange;
  node.onConnectionsChange = function () {
    connections?.apply(this, arguments);
    queueMicrotask(() => { if (activeNodes.has(this)) renderOutputs(this); });
  };
  node.onDrawForeground = function () { draw?.apply(this, arguments); positionOutputs(this); };
  node.onRemoved = function () {
    stopPreviews(this);
    resizeObserver.disconnect();
    node.papanDurationObserver.disconnect();
    activeNodes.delete(this); ++this.papanRevision;
    syncLiveConnections();
    for (const session of this.papanSessions.values()) void request(`/papan/board/${session.token}`, { method: "DELETE" }).catch(() => {});
    removed?.apply(this, arguments);
  };
  node.onExecuted = function (message) {
    executed?.apply(this, arguments);
    const state = entries(this);
    for (const item of message.papan_imported || []) {
      const current = state[item.slot];
      if (current && !current.connectionId && current.boardId === item.boardId && current.pinId === item.pinId && current.itemId === item.itemId) current.filename = item.filename;
    }
    for (const item of message.papan_durations || []) if (state[item.slot]?.kind === "video") state[item.slot].duration = item.seconds;
    saveEntries(this, state); render(this);
  };
  node.setSize([620, 640]); render(node);
}

window.addEventListener("pagehide", () => {
  for (const watcher of liveConnections.values()) watcher.controller.abort();
  for (const node of activeNodes) for (const session of node.papanSessions.values()) void fetch(api.apiURL(`/papan/board/${session.token}`), { method: "DELETE", keepalive: true }).catch(() => {});
});

app.registerExtension({
  name: "Papan.BoardNode",
  setup() {
    const renderSegments = app.canvas._renderAllLinkSegments;
    app.canvas._renderAllLinkSegments = function (ctx, link, start, ...rest) {
      const node = this.graph?.getNodeById(link.origin_id);
      if (activeNodes.has(node)) start = node.getOutputPos(link.origin_slot);
      return renderSegments.call(this, ctx, link, start, ...rest);
    };
    const overlay = document.createElement("canvas"); overlay.className = "papan-wire-overlay";
    overlay.setAttribute("aria-hidden", "true"); app.canvas.canvas.parentElement.append(overlay);
    const context = overlay.getContext("2d"), drawOverlay = app.canvas.onDrawOverlay;
    app.canvas.onDrawOverlay = function (ctx) {
      drawOverlay?.apply(this, arguments);
      if (overlay.width !== this.canvas.width) overlay.width = this.canvas.width;
      if (overlay.height !== this.canvas.height) overlay.height = this.canvas.height;
      context.resetTransform(); context.clearRect(0, 0, overlay.width, overlay.height);
      overlay.hidden = !activeNodes.size;
      if (overlay.hidden) return;
      context.save(); context.setTransform(ctx.getTransform()); this.ds.toCanvasContext(context);
      context.globalAlpha = this.editor_alpha;
      const painted = new Set();
      for (const link of this.graph?.links.values() || []) {
        if (!activeNodes.has(this.graph.getNodeById(link.origin_id)) || link._dragging) continue;
        const reroutes = this.graph.reroutes.get(link.parentId)?.getReroutes() || [];
        for (const segment of [...reroutes, link]) {
          if (!this.renderedPaths.has(segment) || !segment.path || segment._dragging || painted.has(segment)) continue;
          painted.add(segment);
          if (this.render_connections_border) {
            context.lineWidth = this.connections_width + 4; context.strokeStyle = "#0008"; context.stroke(segment.path);
          }
          context.lineWidth = this.connections_width;
          context.strokeStyle = this.highlighted_links[link.id] ? "#fff" : link.color || this.constructor.link_type_colors[link.type] || this.default_link_color;
          context.stroke(segment.path);
        }
      }
      if (this.links_render_mode !== window.LiteGraph.HIDDEN_LINK) {
        for (const target of this.graph?.nodes || []) {
          const input = target.inputs.findIndex(input => input.name === "media");
          if (input < 0) continue;
          for (const link of target.properties?.minimax_h3_virtual_media_links || []) {
            const source = this.graph.getNodeById(Number(link.source_id)), slot = Number(link.source_slot);
            if (!activeNodes.has(source) || !source.outputs[slot]) continue;
            const start = source.getOutputPos(slot), end = target.getInputPos(input), path = new Path2D();
            path.moveTo(...start); path.bezierCurveTo(start[0] + 80, start[1], end[0] - 80, end[1], ...end);
            if (this.render_connections_border) {
              context.lineWidth = this.connections_width + 4; context.strokeStyle = "#0008"; context.stroke(path);
            }
            context.lineWidth = this.connections_width;
            context.strokeStyle = source.selected || target.selected ? "#fff" : this.constructor.link_type_colors[link.source_type || source.outputs[slot].type] || this.default_link_color;
            context.stroke(path);
          }
        }
      }
      if (this.linkConnector.renderLinks.some(link => activeNodes.has(link.node))) this._drawConnectingLinks(context);
      context.restore();
    };
    const original = app.graphToPrompt;
    app.graphToPrompt = async function () {
      const result = await original.apply(this, arguments);
      for (const node of activeNodes) {
        const output = result.output?.[String(node.id)];
        if (output) output.inputs.session_tokens = JSON.stringify(Object.fromEntries([...node.papanSessions].map(([id, session]) => [id, session.token])));
      }
      return result;
    };
  },
  nodeCreated(node) { if (node.comfyClass === "PapanReferences") prepareNode(node); },
  loadedGraphNode(node) {
    if (node.comfyClass === "PapanReferences") {
      const state = entries(node);
      for (const entry of state) if (entry.connectionId) { entry.present = false; entry.available = false; delete entry.filename; }
      saveEntries(node, state); render(node);
      void (async () => {
        const descriptors = files(node).filter(file => file.live).sort((a, b) => Number(a.key === node.properties.papan_selected) - Number(b.key === node.properties.papan_selected));
        for (const file of descriptors) await openBoard(node, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(file.source) }, false, false);
        if (!files(node).find(file => file.key === node.properties.papan_selected)?.live) await reopen(node, false);
      })();
      syncLiveConnections();
    }
  },
});
