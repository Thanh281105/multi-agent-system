"use strict";

(() => {
  const API_URL = "/api/v1/chat/stream";
  const STORAGE = {
    sessionId: "thuong-tri.session-id",
    history: "thuong-tri.history",
  };
  const MAX_HISTORY_ITEMS = 24;

  const elements = {
    conversation: document.querySelector("#conversation"),
    welcome: document.querySelector("#welcome-card"),
    form: document.querySelector("#composer-form"),
    input: document.querySelector("#composer-input"),
    send: document.querySelector("#send-button"),
    progressPanel: document.querySelector("#progress-panel"),
    progressList: document.querySelector("#progress-list"),
    cancel: document.querySelector("#cancel-button"),
    credentialButton: document.querySelector("#credential-button"),
    credentialDialog: document.querySelector("#credential-dialog"),
    credentialForm: document.querySelector("#credential-form"),
    credentialInput: document.querySelector("#api-key-input"),
    credentialCancel: document.querySelector("#credential-cancel"),
    newSession: document.querySelector("#new-session-button"),
    sessionLabel: document.querySelector("#session-label"),
    resultStatus: document.querySelector("#result-status"),
    executionList: document.querySelector("#execution-list"),
    executionsEmpty: document.querySelector("#executions-empty"),
    sourceList: document.querySelector("#source-list"),
    sourcesEmpty: document.querySelector("#sources-empty"),
    traceId: document.querySelector("#trace-id"),
    connection: document.querySelector("#connection-state"),
    connectionLabel: document.querySelector("#connection-label"),
    toast: document.querySelector("#toast"),
  };

  const state = {
    apiKey: "",
    sessionId: readStorage(STORAGE.sessionId),
    history: readHistory(),
    controller: null,
    pendingMessage: null,
    toastTimer: null,
    requestGeneration: 0,
  };

  class GatewayStreamError extends Error {
    constructor(code, message) {
      super(message);
      this.name = "GatewayStreamError";
      this.code = code;
    }
  }

  function readStorage(key) {
    try {
      return window.sessionStorage.getItem(key) || "";
    } catch (_error) {
      return "";
    }
  }

  function writeStorage(key, value) {
    try {
      if (value) {
        window.sessionStorage.setItem(key, value);
      } else {
        window.sessionStorage.removeItem(key);
      }
    } catch (_error) {
      showToast("Trình duyệt đang chặn session storage; phiên chỉ tồn tại trên màn hình.");
    }
  }

  function readHistory() {
    const raw = readStorage(STORAGE.history);
    if (!raw) return [];
    try {
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter(
          (item) =>
            item &&
            (item.role === "user" || item.role === "assistant") &&
            typeof item.text === "string",
        )
        .slice(-MAX_HISTORY_ITEMS);
    } catch (_error) {
      return [];
    }
  }

  function saveHistory() {
    writeStorage(
      STORAGE.history,
      JSON.stringify(state.history.slice(-MAX_HISTORY_ITEMS)),
    );
  }

  function recordHistory(role, text) {
    state.history.push({ role, text });
    state.history = state.history.slice(-MAX_HISTORY_ITEMS);
    saveHistory();
  }

  function appendMessage(role, text, options = {}) {
    const article = document.createElement("article");
    article.className = `message message-${role}`;
    if (options.error) article.classList.add("message-error");
    if (options.pending) article.classList.add("message-pending");

    const meta = document.createElement("div");
    meta.className = "message-meta";
    meta.textContent = role === "user" ? "Bạn" : "Orchestrator";

    const body = document.createElement("div");
    body.className = "message-body";
    body.textContent = text;

    if (options.pending) {
      const dots = document.createElement("span");
      dots.className = "typing-dots";
      dots.setAttribute("aria-label", "Đang xử lý");
      for (let index = 0; index < 3; index += 1) {
        dots.append(document.createElement("i"));
      }
      body.append(dots);
    }

    article.append(meta, body);
    elements.conversation.append(article);
    elements.welcome.hidden = true;
    scrollConversation();
    return article;
  }

  function updatePendingMessage(text, options = {}) {
    if (!state.pendingMessage) return;
    const body = state.pendingMessage.querySelector(".message-body");
    body.textContent = text;
    state.pendingMessage.classList.remove("message-pending");
    if (options.error) state.pendingMessage.classList.add("message-error");
    state.pendingMessage = null;
    scrollConversation();
  }

  function scrollConversation() {
    window.requestAnimationFrame(() => {
      elements.conversation.scrollTop = elements.conversation.scrollHeight;
    });
  }

  function setBusy(busy) {
    elements.input.disabled = busy;
    elements.send.disabled = busy;
    elements.progressPanel.hidden = !busy;
    elements.cancel.hidden = !busy;
    elements.connection.classList.toggle("offline", !navigator.onLine);
    if (!busy) {
      state.controller = null;
      elements.input.focus();
    }
  }

  function addProgress(event) {
    const item = document.createElement("li");
    const agent = event.agent_id ? ` · ${friendlyAgent(event.agent_id)}` : "";
    item.textContent = `${friendlyPhase(event.phase)}${agent}`;
    item.title = event.message || event.phase;
    elements.progressList.append(item);
    elements.progressList.scrollLeft = elements.progressList.scrollWidth;
  }

  function friendlyPhase(phase) {
    const labels = {
      "request.accepted": "Đã tiếp nhận",
      "routing.completed": "Đã định tuyến",
      "planning.completed": "Đã lập kế hoạch",
      "agent.started": "Agent bắt đầu",
      "agent.completed": "Agent hoàn tất",
      "aggregation.completed": "Đã tổng hợp",
    };
    return labels[phase] || phase;
  }

  function friendlyAgent(agentId) {
    const labels = {
      product_agent: "Product",
      review_agent: "Review",
      trust_agent: "Trust",
      market_agent: "Market",
      orchestrator: "Orchestrator",
    };
    return labels[agentId] || agentId;
  }

  function statusLabel(status) {
    const labels = {
      success: "Thành công",
      partial_success: "Một phần",
      failed: "Thất bại",
      running: "Đang chạy",
    };
    return labels[status] || status || "Chưa có";
  }

  function renderEvidence(result) {
    elements.resultStatus.textContent = statusLabel(result.status);
    elements.resultStatus.className = `status-badge ${result.status || ""}`;
    elements.traceId.textContent = result.trace_id || "—";

    elements.executionList.replaceChildren();
    const executions = Array.isArray(result.executions) ? result.executions : [];
    elements.executionsEmpty.hidden = executions.length > 0;
    for (const execution of executions) {
      const item = document.createElement("li");
      const title = document.createElement("div");
      title.className = "execution-title";
      const name = document.createElement("span");
      name.textContent = friendlyAgent(execution.agent_id);
      const status = document.createElement("span");
      status.textContent = statusLabel(execution.status);
      title.append(name, status);
      const detail = document.createElement("small");
      const duration = Number.isFinite(execution.duration_ms)
        ? `${Math.round(execution.duration_ms)} ms`
        : "—";
      detail.textContent = `${execution.action} · ${duration}`;
      item.append(title, detail);
      elements.executionList.append(item);
    }

    elements.sourceList.replaceChildren();
    const sources = Array.isArray(result.provenance) ? result.provenance : [];
    elements.sourcesEmpty.hidden = sources.length > 0;
    for (const source of sources) {
      const item = document.createElement("li");
      const title = document.createElement("div");
      title.className = "source-title";
      const name = document.createElement("span");
      name.textContent = source.source_type || "Nguồn dữ liệu";
      const sample = document.createElement("span");
      sample.textContent = source.sample_data ? "Mẫu" : "Thật";
      title.append(name, sample);
      const detail = document.createElement("small");
      const fields = Array.isArray(source.fields) ? source.fields.join(", ") : "";
      detail.textContent = `${source.source_id || "—"}${fields ? ` · ${fields}` : ""}`;
      item.append(title, detail);
      elements.sourceList.append(item);
    }
  }

  function updateSession(sessionId) {
    state.sessionId = sessionId || "";
    writeStorage(STORAGE.sessionId, state.sessionId);
    elements.sessionLabel.textContent = state.sessionId || "Phiên mới";
    elements.sessionLabel.title = state.sessionId;
  }

  function parseSSEBlock(block) {
    const event = { event: "message", data: null };
    const dataLines = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const separator = line.indexOf(":");
      const field = separator >= 0 ? line.slice(0, separator) : line;
      const rawValue = separator >= 0 ? line.slice(separator + 1) : "";
      const value = rawValue.startsWith(" ") ? rawValue.slice(1) : rawValue;
      if (field === "event") event.event = value;
      if (field === "id") event.id = value;
      if (field === "data") dataLines.push(value);
    }
    if (dataLines.length) {
      event.data = JSON.parse(dataLines.join("\n"));
    }
    return event;
  }

  async function readGatewayStream(response) {
    if (!response.body) {
      throw new GatewayStreamError(
        "gateway.stream_unavailable",
        "Trình duyệt không hỗ trợ stream phản hồi.",
      );
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let terminalResult = null;

    try {
      while (!terminalResult) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        buffer = buffer.replaceAll("\r\n", "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const block = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (block && !block.startsWith(":")) {
            const event = parseSSEBlock(block);
            if (event.event === "status" && event.data) addProgress(event.data);
            if (event.event === "completed" && event.data) {
              terminalResult = event.data;
              break;
            }
            if (event.event === "error" && event.data) {
              const detail = event.data.error || {};
              throw new GatewayStreamError(
                detail.code || "gateway.stream_failed",
                detail.message || "Không thể xử lý yêu cầu lúc này.",
              );
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
        if (done) break;
      }
    } finally {
      try {
        await reader.cancel();
      } catch (_error) {
        // The stream may already be closed by the server or AbortController.
      }
      reader.releaseLock();
    }

    if (!terminalResult) {
      throw new GatewayStreamError(
        "gateway.incomplete_stream",
        "Kết nối kết thúc trước khi có kết quả hoàn chỉnh.",
      );
    }
    return terminalResult;
  }

  async function parseHTTPError(response) {
    try {
      const payload = await response.json();
      const detail = payload.error || {};
      return new GatewayStreamError(
        detail.code || `gateway.http_${response.status}`,
        detail.message || "Không thể xử lý yêu cầu lúc này.",
      );
    } catch (_error) {
      return new GatewayStreamError(
        `gateway.http_${response.status}`,
        "Không thể xử lý yêu cầu lúc này.",
      );
    }
  }

  async function sendMessage(message, controller) {
    const response = await fetch(API_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": state.apiKey,
      },
      body: JSON.stringify({
        message,
        ...(state.sessionId ? { session_id: state.sessionId } : {}),
      }),
      signal: controller.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw await parseHTTPError(response);
    return readGatewayStream(response);
  }

  async function submitMessage(message) {
    const cleaned = message.trim();
    if (!cleaned || state.controller) return;
    if (!state.apiKey) {
      openCredentialDialog();
      showToast("Cần API key để kết nối Gateway.");
      return;
    }

    appendMessage("user", cleaned);
    recordHistory("user", cleaned);
    state.pendingMessage = appendMessage(
      "assistant",
      "Đang điều phối các chuyên gia miền",
      { pending: true },
    );
    elements.progressList.replaceChildren();
    elements.input.value = "";
    resizeComposer();
    const controller = new AbortController();
    const generation = state.requestGeneration + 1;
    state.requestGeneration = generation;
    state.controller = controller;
    setBusy(true);

    try {
      const result = await sendMessage(cleaned, controller);
      if (generation !== state.requestGeneration) return;
      updatePendingMessage(result.answer || "Không có nội dung trả lời.");
      recordHistory("assistant", result.answer || "Không có nội dung trả lời.");
      updateSession(result.session_id);
      renderEvidence(result);
      if (Array.isArray(result.warnings) && result.warnings.length) {
        showToast(`Cảnh báo: ${result.warnings[0]}`);
      }
    } catch (error) {
      if (generation !== state.requestGeneration) return;
      const aborted = error && error.name === "AbortError";
      const messageText = aborted
        ? "Đã dừng yêu cầu theo thao tác của bạn."
        : error.message || "Không thể xử lý yêu cầu lúc này.";
      updatePendingMessage(messageText, { error: !aborted });
      if (error.code === "gateway.authentication_failed") {
        state.apiKey = "";
        openCredentialDialog();
      }
      if (error.code === "gateway.session_not_found") {
        updateSession("");
        showToast("Phiên cũ đã hết hạn. Hãy gửi lại để bắt đầu phiên mới.");
      }
    } finally {
      if (generation === state.requestGeneration) setBusy(false);
    }
  }

  function resetSession() {
    state.requestGeneration += 1;
    if (state.controller) state.controller.abort();
    state.controller = null;
    state.pendingMessage = null;
    updateSession("");
    state.history = [];
    saveHistory();
    elements.conversation.replaceChildren(elements.welcome);
    elements.welcome.hidden = false;
    elements.executionList.replaceChildren();
    elements.sourceList.replaceChildren();
    elements.executionsEmpty.hidden = false;
    elements.sourcesEmpty.hidden = false;
    elements.resultStatus.textContent = "Chưa có";
    elements.resultStatus.className = "status-badge";
    elements.traceId.textContent = "—";
    elements.progressPanel.hidden = true;
    setBusy(false);
    showToast("Đã tạo phiên mới trên thiết bị này.");
  }

  function openCredentialDialog() {
    elements.credentialInput.value = "";
    if (!elements.credentialDialog.open) elements.credentialDialog.showModal();
    window.setTimeout(() => elements.credentialInput.focus(), 0);
  }

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.hidden = false;
    if (state.toastTimer) window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(() => {
      elements.toast.hidden = true;
    }, 4200);
  }

  function resizeComposer() {
    elements.input.style.height = "auto";
    elements.input.style.height = `${Math.min(elements.input.scrollHeight, 160)}px`;
  }

  function restoreInterface() {
    updateSession(state.sessionId);
    if (state.history.length) {
      for (const item of state.history) appendMessage(item.role, item.text);
    }
    if (state.apiKey) {
      elements.credentialButton.textContent = "Đã cấu hình key";
    } else {
      window.setTimeout(openCredentialDialog, 350);
    }
    updateConnectionState();
  }

  function updateConnectionState() {
    const online = navigator.onLine;
    elements.connection.classList.toggle("offline", !online);
    elements.connectionLabel.textContent = online ? "Sẵn sàng" : "Ngoại tuyến";
  }

  elements.form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitMessage(elements.input.value);
  });

  elements.input.addEventListener("input", resizeComposer);
  elements.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      elements.form.requestSubmit();
    }
  });

  document.querySelectorAll("[data-prompt]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.input.value = button.dataset.prompt || "";
      resizeComposer();
      elements.input.focus();
    });
  });

  elements.cancel.addEventListener("click", () => {
    if (state.controller) state.controller.abort();
  });
  elements.newSession.addEventListener("click", resetSession);
  elements.credentialButton.addEventListener("click", openCredentialDialog);
  elements.credentialCancel.addEventListener("click", () => {
    elements.credentialDialog.close();
  });
  elements.credentialForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const apiKey = elements.credentialInput.value.trim();
    if (apiKey.length < 8) {
      elements.credentialInput.setCustomValidity("API key cần ít nhất 8 ký tự.");
      elements.credentialInput.reportValidity();
      return;
    }
    elements.credentialInput.setCustomValidity("");
    state.apiKey = apiKey;
    elements.credentialButton.textContent = "Đã cấu hình key";
    elements.credentialDialog.close();
    showToast("Đã dùng API key cho phiên trang hiện tại.");
    elements.input.focus();
  });

  window.addEventListener("online", updateConnectionState);
  window.addEventListener("offline", updateConnectionState);
  restoreInterface();
})();
