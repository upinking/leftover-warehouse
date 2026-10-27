"use strict";

// AI 对话单独维护；库存变化仍使用 app.js 的统一 snapshot。
const assistantUI = {
  settings: null,
  messages: [],
  pendingPlan: null,
  busy: false,
  initialized: false,
  draft: "",
  error: "",
  requestId: null,
  requestText: "",
};

function renderAI() {
  paintAI();
  Promise.all([api("/api/ai/settings"), api("/api/ai/history")])
    .then(([settings, history]) => {
      assistantUI.settings = settings;
      Object.assign(assistantUI, history, { initialized: true });
      if (currentPage === "ai") paintAI();
    })
    .catch((error) => {
      assistantUI.error = error.message;
      if (currentPage === "ai") paintAI();
    });
}

function paintAI() {
  if (currentPage !== "ai") return;
  const u = assistantUI,
    s = u.settings || {},
    p = u.pendingPlan;
  main.innerHTML =
    heading("AI 助手") +
    `
    <div class="notice">提问时，会把本次问题、必要的元件资料、库存和相关流水发送到你设置的 AI 接口。账本保存在本机；修改先列明细，确认后才记账。</div>
    <details class="panel ai-settings" ${!s.configured ? "open" : ""}>
      <summary>AI 设置 · ${s.configured ? (s.hasKey || s.baseUrl?.startsWith("http://") ? "已配置" : "接口已设置，密钥未保存") : "待配置"}</summary>
      <form id="ai-settings-form"><div class="form-grid">
        <label class="field full">接口地址<input name="baseUrl" type="url" value="${esc(s.baseUrl)}" required placeholder="https://服务地址/v1" autocomplete="off"><small>支持兼容 OpenAI Chat Completions 的接口；本机模型可填 http://127.0.0.1:端口/v1。</small></label>
        <label class="field full">模型名称<input name="model" value="${esc(s.model)}" required placeholder="填写服务提供方的模型名" autocomplete="off"></label>
        <label class="field full">API 密钥<input name="apiKey" type="password" autocomplete="off" placeholder="${s.hasKey ? "已设置，留空保留" : "本机无鉴权模型可留空"}"><small>${s.keyStorage === "keychain" ? "密钥保存在此 Mac 的钥匙串中" : s.keyStorage === "session" ? "密钥仅在本次运行有效，退出后需重填" : "密钥不会进入库存备份"}。更换接口地址时，旧密钥会清除。</small></label>
        <label class="ai-check full"><input name="rememberKey" type="checkbox" ${s.keyStorage === "keychain" ? "checked" : ""}>记住新填写的密钥，保存在 Mac 钥匙串</label>
      </div><div class="ai-settings-actions"><button type="submit" class="primary">保存设置</button><button type="button" data-ai-action="test">测试连接</button><button type="button" data-ai-action="clear-key">清除本机密钥</button><span class="mini-note">测试连接前请先保存设置。</span></div></form>
    </details>
    <div class="error-box ai-error" id="ai-error" role="alert">${esc(u.error)}</div>
    <section class="panel ai-chat"><div class="panel-heading"><h2>对话</h2><span class="mini-note">可回复“确认执行”或“取消”</span></div>
      <div class="ai-messages" id="ai-messages" aria-live="polite">${u.messages.length ? u.messages.map((m) => `<article class="ai-message ${m.role === "user" ? "user" : "assistant"}"><div class="ai-message-meta">${m.role === "user" ? "你" : "AI 助手"} · ${esc(date(m.createdAt))}</div><div class="ai-message-body">${esc(m.content)}</div></article>`).join("") : `<div class="empty">${s.configured ? "输入需要查询或登记的内容。" : "先填写接口地址、模型名和密钥，再开始对话。"}</div>`}</div>
      ${p ? aiPlan(p) : ""}
      <form id="ai-chat-form" class="ai-composer"><label class="field">输入问题或库存操作<textarea id="ai-input" name="message" rows="3" maxlength="4000" required placeholder="例如：收到20颗C84270，放在A01">${esc(u.draft)}</textarea></label>
        <div class="ai-examples"><button type="button" data-ai-example="查一下 C84270 的库存，区分实物和预计余料">查库存</button><button type="button" data-ai-example="STM32 八路灯效练焊板还能配齐几块？缺什么？">查配料</button><button type="button" data-ai-example="收到20颗 C84270，放在A01，请先给我操作明细">登记收货</button></div>
        <div class="ai-send"><span class="mini-note">${u.busy ? "正在处理，请勿重复发送…" : "修改库存先试算，确认后才会保存。"}</span><button class="primary" type="submit">${u.busy ? "正在处理…" : "发送"}</button></div>
      </form></section>`;
  main.querySelectorAll("button").forEach((b) => (b.disabled = u.busy));
  const input = document.getElementById("ai-input");
  if (input) input.disabled = u.busy;
  const messages = document.getElementById("ai-messages");
  messages.scrollTop = messages.scrollHeight;
}

