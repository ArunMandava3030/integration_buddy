"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = { csrf: "", status: null, documents: [], selectedDocument: null, files: [], history: [], chatting: false, uploading: false, connected: false, source: null, deleting: null, dismissedJob: null, lastJobKey: "", statusBusy: false, documentRequest: 0, sourceRequest: 0, folderPrefilled: false };
  let toastTimer;
  let lastDocumentRefresh = 0;
  const runningStates = new Set(["queued", "pending", "running", "processing", "indexing", "started"]);
  const busyJob = () => Boolean(state.status?.job && runningStates.has(String(state.status.job.state).toLowerCase()));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function icon(name) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.classList.add("icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#icon-${name}`);
    svg.append(use);
    return svg;
  }
  function bytes(value) {
    let amount = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return `${amount.toLocaleString(undefined, { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
  }
  function readableError(error) {
    if (error instanceof TypeError) return "The local server could not be reached. Check that CPI Chat is running, then try again.";
    return error?.message || "Something went wrong. Please try again.";
  }
  function toast(message) {
    $("toast").textContent = message;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5000);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== "GET") headers.set("X-CSRF-Token", state.csrf);
    if (options.json !== undefined) { headers.set("Content-Type", "application/json"); options.body = JSON.stringify(options.json); }
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : { error: await response.text() };
    if (!response.ok) {
      const errorValue = data.error || data.detail || data.message;
      throw new Error(typeof errorValue === "string" ? errorValue : errorValue?.message || `Request failed (${response.status}).`);
    }
    return data;
  }
  function setDocument(id) {
    state.selectedDocument = id === null ? null : state.documents.find((doc) => String(doc.id) === String(id))?.id ?? null;
    renderDocuments();
    renderContext();
    $("uploadDialog").close();
    $("question").focus();
  }
  function renderContext() {
    const selected = state.documents.find((doc) => String(doc.id) === String(state.selectedDocument));
    $("contextLabel").textContent = selected ? `Searching ${selected.name}` : "Searching all documents";
    $("contextLabel").title = selected?.name || "Searching all documents";
    $("clearContextButton").hidden = !selected;
    $("composerContext").hidden = !selected;
  }
  function renderDocuments() {
    const query = $("documentSearch").value.trim().toLocaleLowerCase();
    const documents = state.documents.filter((doc) => `${doc.name} ${Object.values(doc.metadata || {}).join(" ")}`.toLocaleLowerCase().includes(query));
    const list = $("documentList");
    list.replaceChildren();
    $("documentCount").textContent = state.documents.length.toLocaleString();
    $("allDocumentsButton").classList.toggle("selected", state.selectedDocument === null);
    $("allDocumentsButton").setAttribute("aria-pressed", String(state.selectedDocument === null));
    $("libraryEmpty").hidden = state.documents.length > 0;
    list.hidden = state.documents.length === 0;
    if (state.documents.length && !documents.length) list.append(element("p", "no-results", "No documents match your search."));
    for (const doc of documents.slice(0, 200)) {
      const selected = String(doc.id) === String(state.selectedDocument);
      const row = element("div", `document-row${selected ? " selected" : ""}`);
      const select = element("button", "document-select");
      select.type = "button";
      select.title = doc.name;
      select.setAttribute("aria-pressed", String(selected));
      const info = element("span", "document-info");
      info.append(element("span", "document-name", doc.name), element("span", "document-detail", `${bytes(doc.size)} · ${Number(doc.chunks || 0).toLocaleString()} passages${doc.metadata?.environment ? ` · ${doc.metadata.environment}` : ""}`));
      select.append(icon("file"), info);
      select.addEventListener("click", () => setDocument(doc.id));
      const remove = element("button", "icon-button document-delete");
      remove.type = "button";
      remove.setAttribute("aria-label", `Remove ${doc.name}`);
      remove.title = "Remove from library";
      remove.disabled = busyJob() || state.uploading || state.chatting;
      remove.append(icon("trash"));
      remove.addEventListener("click", () => requestDelete(doc));
      row.append(select, remove);
      list.append(row);
    }
    if (documents.length > 200) list.append(element("p", "no-results", `Showing 200 of ${documents.length.toLocaleString()} documents. Search to narrow the list.`));
  }
  async function refreshDocuments() {
    const request = ++state.documentRequest;
    try {
      const data = await api("/api/documents");
      if (request !== state.documentRequest) return;
      state.documents = Array.isArray(data.documents) ? data.documents : [];
      lastDocumentRefresh = Date.now();
      if (state.selectedDocument !== null && !state.documents.some((doc) => String(doc.id) === String(state.selectedDocument))) state.selectedDocument = null;
      renderDocuments();
      renderContext();
    } catch (error) { toast(readableError(error)); }
  }
  function renderModels() {
    const availableModels = (state.status?.ollama?.models || []).map((model) => typeof model === "string" ? model : model.name).filter(Boolean);
    const hasModels = Boolean(state.status?.ollama?.available && availableModels.length);
    const select = $("modelSelect");
    const selected = state.status?.settings?.model;
    const key = `${hasModels}|${availableModels.join("|")}`;
    if (select.dataset.optionsKey !== key) {
      select.replaceChildren();
      if (!hasModels) select.append(new Option("Document search", ""));
      else for (const model of availableModels) select.append(new Option(model, model));
      select.dataset.optionsKey = key;
    }
    if (hasModels && availableModels.includes(selected)) select.value = selected;
    select.disabled = !hasModels || state.chatting || !state.connected;
    $("connectionBadge").classList.toggle("search-only", !hasModels && state.connected);
    if (state.connected) $("connectionText").textContent = hasModels ? "Local" : "Search only";
    const count = Number(state.status?.stats?.documents || 0);
    if (!state.connected) $("modelNotice").textContent = "Waiting for the local server. Your conversation is still here.";
    else if (!hasModels) $("modelNotice").textContent = "Document search mode · No local model is available. You’ll get relevant source passages, without a generated answer.";
    $("modelNotice").hidden = state.connected && (count === 0 || hasModels);
    $("welcomeSetup").hidden = count > 0;
    $("question").disabled = !state.connected || count === 0;
    $("question").placeholder = count ? "Ask about your iFlows…" : "Add documents to start chatting…";
    updateSendButton();
  }
  function renderJob() {
    const job = state.status?.job;
    const banner = $("indexBanner");
    if (!job || state.dismissedJob === String(job.id)) { banner.hidden = true; return; }
    const running = busyJob();
    banner.hidden = false;
    banner.classList.toggle("done", !running);
    banner.classList.toggle("failed", ["failed", "error"].includes(String(job.state).toLowerCase()));
    $("dismissIndexButton").hidden = running;
    const total = Number(job.total || 0);
    const processed = Number(job.processed || 0);
    $("indexTitle").textContent = running ? "Indexing documentation" : ["failed", "error"].includes(String(job.state).toLowerCase()) ? "Import needs attention" : "Library updated";
    $("indexCounts").textContent = running ? `${processed.toLocaleString()}${total ? ` / ${total.toLocaleString()}` : ""} files` : `${Number(job.indexed || 0)} indexed · ${Number(job.unchanged || 0)} unchanged${job.failed ? ` · ${job.failed} failed` : ""}`;
    $("indexProgress").max = total || 1;
    if (total) $("indexProgress").value = Math.min(processed, total);
    else if (running) $("indexProgress").removeAttribute("value");
    else $("indexProgress").value = 1;
    $("indexProgress").hidden = !running;
    $("indexCurrent").textContent = String(running ? job.current || job.message || "Preparing the searchable library…" : job.message || "Your documents are ready to search.");
    const errors = Array.isArray(job.errors) ? job.errors : [];
    $("indexErrorsDetails").hidden = !errors.length;
    $("indexErrorsSummary").textContent = `${errors.length} import ${errors.length === 1 ? "detail" : "details"}`;
    $("indexErrors").replaceChildren(...errors.map((error) => element("li", "", typeof error === "string" ? error : [error.name || error.file || error.path, error.error || error.message].filter(Boolean).join(": ") || JSON.stringify(error))));
  }
  function updateUploadControls() {
    const blocked = state.uploading || busyJob() || !state.connected;
    $("uploadActionRow").hidden = !state.files.length && !state.uploading;
    $("importFilesButton").disabled = blocked || !state.files.length;
    $("importFolderButton").disabled = blocked;
    $("chooseFilesButton").disabled = state.uploading;
    $("chooseFolderButton").disabled = state.uploading;
    $("clearFilesButton").disabled = state.uploading;
    if (busyJob() && !state.uploading) $("uploadHint").textContent = "An import is running. You can add more files when it finishes.";
    else if (!state.uploading) $("uploadHint").textContent = state.files.length ? `${state.files.length.toLocaleString()} files · ${bytes(state.files.reduce((sum, file) => sum + file.size, 0))}` : "Select files to begin.";
  }
  async function refreshStatus(forceDocuments = false) {
    if (state.statusBusy) return;
    state.statusBusy = true;
    try {
      const previousCount = state.status?.stats?.documents;
      const previousBusy = busyJob();
      const data = await api("/api/status");
      state.status = data;
      state.csrf = data.csrf_token || state.csrf;
      state.connected = true;
      $("connectionBadge").classList.remove("disconnected");
      $("connectionNotice").hidden = !data.notice;
      if (data.notice) $("connectionNotice").textContent = data.notice;
      if (!state.folderPrefilled) {
        if (!$("folderPath").value.trim() && data.settings?.documents_folder) $("folderPath").value = data.settings.documents_folder;
        state.folderPrefilled = true;
      }
      if (Array.isArray(data.supported_extensions)) $("fileInput").accept = data.supported_extensions.join(",");
      const stats = data.stats || {};
      $("storageSummary").textContent = `${Number(stats.documents || 0).toLocaleString()} documents · ${bytes(stats.bytes)}`;
      const semanticReady = Boolean(data.settings?.semantic_search && data.semantic?.available && Number(data.semantic?.chunks));
      $("searchSummary").textContent = semanticReady ? "Search: keywords + meaning" : "Search: keywords";
      $("searchSummary").title = semanticReady ? `${Number(data.semantic.chunks).toLocaleString()} passages indexed for semantic search${data.semantic.model ? ` with ${data.semantic.model}` : ""}.` : "Keyword search is available. Semantic search becomes available after local embeddings are indexed.";
      renderModels();
      renderJob();
      updateUploadControls();
      const jobKey = `${data.job?.id || ""}:${data.job?.state || ""}:${data.job?.processed || 0}`;
      if (forceDocuments || previousCount !== stats.documents || (previousBusy && !busyJob()) || (jobKey !== state.lastJobKey && busyJob()) || Date.now() - lastDocumentRefresh > 30000) await refreshDocuments();
      state.lastJobKey = jobKey;
    } catch (error) {
      state.connected = false;
      $("connectionBadge").classList.add("disconnected");
      $("connectionText").textContent = "Disconnected";
      $("connectionNotice").textContent = readableError(error);
      $("connectionNotice").hidden = false;
      renderModels();
      updateUploadControls();
    } finally { state.statusBusy = false; }
  }
  function updateSendButton() {
    $("sendButton").disabled = state.chatting || !state.connected || !Number(state.status?.stats?.documents || 0) || !$("question").value.trim();
    $("sendButton").setAttribute("aria-label", state.chatting ? "Waiting for answer" : "Send question");
    $("newChatButton").disabled = state.chatting;
  }
  function resizeQuestion() {
    $("question").style.height = "auto";
    $("question").style.height = `${Math.min($("question").scrollHeight, 180)}px`;
    updateSendButton();
  }
  function scrollChat() { $("chatScroll").scrollTop = $("chatScroll").scrollHeight; }
  function addInline(parent, text) {
    const tokens = String(text).split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
    for (const token of tokens) {
      if (token.startsWith("**") && token.endsWith("**") && token.length > 4) parent.append(element("strong", "", token.slice(2, -2)));
      else if (token.startsWith("`") && token.endsWith("`") && token.length > 2) parent.append(element("code", "", token.slice(1, -1)));
      else parent.append(document.createTextNode(token));
    }
  }
  function renderAnswer(container, text) {
    const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
    let paragraph = [];
    let list = null;
    let code = null;
    const flush = () => {
      if (paragraph.length) { const p = element("p"); addInline(p, paragraph.join("\n")); p.style.whiteSpace = "pre-wrap"; container.append(p); paragraph = []; }
      list = null;
    };
    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        if (code !== null) { const pre = element("pre"); pre.append(element("code", "", code.join("\n"))); container.append(pre); code = null; }
        else { flush(); code = []; }
        continue;
      }
      if (code !== null) { code.push(line); continue; }
      if (!line.trim()) { flush(); continue; }
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      if (heading) { flush(); const h = element("h3"); addInline(h, heading[1]); container.append(h); continue; }
      const bullet = line.match(/^\s*(?:([-*])|\d+[.)])\s+(.+)$/);
      if (bullet) {
        if (paragraph.length) flush();
        const type = bullet[1] ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== type) { list = element(type); container.append(list); }
        const item = element("li"); addInline(item, bullet[2]); list.append(item); continue;
      }
      list = null;
      paragraph.push(line);
    }
    flush();
    if (code !== null) { const pre = element("pre"); pre.append(element("code", "", code.join("\n"))); container.append(pre); }
  }
  function assistantHeading() {
    return element("div", "message-heading", "CPI Chat");
  }
  function renderSources(parent, sources) {
    if (!sources.length) return;
    const details = element("details", "message-sources");
    details.append(element("summary", "", `Sources (${sources.length})`));
    const cards = element("div", "source-cards");
    sources.forEach((source, index) => {
      const card = element("button", "source-card"); card.type = "button";
      card.setAttribute("aria-label", `Open source ${index + 1}: ${source.name}`);
      const info = element("span", "source-card-info");
      info.append(element("strong", "", source.name || "Document"), element("span", "source-card-location", source.location || "Indexed passage"));
      card.append(element("span", "source-number", index + 1), info);
      card.addEventListener("click", () => openSource(source));
      cards.append(card);
    });
    details.append(cards);
    parent.append(details);
  }
  async function submitQuestion(event) {
    event.preventDefault();
    const question = $("question").value.trim();
    if (!question || state.chatting || !state.connected || !Number(state.status?.stats?.documents)) return;
    state.chatting = true;
    $("welcome").hidden = true;
    $("conversation").hidden = false;
    const user = element("article", "message message-user"); user.setAttribute("aria-label", "Your question");
    user.append(element("div", "message-content", question));
    $("conversation").append(user);
    const assistant = element("article", "message message-assistant"); assistant.setAttribute("aria-label", "CPI Chat answer");
    assistant.setAttribute("aria-busy", "true"); assistant.append(assistantHeading());
    const thinking = element("div", "thinking");
    const dots = element("span", "thinking-dots"); dots.setAttribute("aria-hidden", "true"); dots.append(element("i"), element("i"), element("i"));
    const hasModel = Boolean($("modelSelect").value);
    const thinkingText = element("span", "", "Finding relevant documentation");
    const elapsed = element("span", "thinking-elapsed", "0s");
    thinking.append(dots, thinkingText, elapsed);
    assistant.append(thinking);
    const help = element("p", "thinking-help", hasModel ? "A local model may take a few minutes, especially on its first request." : "Searching indexed passages on this computer.");
    assistant.append(help);
    $("conversation").append(assistant);
    $("question").value = "";
    resizeQuestion(); renderModels(); renderDocuments(); scrollChat();
    const start = Date.now();
    const timer = setInterval(() => {
      const seconds = Math.floor((Date.now() - start) / 1000);
      elapsed.textContent = seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
      if (seconds >= 8 && hasModel) thinkingText.textContent = "Preparing your local answer";
    }, 1000);
    try {
      const result = await api("/api/chat", { method: "POST", json: { question, model: $("modelSelect").value || state.status?.settings?.model || "", document_id: state.selectedDocument, history: state.history.slice(-12) } });
      assistant.replaceChildren(assistantHeading());
      if (result.notice) assistant.append(element("p", "message-notice", result.notice));
      if (result.mode === "search" && !result.notice) assistant.append(element("p", "message-notice", "These are matching passages from your documentation. No AI answer was generated."));
      const content = element("div", "message-content");
      renderAnswer(content, result.answer || "No matching information was returned. Try a specific iFlow name, endpoint, or field name.");
      assistant.append(content);
      renderSources(assistant, Array.isArray(result.sources) ? result.sources : []);
      const actions = element("div", "message-actions");
      const copy = element("button", "copy-button"); copy.type = "button"; copy.append(icon("copy"), document.createTextNode("Copy answer"));
      copy.addEventListener("click", async () => { try { await navigator.clipboard.writeText(String(result.answer || "")); toast("Answer copied."); } catch { toast("Copy is unavailable in this browser. Select the answer text to copy it."); } });
      actions.append(copy); assistant.append(actions);
      state.history.push({ role: "user", content: question }, { role: "assistant", content: String(result.answer || "") });
    } catch (error) {
      assistant.replaceChildren(assistantHeading());
      assistant.append(element("p", "message-error", readableError(error)));
      const retry = element("button", "text-button", "Put question back in the composer");
      retry.addEventListener("click", () => { $("question").value = question; resizeQuestion(); $("question").focus(); });
      assistant.append(retry);
    } finally {
      clearInterval(timer);
      assistant.removeAttribute("aria-busy");
      state.chatting = false;
      renderModels(); renderDocuments(); scrollChat();
      $("question").focus();
    }
  }
  function newConversation() {
    if (state.chatting) return;
    state.history = [];
    $("conversation").replaceChildren();
    $("conversation").hidden = true;
    $("welcome").hidden = false;
    $("question").value = "";
    resizeQuestion();
    $("chatScroll").scrollTop = 0;
    $("question").focus();
  }
  function openUpload() {
    $("uploadFeedback").hidden = true;
    updateUploadControls();
    if (!$("uploadDialog").open) $("uploadDialog").showModal();
  }
  function renderFiles() {
    $("selectedFilesPanel").hidden = !state.files.length;
    $("selectedFilesHeading").textContent = `${state.files.length.toLocaleString()} ${state.files.length === 1 ? "file" : "files"} selected`;
    const list = $("selectedFilesList"); list.replaceChildren();
    state.files.slice(0, 30).forEach((file) => { const row = element("li"); row.append(element("span", "", file.webkitRelativePath || file.name), element("span", "", bytes(file.size))); list.append(row); });
    if (state.files.length > 30) list.append(element("li", "", `And ${(state.files.length - 30).toLocaleString()} more files…`));
    updateUploadControls();
  }
  function addFiles(files) {
    if (state.uploading) return;
    const existing = new Set(state.files.map((file) => `${file.webkitRelativePath || file.name}|${file.size}|${file.lastModified}`));
    const selectedNames = new Set(state.files.map((file) => (file.webkitRelativePath || file.name).replace(/\\/g, "/").toLocaleLowerCase()));
    const supported = new Set(state.status?.supported_extensions || []);
    const uploadLimit = Number(state.status?.max_upload_bytes || 256 * 1024 * 1024);
    const unsupported = [];
    const oversized = [];
    const empty = [];
    const conflicting = [];
    for (const file of files) {
      const suffix = file.name.includes(".") ? `.${file.name.split(".").pop().toLowerCase()}` : "";
      if (supported.size && !supported.has(suffix)) { unsupported.push(file.name); continue; }
      if (!file.size) { empty.push(file.name); continue; }
      if (file.size > uploadLimit) { oversized.push(file.name); continue; }
      const key = `${file.webkitRelativePath || file.name}|${file.size}|${file.lastModified}`;
      if (existing.has(key)) continue;
      const relativeName = (file.webkitRelativePath || file.name).replace(/\\/g, "/").toLocaleLowerCase();
      if (selectedNames.has(relativeName)) { conflicting.push(file.webkitRelativePath || file.name); continue; }
      state.files.push(file); existing.add(key); selectedNames.add(relativeName);
    }
    renderFiles();
    const notices = [];
    if (unsupported.length) notices.push(`Skipped ${unsupported.length} unsupported ${unsupported.length === 1 ? "file" : "files"} (${unsupported.slice(0, 3).join(", ")}${unsupported.length > 3 ? ", …" : ""}).`);
    if (empty.length) notices.push(`Skipped ${empty.length} empty ${empty.length === 1 ? "file" : "files"}.`);
    if (oversized.length) notices.push(`${oversized.length} ${oversized.length === 1 ? "file exceeds" : "files exceed"} the ${bytes(uploadLimit)} upload limit. Import a local folder path for larger files.`);
    if (conflicting.length) notices.push(`Skipped ${conflicting.length} duplicate ${conflicting.length === 1 ? "path" : "paths"} (${conflicting.slice(0, 3).join(", ")}). Use Choose folder to preserve subfolder names, or rename the files before adding them.`);
    if (notices.length) uploadFeedback(notices.join(" "));
    else $("uploadFeedback").hidden = true;
  }
  function metadata() {
    const result = {};
    const iflow = $("metadataIflow").value.trim();
    const packageName = $("metadataPackage").value.trim();
    const environment = $("metadataEnvironment").value;
    if (iflow) result.iflow = iflow;
    if (packageName) result.package = packageName;
    if (environment) result.environment = environment;
    return result;
  }
  function uploadFeedback(message, success = false) {
    $("uploadFeedback").textContent = message;
    $("uploadFeedback").classList.toggle("success", success);
    $("uploadFeedback").hidden = false;
  }
  function applyJob(result) {
    if (result.job) { state.status = { ...state.status, job: result.job }; state.dismissedJob = null; renderJob(); }
    updateUploadControls(); renderDocuments();
    $("uploadDialog").close();
    toast("Import started. You can follow its progress above the conversation.");
    refreshStatus(true);
  }
  async function importFiles() {
    if (!state.files.length || state.uploading || busyJob()) return;
    state.uploading = true; updateUploadControls(); renderDocuments();
    $("uploadFeedback").hidden = true;
    const files = state.files.slice();
    const ids = [];
    try {
      for (let index = 0; index < files.length; index += 1) {
        const file = files[index];
        $("uploadHint").textContent = `Uploading ${index + 1} of ${files.length}…`;
        uploadFeedback(`Uploading ${file.webkitRelativePath || file.name}`, true);
        const result = await api(`/api/upload?name=${encodeURIComponent(file.webkitRelativePath || file.name)}`, { method: "POST", body: file, headers: { "Content-Type": "application/octet-stream" } });
        ids.push(result.id);
      }
      $("uploadHint").textContent = "Starting document indexing…";
      const result = await api("/api/import-uploaded", { method: "POST", json: { ids, metadata: metadata() } });
      state.files = []; $("fileInput").value = ""; $("folderInput").value = ""; renderFiles();
      applyJob(result);
    } catch (error) { uploadFeedback(readableError(error)); }
    finally { state.uploading = false; updateUploadControls(); renderDocuments(); }
  }
  async function importFolder(event) {
    event.preventDefault();
    const path = $("folderPath").value.trim();
    if (!path) { $("folderPath").focus(); uploadFeedback("Enter a folder path first."); return; }
    if (state.uploading || busyJob()) return;
    state.uploading = true; updateUploadControls(); renderDocuments();
    uploadFeedback("Preparing the folder import…", true);
    try {
      const result = await api("/api/import-folder", { method: "POST", json: { path, metadata: metadata() } });
      applyJob(result);
    } catch (error) { uploadFeedback(readableError(error)); }
    finally { state.uploading = false; updateUploadControls(); renderDocuments(); }
  }
  function showSource(source) {
    state.source = source;
    $("sourceTitle").textContent = source.name || "Source excerpt";
    $("sourceLocation").textContent = source.location || "Indexed passage";
    $("sourceText").textContent = source.text || "No source text is available.";
    $("sourceMetadata").replaceChildren();
    for (const [key, value] of Object.entries(source.metadata || {})) {
      if (value === null || value === undefined || value === "") continue;
      $("sourceMetadata").append(element("dt", "", key.replace(/_/g, " ")), element("dd", "", typeof value === "object" ? JSON.stringify(value) : value));
    }
    $("filterSourceButton").disabled = source.document_id === null || source.document_id === undefined || !state.documents.some((doc) => String(doc.id) === String(source.document_id));
  }
  async function openSource(source) {
    const request = ++state.sourceRequest;
    showSource(source);
    if (!$("sourceDialog").open) $("sourceDialog").showModal();
    if (source.id === null || source.id === undefined) return;
    try {
      const result = await api(`/api/sources/${encodeURIComponent(source.id)}`);
      if (request === state.sourceRequest && $("sourceDialog").open) showSource(result.source || result);
    } catch (error) { if (!source.text && request === state.sourceRequest) $("sourceText").textContent = readableError(error); }
  }
  function requestDelete(doc) {
    state.deleting = doc;
    $("deleteDocumentName").textContent = doc.name;
    $("deleteFeedback").hidden = true;
    $("confirmDeleteButton").disabled = false;
    $("deleteDialog").showModal();
    $("cancelDeleteButton").focus();
  }
  async function deleteDocument() {
    if (!state.deleting) return;
    const doc = state.deleting;
    $("confirmDeleteButton").disabled = true;
    try {
      await api(`/api/documents/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
      $("deleteDialog").close(); state.deleting = null;
      toast("Document removed from the searchable library.");
      await refreshStatus(true);
    } catch (error) {
      $("deleteFeedback").textContent = readableError(error); $("deleteFeedback").hidden = false;
    } finally { $("confirmDeleteButton").disabled = false; }
  }

  $("documentSearch").addEventListener("input", renderDocuments);
  $("allDocumentsButton").addEventListener("click", () => setDocument(null));
  $("clearContextButton").addEventListener("click", () => setDocument(null));
  $("newChatButton").addEventListener("click", newConversation);
  $("chatForm").addEventListener("submit", submitQuestion);
  $("question").addEventListener("input", resizeQuestion);
  $("question").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); if (!$("sendButton").disabled) $("chatForm").requestSubmit(); } });
  for (const id of ["headerAddButton", "welcomeAddButton"]) $(id).addEventListener("click", openUpload);
  $("closeUploadButton").addEventListener("click", () => $("uploadDialog").close());
  $("chooseFilesButton").addEventListener("click", () => $("fileInput").click());
  $("chooseFolderButton").addEventListener("click", () => $("folderInput").click());
  $("fileInput").addEventListener("change", (event) => addFiles(event.target.files));
  $("folderInput").addEventListener("change", (event) => addFiles(event.target.files));
  $("clearFilesButton").addEventListener("click", () => { state.files = []; $("fileInput").value = ""; $("folderInput").value = ""; renderFiles(); });
  $("importFilesButton").addEventListener("click", importFiles);
  $("folderForm").addEventListener("submit", importFolder);
  const dropzone = $("uploadDropzone");
  for (const name of ["dragenter", "dragover"]) dropzone.addEventListener(name, (event) => { event.preventDefault(); if (!state.uploading) dropzone.classList.add("dragover"); });
  for (const name of ["dragleave", "drop"]) dropzone.addEventListener(name, (event) => { event.preventDefault(); dropzone.classList.remove("dragover"); });
  dropzone.addEventListener("drop", (event) => {
    const entries = Array.from(event.dataTransfer.items || []).map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
    if (entries.some((entry) => entry.isDirectory)) { uploadFeedback("For folders, use Choose folder or enter a local folder path below."); return; }
    addFiles(event.dataTransfer.files);
  });
  $("dismissIndexButton").addEventListener("click", () => { state.dismissedJob = String(state.status?.job?.id); renderJob(); });
  $("closeSourceButton").addEventListener("click", () => $("sourceDialog").close());
  $("filterSourceButton").addEventListener("click", () => { if (state.source) setDocument(state.source.document_id); $("sourceDialog").close(); $("question").focus(); });
  for (const id of ["closeDeleteButton", "cancelDeleteButton"]) $(id).addEventListener("click", () => $("deleteDialog").close());
  $("confirmDeleteButton").addEventListener("click", deleteDocument);
  $("modelSelect").addEventListener("change", async () => {
    const select = $("modelSelect");
    const model = select.value;
    select.disabled = true;
    try { await api("/api/settings", { method: "POST", json: { model } }); if (state.status) state.status.settings = { ...state.status.settings, model }; toast(`Local model set to ${model}.`); }
    catch (error) { toast(readableError(error)); }
    finally { renderModels(); }
  });
  renderDocuments();
  renderFiles();
  refreshStatus(true);
  setInterval(() => { if (!document.hidden || state.chatting || busyJob() || state.uploading) refreshStatus(); }, 5000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshStatus(true); });
})();