function aiOperation(item) {
  const a = item.payload,
    m = material(a.materialId || a.id),
    project = projects().find((p) => p.id === (a.projectId || a.id));
  const identity = m
    ? `${m.name} · ${m.supplierPartNumber || m.manufacturerPartNumber || "自有元件"} · ${m.package || "未填封装"}`
    : a.name || "";
  if (item.action === "stock.record")
    return `${kinds[a.type] || a.type}：${identity}，${a.quantity} 个${a.type === "count" ? "（盘点后的余额）" : ""}${a.locationCode ? `，库位 ${a.locationCode}` : ""}；原因：${a.reason}`;
  if (item.action === "purchase.add")
    return `采购：${identity}，${a.quantity} 个（不增加实物）；依据：${a.reason}`;
  if (item.action === "project.issue")
    return `整板领料：${project?.name || a.projectId}，${a.units} 块`;
  if (item.action === "project.save")
    return `项目：${a.name}，计划 ${a.plannedUnits} 块，BOM ${a.bom?.length || 0} 行`;
  const fields = {name: "名称", category: "类别", manufacturerPartNumber: "型号", supplier: "供应商",
    supplierPartNumber: "供应商编号", specification: "规格", package: "封装", locationCode: "库位", notes: "备注"};
  const details = Object.entries(fields).filter(([key]) => Object.hasOwn(a, key))
    .map(([key, label]) => `${label}：${a[key] || "（清空）"}`).join("；");
  return `${a.id ? "修改" : "新增"}元件 ${m?.name || a.name || ""}：${details}`;
}

function aiPlan(plan) {
  const p = plan.preview;
  const change = (a, b) => `${fmt(a)} → ${fmt(b)}`;
  return `<section class="ai-plan" aria-label="待确认操作单"><h3>待确认，尚未记账</h3><p>${esc(plan.summary)}</p>
    <ol>${p.operations.map((o) => `<li>${esc(aiOperation(o))}</li>`).join("")}</ol>
    ${p.changes.length ? `<div class="table-wrap"><table><thead><tr><th>元件 / 封装</th><th>采购记录</th><th>待领预留</th><th>预计自由</th><th>实物在手</th><th>库位</th></tr></thead><tbody>${p.changes.map((c) => `<tr><td>${esc(c.name)}<div class="mini-note">${esc(c.supplierPartNumber)} · ${esc(c.package)}</div></td><td>${change(c.before.ordered, c.after.ordered)}</td><td>${change(c.before.reserved, c.after.reserved)}</td><td>${change(c.before.forecastFree, c.after.forecastFree)}</td><td>${change(c.before.actualOnHand, c.after.actualOnHand)}</td><td>${esc(c.before.locationCode || "未设置")} → ${esc(c.after.locationCode || "未设置")}</td></tr>`).join("")}</tbody></table></div>` : ""}
    ${(p.projectChanges || []).map((c) => `<div class="ai-project-change"><strong>${esc(c.name)}</strong>：计划 ${c.before?.plannedUnits ?? 0} → ${c.after.plannedUnits} 块，已领 ${c.before?.issuedUnits ?? 0} → ${c.after.issuedUnits} 块<details><summary>确认每板 BOM</summary><ul>${c.after.bom.map((b) => `<li>${esc(material(b.materialId)?.name || b.materialId)} · ${b.quantityPerUnit} 个 · ${esc(b.designatorsText)}</li>`).join("")}</ul></details></div>`).join("")}
    <div class="ai-plan-actions"><button type="button" class="primary" data-ai-action="confirm">确认记账</button><button type="button" data-ai-action="cancel">取消</button></div></section>`;
}

async function aiRequest(path, payload) {
  if (assistantUI.busy) return;
  assistantUI.busy = true;
  assistantUI.error = "";
  main.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const note = main.querySelector(".ai-send .mini-note");
  if (note) note.textContent = "正在处理，请勿重复发送…";
  try {
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (result.messages)
      Object.assign(assistantUI, {
        messages: result.messages,
        pendingPlan: result.pendingPlan,
      });
    if (result.snapshot) snapshot = result.snapshot;
    if (path === "/api/ai/settings" || path === "/api/ai/clear-key")
      assistantUI.settings = result;
    if (path === "/api/ai/chat") {
      assistantUI.draft = "";
      assistantUI.requestId = null;
    }
    if (result.message) notify(result.message);
    else if (result.reply && path === "/api/ai/confirm") notify("已确认记账");
    assistantUI.busy = false;
    if (currentPage === "ai") paintAI();
    else if (result.snapshot) render();
  } catch (error) {
    assistantUI.error =
      error.message +
      (error.details ? "\n" + readableDetails(error.details) : "");
    assistantUI.busy = false;
    if (currentPage === "ai") {
      // 失败不重绘，保留用户尚未保存的设置和消息。
      document.getElementById("ai-error").textContent = assistantUI.error;
      main.querySelectorAll("button").forEach((b) => (b.disabled = false));
      if (note) note.textContent = "本次未完成，请检查提示；可重试同一请求。";
    }
  }
}

document.addEventListener("input", (event) => {
  if (event.target.id === "ai-input") assistantUI.draft = event.target.value;
});
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!["ai-settings-form", "ai-chat-form"].includes(form.id)) return;
  event.preventDefault();
  if (form.id === "ai-settings-form") {
    const values = Object.fromEntries(new FormData(form));
    if (!values.apiKey) delete values.apiKey;
    values.rememberKey = form.elements.rememberKey.checked;
    aiRequest("/api/ai/settings", values);
  } else {
    const text = form.elements.message.value.trim();
    assistantUI.draft = text;
    if (!text) return;
    if (assistantUI.requestText !== text || !assistantUI.requestId) {
      assistantUI.requestId = uuid();
      assistantUI.requestText = text;
    }
    aiRequest("/api/ai/chat", {
      message: text,
      requestId: assistantUI.requestId,
    });
  }
});
document.addEventListener("click", (event) => {
  const example = event.target.closest("[data-ai-example]");
  if (example) {
    assistantUI.draft = example.dataset.aiExample;
    const input = document.getElementById("ai-input");
    input.value = assistantUI.draft;
    input.focus();
    return;
  }
  const button = event.target.closest("[data-ai-action]");
  if (!button) return;
  const action = button.dataset.aiAction,
    plan = assistantUI.pendingPlan;
  if (action === "test") aiRequest("/api/ai/test", {});
  else if (action === "clear-key") aiRequest("/api/ai/clear-key", {});
  else if (action === "confirm" && plan)
    aiRequest("/api/ai/confirm", {
      planId: plan.id,
      requestId: "AI-plan:" + plan.id,
    });
  else if (action === "cancel" && plan)
    aiRequest("/api/ai/cancel", { planId: plan.id });
});
