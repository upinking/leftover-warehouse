"use strict";

// 页面只负责输入和展示；每次保存都交给本机服务，由数据库统一检查数量。
let snapshot = null;
let currentPage = "materials";
let filters = { q: "", category: "", package: "", status: "" };
let historyQuery = "";
let pendingBackup = null;
let lastSafetyBackupPath = "";
try {
  lastSafetyBackupPath =
    sessionStorage.getItem("warehouse-safety-backup") || "";
} catch {}
let toastTimer;
const main = document.getElementById("main");
const dialog = document.getElementById("editor");
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const int = (value) => Number.parseInt(value, 10);
const names = {
  ai: "AI 助手",
  materials: "物料仓库",
  projects: "项目配料",
  history: "库存流水",
  backup: "备份与恢复",
};
const kinds = {
  receipt: "收货",
  issue: "零散领用",
  return: "归还",
  damage: "焊坏",
  loss: "损耗",
  count: "盘点修正",
  project_issue: "整板领料",
  "project.issue": "整板领料",
};
const labels = {
  "material.save": "保存物料",
  "purchase.add": "采购登记",
  "project.save": "项目与 BOM",
  "seed.import": "初始化导入",
  "backup.restore": "备份恢复",
  "stock.record": "库存登记",
  "project.issue": "整板领料",
};
const materials = () => snapshot?.data.materials ?? [];
const projects = () => snapshot?.data.projects ?? [];
const material = (id) => materials().find((m) => m.id === id);
const stock = (id) => snapshot.stocks.find((s) => s.materialId === id) ?? {};
const fmt = (value) =>
  value === null || value === undefined
    ? "待点数"
    : Number(value).toLocaleString("zh-CN");
const date = (value) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "原始时间未知";
const uuid = () =>
  crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error("本地服务返回异常，请查看服务日志。");
  }
  if (!response.ok) {
    const error = new Error(body.error || "保存失败");
    error.details = body.details;
    throw error;
  }
  return body;
}
function notify(message) {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 4500);
}
function setStatus(message, bad = false) {
  const el = document.getElementById("save-status");
  el.innerHTML = `<span class="status-dot"></span>${esc(message)}`;
  el.classList.toggle("error", bad);
}
async function load() {
  try {
    snapshot = await api("/api/state");
    render();
    setStatus("已读取本地数据库");
  } catch (error) {
    main.innerHTML = `<div class="error-box">${esc(error.message)}\n请重新打开余料仓库，或查看数据目录中的 server.log。</div><button data-action="reload">重新连接</button>`;
    setStatus("连接中断", true);
  }
}
function heading(title, actions = "") {
  return `<div class="page-heading"><h1>${title}</h1><div class="heading-actions">${actions}</div></div>`;
}
function statCards() {
  const s = snapshot.summary;
  return `<div class="stats">
    <div class="stat"><div class="stat-label">采购记录</div><div class="stat-number">${fmt(s.orderedPieces)}<small>个</small></div><div class="stat-note">${s.materialKinds} 种元件 · 不代表已到货</div></div>
    <div class="stat"><div class="stat-label">待领预留</div><div class="stat-number">${fmt(s.reservedPieces)}<small>个</small></div><div class="stat-note">已承诺给 ${projects().filter((p) => p.plannedUnits > p.issuedUnits).length} 个项目 · 尚未扣实物</div></div>
    <div class="stat featured"><div class="stat-label">预计自由余料</div><div class="stat-number">${fmt(s.forecastFreePieces)}<small>个</small></div><div class="stat-note">按采购记录减去消耗与待领预留</div></div>
    <div class="stat ${s.actualOnHandPieces === null ? "pending" : ""}"><div class="stat-label">实物在手</div><div class="stat-number">${fmt(s.actualOnHandPieces)}${s.actualOnHandPieces === null ? "" : "<small>个</small>"}</div><div class="stat-note">${s.notCountedKinds ? `${s.notCountedKinds} 种元件尚未点数` : "全部元件已确认实物余额"}</div></div>
  </div>`;
}
function render() {
  document.querySelectorAll("[data-page]").forEach((b) => {
    b.classList.toggle("active", b.dataset.page === currentPage);
    b.setAttribute(
      "aria-current",
      b.dataset.page === currentPage ? "page" : "false",
    );
  });
  document.getElementById("page-crumb").textContent = names[currentPage];
  if (currentPage === "ai") renderAI();
  else if (currentPage === "materials") renderMaterials();
  else if (currentPage === "projects") renderProjects();
  else if (currentPage === "history") renderHistory();
  else renderBackup();
}
function renderMaterials() {
  const cats = [
    ...new Set(
      materials()
        .map((m) => m.category)
        .filter(Boolean),
    ),
  ].sort();
  const packs = [
    ...new Set(
      materials()
        .map((m) => m.package)
        .filter(Boolean),
    ),
  ].sort();
  main.innerHTML =
    heading(
      "物料仓库",
      '<button data-action="movement">↗ 登记库存</button><button class="primary" data-action="new-material">＋ 新增物料</button>',
    ) +
    statCards() +
    `<div class="notice"><span class="symbol">ⓘ</span><span>采购记录用于预计配料，实物以到货点数为准。预留只占计划，整板领料才扣库存。<br>本批记录含待核实订单数量；未点数与确认 0 个会分别显示。</span></div>
    <section class="panel"><div class="panel-heading"><h2>全部元件<small>${materials().length} 种物料</small></h2><button class="refresh" data-action="reload" aria-label="重新读取数据库">↻ 刷新</button></div>
      <div class="filters"><div class="search"><span>⌕</span><input id="search" aria-label="搜索元件" placeholder="搜索名称、型号、立创 C 编号、封装或库位…" value="${esc(filters.q)}"></div>
      <select id="category-filter" aria-label="类别筛选"><option value="">全部类别</option>${cats.map((x) => `<option ${filters.category === x ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
      <select id="package-filter" aria-label="封装筛选"><option value="">全部封装</option>${packs.map((x) => `<option ${filters.package === x ? "selected" : ""}>${esc(x)}</option>`).join("")}</select>
      <select id="status-filter" aria-label="库存状态筛选"><option value="">全部状态</option><option value="unknown" ${filters.status === "unknown" ? "selected" : ""}>待点数</option><option value="shortage" ${filters.status === "shortage" ? "selected" : ""}>计划缺料</option><option value="counted" ${filters.status === "counted" ? "selected" : ""}>已点数</option></select></div>
      <div class="table-wrap"><table><thead><tr><th>元件 / 型号</th><th>C 编号</th><th>封装</th><th class="num">采购</th><th class="num">待领预留</th><th class="num">预计自由</th><th class="num">实物在手</th><th>库位</th><th></th></tr></thead><tbody id="material-rows"></tbody></table></div>
      <div class="table-bottom"><span id="row-count"></span><span>数量单位：个完整元件 · 点击名称查看明细</span></div></section>`;
  renderRows();
}
function renderRows() {
  const query = filters.q.trim().toLocaleLowerCase();
  const shown = materials().filter((m) => {
    const s = stock(m.id);
    return (
      (!query ||
        [
          m.name,
          m.manufacturerPartNumber,
          m.supplierPartNumber,
          m.package,
          m.specification,
          s.locationCode,
          m.category,
        ]
          .join(" ")
          .toLocaleLowerCase()
          .includes(query)) &&
      (!filters.category || m.category === filters.category) &&
      (!filters.package || m.package === filters.package) &&
      (!filters.status ||
        (filters.status === "unknown" && s.actualOnHand == null) ||
        (filters.status === "counted" && s.actualOnHand != null) ||
        (filters.status === "shortage" &&
          (s.forecastFree < 0 || (s.actualFree != null && s.actualFree < 0))))
    );
  });
  document.getElementById("material-rows").innerHTML = shown.length
    ? shown
        .map((m) => {
          const s = stock(m.id);
          return `<tr><td><div class="material-name"><button data-action="detail" data-id="${esc(m.id)}">${esc(m.name)}</button></div><div class="material-meta" title="${esc(m.manufacturerPartNumber)}">${esc(m.manufacturerPartNumber || m.specification || "自有元件")}</div></td><td class="mono">${esc(m.supplierPartNumber || "—")}</td><td><span class="pill">${esc(m.package || "未填写")}</span></td><td class="num">${fmt(s.ordered)}</td><td class="num muted">${fmt(s.reserved)}</td><td class="num ${s.forecastFree < 0 ? "negative" : "positive"}">${fmt(s.forecastFree)}${s.forecastFree < 0 ? `<div class="mini-note negative">缺 ${Math.abs(s.forecastFree)} 个</div>` : ""}</td><td class="num">${s.actualOnHand == null ? '<span class="pill pending">待点数</span>' : `<span>${fmt(s.actualOnHand)}</span>${s.actualFree < 0 ? `<div class="mini-note negative">预留缺 ${Math.abs(s.actualFree)} 个</div>` : ""}`}</td><td><span class="location">${esc(s.locationCode || "未设置")}</span></td><td><button class="small-button" data-action="movement" data-id="${esc(m.id)}">登记</button></td></tr>`;
        })
        .join("")
    : '<tr><td colspan="9" class="empty">没有找到符合条件的元件。<br>试试型号、C 编号，或清除筛选条件。</td></tr>';
  document.getElementById("row-count").textContent =
    `显示 ${shown.length} / ${materials().length} 种元件`;
}
function renderProjects() {
  main.innerHTML =
    heading(
      "项目配料",
      '<button class="primary" data-action="new-project">＋ 新建项目</button>',
    ) +
    `<div class="notice"><span class="symbol">ⓘ</span><span>配齐总上限先满足其他项目的待领预留，包含当前项目已领和已计划数量，不能直接当成“还可追加几块”。实物待点数时，只提供预计结果。</span></div><div class="project-grid">${
      projects()
        .map((p) => {
          const c = snapshot.capacities.find((c) => c.projectId === p.id) ?? {};
          const per = p.bom.reduce((sum, b) => sum + b.quantityPerUnit, 0);
          const bottleneck = (c.bottleneckMaterialIds || [])
            .map(
              (id) =>
                material(id)?.supplierPartNumber || material(id)?.name || id,
            )
            .join("、");
          const short = p.bom.filter(
            (b) => stock(b.materialId).forecastFree < 0,
          );
          return `<article class="project-card"><div class="project-heading"><h2>${esc(p.name)}</h2><span class="pill ${p.issuedUnits >= p.plannedUnits ? "ok" : ""}">${p.plannedUnits === 0 ? "未计划" : p.issuedUnits >= p.plannedUnits ? "计划已领齐" : "待领料"}</span></div><div class="project-meta">${p.bom.length} 种元件 · 每块 ${per} 个 · BOM v${p.bomVersion}</div><div class="plan"><div><strong>${p.plannedUnits}</strong>块计划</div><div><strong>${p.issuedUnits}</strong>块已领</div><div><strong>${p.plannedUnits - p.issuedUnits}</strong>块待领</div></div><div class="capacity"><div>预计配齐总上限<strong>${c.forecastWholeUnits == null ? "—" : fmt(c.forecastWholeUnits) + " 块"}</strong></div><div class="actual">实物配齐总上限<strong>${c.actualWholeUnits == null ? "待点数" : fmt(c.actualWholeUnits) + " 块"}</strong></div></div><p class="project-hint">优先保留其他项目的待领用料。${bottleneck ? `预计瓶颈：${esc(bottleneck)}。` : ""}</p>${short.length ? `<div class="notice error">${short.map((b) => `${esc(material(b.materialId)?.name)}：全部计划合计缺 ${Math.abs(stock(b.materialId).forecastFree)} 个`).join("<br>")}</div>` : ""}<div class="card-actions"><button class="primary" data-action="issue-project" data-id="${esc(p.id)}" ${p.plannedUnits <= p.issuedUnits ? "disabled" : ""}>整板领料</button><button data-action="edit-project" data-id="${esc(p.id)}">编辑计划 / BOM</button></div><details class="project-bom"><summary>查看每板用料及预计可用数</summary><div class="table-wrap"><table><thead><tr><th>元件</th><th>位号</th><th class="num">每板</th><th class="num">扣其他预留后</th></tr></thead><tbody>${p.bom
            .map((b) => {
              const limit = c.limits?.find(
                (l) => l.materialId === b.materialId,
              );
              return `<tr><td>${esc(material(b.materialId)?.name || b.materialId)}<br><span class="mono muted">${esc(material(b.materialId)?.supplierPartNumber)}</span></td><td>${esc(b.designatorsText)}</td><td class="num">${b.quantityPerUnit}</td><td class="num">${fmt(limit?.availableForecast)}</td></tr>`;
            })
            .join("")}</tbody></table></div></details></article>`;
        })
        .join("") || '<div class="empty">还没有项目，先建立一份 BOM 吧。</div>'
    }</div><div class="below-note">“预留”是一张待办清单，不会扣掉在手元件。整板领料会在同一次保存中扣实物、增加已领套数，并释放相应预留；任何一项不足，整笔都不会扣。<br>需要取消未领计划时，将计划数改到已领数即可。已领数量保留历史记录。</div>`;
}
function movementDescription(m) {
  if (m.lines?.length)
    return m.lines
      .map(
        (l) =>
          `${material(l.materialId)?.name || l.materialId} × ${l.quantity ?? Math.abs(l.delta ?? 0)}`,
      )
      .join("；");
  return material(m.materialId)?.name || m.detail || m.reason || "";
}
function renderHistory() {
  main.innerHTML =
    heading(
      "库存流水",
      '<button class="primary" data-action="movement">＋ 登记库存</button>',
    ) +
    `<section class="panel"><div class="panel-heading"><h2>实物变动<small>${snapshot.data.stockMovements.length} 笔记录</small></h2></div><div class="history-tools"><input id="history-search" aria-label="搜索库存流水" placeholder="搜索元件、类型、项目或原因…" value="${esc(historyQuery)}"></div><div class="table-wrap"><table><thead><tr><th>时间 / 操作者</th><th>类型</th><th>元件 / 项目</th><th class="num">实物变化</th><th>原因</th></tr></thead><tbody id="history-rows"></tbody></table></div></section><section class="panel panel-gap"><div class="panel-heading"><h2>资料与计划操作</h2><small class="muted">保留本地操作轨迹</small></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>操作</th><th>说明</th></tr></thead><tbody>${
      [...(snapshot.data.auditLog || [])]
        .reverse()
        .slice(0, 200)
        .map(
          (e) =>
            `<tr><td class="mono">${esc(date(e.at || e.occurredAt || e.createdAt))}</td><td>${esc(e.actor || "本地用户")}</td><td>${esc(labels[e.action] || e.action || e.type || "资料更新")}</td><td class="record-detail">${esc(typeof e.detail === "string" ? e.detail : e.detail?.summary || e.message || "")}</td></tr>`,
        )
        .join("") ||
      '<tr><td colspan="4" class="empty">还没有资料修改记录。原始采购时间未知，保留为空。</td></tr>'
    }</tbody></table></div></section>`;
  renderHistoryRows();
}
function renderHistoryRows() {
  const query = historyQuery.toLowerCase();
  const records = [...snapshot.data.stockMovements]
    .reverse()
    .filter((m) =>
      `${movementDescription(m)} ${kinds[m.type] || m.type} ${m.reason || ""} ${projects().find((p) => p.id === m.projectId)?.name || ""} ${material(m.materialId)?.supplierPartNumber || ""}`
        .toLowerCase()
        .includes(query),
    );
  document.getElementById("history-rows").innerHTML =
    records
      .map((m) => {
        const delta =
          m.delta ??
          (m.lines
            ? m.lines.reduce((n, l) => n + (l.delta ?? -l.quantity), 0)
            : null);
        return `<tr><td class="mono">${esc(date(m.occurredAt || m.at || m.createdAt))}<div class="mini-note">${esc(m.actor || "本地用户")}</div></td><td class="record-kind"><span class="pill">${esc(kinds[m.type] || labels[m.type] || m.type)}</span></td><td class="record-detail">${m.projectId ? `<strong>${esc(projects().find((p) => p.id === m.projectId)?.name || m.projectId)} · ${m.units ?? ""} 块</strong><br>` : ""}${esc(movementDescription(m))}${m.bomVersion ? `<div class="mini-note">按当时 BOM v${m.bomVersion} 保存</div>` : ""}</td><td class="num quantity-change ${delta < 0 ? "negative" : "positive"}">${m.type === "count" && m.balanceBefore == null ? "首次确认" : delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta}`}${m.type === "count" ? `<div class="mini-note">盘点后 ${fmt(m.balanceAfter)} 个</div>` : ""}</td><td class="record-detail muted">${esc(m.reason || "—")}</td></tr>`;
      })
      .join("") ||
    '<tr><td colspan="5" class="empty">没有实物流水。<br>采购与计划预留不会生成实物出库记录。</td></tr>';
}
function renderBackup() {
  main.innerHTML =
    heading("备份与恢复") +
    `<div class="backup-grid"><section class="backup-card"><div class="large-icon">⇩</div><h2>导出完整备份</h2><p>一份 JSON 文件包含物料、采购、项目 BOM、库存流水和操作记录。换电脑或恢复数据时，用这份文件即可。</p><a class="button primary download-button" href="/api/backup" download>下载备份 JSON</a><div class="below-note">当前 ${materials().length} 种元件 · ${projects().length} 个项目<br>实物流水 ${snapshot.data.stockMovements.length} 笔 · 数据版本 ${snapshot.data.revision ?? 0}</div></section><section class="backup-card"><div class="large-icon">↺</div><h2>从备份恢复</h2><p>选择本软件导出的 JSON，先校验并预览。恢复会<strong>替换当前仓库</strong>，不会把数量再加一遍。操作前请先下载现有备份。</p><div class="upload-area"><label for="backup-file">选择备份文件</label><input type="file" id="backup-file" accept=".json,application/json"></div><div id="backup-error" class="error-box error-gap"></div><div id="backup-preview"></div></section></div><div class="notice"><span class="symbol">ⓘ</span><span>数据自动保存在本机。退出应用不会丢失数据；重新双击余料仓库.app 即可继续使用。完整备份请保存到你信任的位置。</span></div><details class="backup-details"><summary>关于初始化与 BOM 导入</summary><p>交接包的 seed.json 已在首次启动时导入，Excel、CSV 是相同数据的查阅副本，不应重复导入。软件会拦截重复初始化。<br>单个项目的 BOM 可在“项目配料 → 编辑计划 / BOM”中手工编辑，或粘贴简单 JSON 导入。</p><label class="field seed-field">仅用于检查初始化数据<input type="file" id="seed-file" accept=".json,application/json"></label></details>`;
  if (lastSafetyBackupPath) {
    main.insertAdjacentHTML(
      "beforeend",
      `<div class="notice"><span class="symbol">↺</span><label class="field safety-path">最近恢复前的安全快照（可以复制此路径）<input readonly aria-label="恢复前安全快照路径" value="${esc(lastSafetyBackupPath)}"></label></div>`,
    );
  }
  pendingBackup = null;
}
function openDialog(title, subtitle, body, wide = false) {
  dialog.classList.toggle("wide", wide);
  document.getElementById("dialog-content").innerHTML =
    `<div class="dialog-header"><div><h2 id="dialog-title">${esc(title)}</h2><p>${subtitle}</p></div><button class="close-dialog" data-action="close-dialog" aria-label="关闭对话框">×</button></div><div class="dialog-body">${body}</div>`;
  if (!dialog.open) dialog.showModal();
}
const options = (id) =>
  materials()
    .map(
      (m) =>
        `<option value="${esc(m.id)}" ${id === m.id ? "selected" : ""}>${esc(m.name)} · ${esc(m.supplierPartNumber || m.manufacturerPartNumber || m.package || "自有元件")}</option>`,
    )
    .join("");
const field = (label, name, value = "", extra = "", full = false) =>
  `<label class="field ${full ? "full" : ""}">${label}<input name="${name}" value="${esc(value)}" ${extra}></label>`;
const actions = (text) =>
  `<div class="form-actions"><button type="button" data-action="close-dialog">取消</button><button type="submit" class="primary">${text}</button></div>`;
function editMaterial(id) {
  const m = material(id) || {},
    s = id ? stock(id) : {};
  openDialog(
    id ? "编辑元件资料" : "新增元件",
    "型号、封装和 C 编号按文字保存。没有商城编号的自有元件也可以登记。",
    `<form id="material-form" data-id="${esc(id || "")}"><div class="error-box form-error"></div><div class="form-grid">${field("元件名称 *", "name", m.name, 'required maxlength="200"', true)}${field("类别", "category", m.category)}${field("封装", "package", m.package, 'placeholder="例如 0603"')}${field("厂家型号", "manufacturerPartNumber", m.manufacturerPartNumber)}${field("规格", "specification", m.specification)}${field("供应商", "supplier", m.supplier)}${field("供应商编号 / 立创 C 编号", "supplierPartNumber", m.supplierPartNumber)}${field("盒子 / 格子", "locationCode", s.locationCode, 'placeholder="例如 A盒 / 03格"', true)}<label class="field full">备注<textarea name="notes" rows="2">${esc(m.notes)}</textarea></label></div>${actions("保存元件")}</form>`,
  );
}
function editMovement(id, type = "receipt") {
  openDialog(
    "登记库存",
    "收货才增加实物，采购记录单独登记。所有操作都会留下时间、原因和操作者。",
    `<form id="movement-form"><div class="error-box form-error"></div><div class="form-grid"><label class="field full">元件 *<select name="materialId" required>${options(id)}</select></label><label class="field">操作类型<select name="type">${Object.entries(
      kinds,
    )
      .filter(([k]) => !k.includes("project"))
      .map(
        ([k, v]) =>
          `<option value="${k}" ${type === k ? "selected" : ""}>${v}</option>`,
      )
      .join(
        "",
      )}</select></label>${field("数量（个完整元件） *", "quantity", "", 'type="number" min="0" step="1" required inputmode="numeric"')}${field("盒子 / 格子（可选）", "locationCode", id ? stock(id).locationCode : "", 'placeholder="留空保持当前库位"', true)}<label class="field full">原因 / 说明 *<textarea name="reason" required rows="2" placeholder="例如：收到第一批包裹，逐颗点数"></textarea></label></div><div class="notice warning" id="movement-tip">收货数量填写本次实际收到的数量；第一次登记收货会结束该元件的待点数状态。填 0 表示确认本次为 0。</div>${actions("保存登记")}</form>`,
  );
}
function editPurchase(id) {
  openDialog(
    "登记采购",
    "只改变采购记录与预计配料，实物在手数不会增加。",
    `<form id="purchase-form"><div class="error-box form-error"></div><div class="form-grid"><label class="field full">元件 *<select name="materialId" required>${options(id)}</select></label>${field("本次采购数量 *", "quantity", "", 'type="number" min="1" step="1" required')}${field("订单号（可留空）", "externalOrderId")}<label class="field full">依据 / 说明 *<textarea name="reason" required rows="2" placeholder="例如：核对订单后补记"></textarea></label></div>${actions("保存采购记录")}</form>`,
  );
}
function showDetail(id) {
  const m = material(id),
    s = stock(id);
  if (!m) return;
  const allocations = projects()
    .filter((p) => p.bom.some((b) => b.materialId === id))
    .map((p) => {
      const b = p.bom.find((b) => b.materialId === id);
      return `${p.name}：${b.quantityPerUnit} × ${p.plannedUnits - p.issuedUnits} = ${b.quantityPerUnit * (p.plannedUnits - p.issuedUnits)} 个待领`;
    });
  const evidence = snapshot.data.purchaseBatches.flatMap((p) =>
    p.lines
      .filter((l) => l.materialId === id)
      .map(
        (l) =>
          `${l.orderedQuantity} 个：${l.quantityEvidence || "本地采购登记"}${p.externalOrderId ? `（订单 ${p.externalOrderId}）` : ""}`,
      ),
  );
  const history = snapshot.data.stockMovements.filter(
    (x) => x.materialId === id || x.lines?.some((l) => l.materialId === id),
  );
  openDialog(
    m.name,
    `${esc(m.manufacturerPartNumber || "自有元件")} · ${esc(m.supplierPartNumber || "无商城编号")}`,
    `<div class="detail-stats"><div>采购记录<strong>${fmt(s.ordered)}</strong></div><div>待领预留<strong>${fmt(s.reserved)}</strong></div><div>预计自由<strong class="${s.forecastFree < 0 ? "negative" : ""}">${fmt(s.forecastFree)}</strong></div><div>实物在手<strong>${fmt(s.actualOnHand)}</strong></div><div>实物自由<strong class="${s.actualFree < 0 ? "negative" : ""}">${fmt(s.actualFree)}</strong></div><div>已收货累计<strong>${fmt(s.received)}</strong></div></div><dl class="detail-list"><dt>类别</dt><dd>${esc(m.category || "—")}</dd><dt>封装</dt><dd>${esc(m.package || "—")}</dd><dt>规格</dt><dd>${esc(m.specification || "—")}</dd><dt>盒子 / 格子</dt><dd>${esc(s.locationCode || "未设置")}</dd><dt>备注</dt><dd>${esc(m.notes || "—")}</dd></dl><div class="notice">${allocations.length ? allocations.map(esc).join("<br>") : "此元件暂无项目待领预留。"}</div><div class="detail-actions"><button class="primary" data-action="movement" data-id="${esc(id)}">登记实物</button><button data-action="edit-material" data-id="${esc(id)}">编辑资料 / 库位</button><button data-action="purchase" data-id="${esc(id)}">登记采购</button></div><div class="detail-history"><h3>采购来源与核实情况</h3><p>${evidence.map(esc).join("<br>") || "尚无采购记录"}</p><h3>最近实物流水</h3>${
      history
        .slice(-6)
        .reverse()
        .map(
          (x) =>
            `<p>${esc(date(x.occurredAt || x.at || x.createdAt))} · ${esc(kinds[x.type] || x.type)}<br>${esc(x.reason || "")}</p>`,
        )
        .join("") || "<p>尚无实物流水。初始采购不代表已经收货。</p>"
    }</div>`,
  );
}
function bomRow(b = {}) {
  return `<tr><td><select name="bom-material" aria-label="BOM 元件" required>${options(b.materialId)}</select></td><td><input name="bom-quantity" aria-label="每板用量" type="number" min="1" step="1" required value="${esc(b.quantityPerUnit || 1)}"></td><td><input name="bom-designators" aria-label="位号" value="${esc(b.designatorsText)}" placeholder="R1、R2"></td><td><button type="button" class="text-button" data-action="remove-bom" aria-label="删除此用料">×</button></td></tr>`;
}
function editProject(id) {
  const p = projects().find((p) => p.id === id) || {
    name: "",
    plannedUnits: 1,
    bom: [],
  };
  openDialog(
    id ? "编辑计划与 BOM" : "新建项目",
    "每板用量与位号属于这个项目。修改 BOM 不会改变历史领料。",
    `<form id="project-form" data-id="${esc(id || "")}"><div class="error-box form-error"></div><div class="form-grid">${field("项目名称 *", "name", p.name, "required")}${field("计划总块数 *", "plannedUnits", p.plannedUnits, `type="number" min="${p.issuedUnits || 0}" step="1" required`)}<div class="field full"><small>已领 ${p.issuedUnits || 0} 块；计划数不能低于已领数。计划等于已领时，释放其余待领预留。</small></div></div><table class="bom-editor"><thead><tr><th>元件（型号 / 编号）</th><th>每板用量</th><th>位号</th><th></th></tr></thead><tbody id="bom-rows">${(p.bom.length ? p.bom : [{}]).map(bomRow).join("")}</tbody></table><button type="button" class="small-button add-bom" data-action="add-bom">＋ 添加用料</button><details class="import-bom"><summary>粘贴 JSON 导入每板 BOM</summary><p class="mini-note">可粘贴 BOM 数组，或包含 bom 字段的对象。用 materialId 或已登记的 supplierPartNumber 匹配；不按名称自动合并。</p><textarea id="bom-json" rows="5" placeholder='[{"supplierPartNumber":"C84270","quantityPerUnit":8,"designatorsText":"LED1–LED8"}]'></textarea><button type="button" class="small-button" data-action="import-bom">校验并替换编辑区</button></details>${actions("保存项目与预留")}</form>`,
    true,
  );
}
function issueProject(id) {
  const p = projects().find((p) => p.id === id);
  if (!p) return;
  const c = snapshot.capacities.find((c) => c.projectId === id);
  openDialog(
    "整板领料",
    `${esc(p.name)} · 每板 ${p.bom.reduce((n, b) => n + b.quantityPerUnit, 0)} 个元件`,
    `<form id="issue-form" data-id="${esc(id)}"><div class="error-box form-error"></div><div class="form-grid">${field("本次领几块 *", "units", 1, `type="number" min="1" max="${p.plannedUnits - p.issuedUnits}" step="1" required`, true)}</div><div class="notice warning">当前已领 ${p.issuedUnits} / 计划 ${p.plannedUnits} 块。${c?.actualWholeUnits == null ? "所需元件尚未全部点数，提交后会列明缺项。" : "会保留其他项目已预留的元件。"}<br>整笔会一次提交；有任何缺项就全部不扣。成功后相应待领预留会自动释放。</div>${actions("检查并整板领料")}</form>`,
  );
}
function readableDetails(details) {
  if (!details) return "";
  if (details.missing) return readableDetails(details.missing);
  if (details.projects)
    return (
      "受影响项目：" +
      details.projects.map((p) => p.name).join("、") +
      (details.available != null
        ? `；实物自由数 ${details.available}，本次需要 ${details.required}`
        : "")
    );
  if (typeof details === "string") return details;
  if (Array.isArray(details))
    return details
      .map((d) => {
        if (typeof d === "string") return d;
        const m = material(d.materialId);
        const parts = [];
        if (m) parts.push(`${m.name} ${m.supplierPartNumber || ""}`);
        else if (d.materialId) parts.push(d.materialId);
        if (d.message || d.reason || d.status)
          parts.push(
            d.message ||
              d.reason ||
              (d.status === "not_counted" ? "待点数" : d.status),
          );
        if (d.required != null) parts.push(`需要 ${d.required}`);
        if (d.available != null) parts.push(`可用 ${d.available}`);
        else if ("available" in d) parts.push("可用数待点数");
        if (d.shortage != null) parts.push(`缺 ${d.shortage}`);
        return parts.length ? parts.join(" · ") : JSON.stringify(d);
      })
      .join("\n");
  return JSON.stringify(details, null, 2);
}
async function submitAction(form, action, payload) {
  if (form.dataset.pending) return;
  const fingerprint = JSON.stringify({ action, payload });
  if (form.dataset.fingerprint !== fingerprint) {
    form.dataset.requestId = uuid();
    form.dataset.fingerprint = fingerprint;
  }
  form.dataset.pending = "true";
  const button = form.querySelector('[type="submit"]');
  const original = button?.textContent;
  if (button) {
    button.disabled = true;
    button.textContent = "正在保存…";
  }
  const errorBox = form.querySelector(".form-error");
  if (errorBox) errorBox.textContent = "";
  setStatus("正在写入本地数据库…");
  try {
    const response = await api("/api/action", {
      method: "POST",
      body: JSON.stringify({
        action,
        payload,
        requestId: form.dataset.requestId,
      }),
    });
    snapshot = response.snapshot || (await api("/api/state"));
    if (response.result?.safetyBackupPath) {
      lastSafetyBackupPath = response.result.safetyBackupPath;
      try {
        sessionStorage.setItem("warehouse-safety-backup", lastSafetyBackupPath);
      } catch {}
    }
    dialog.close();
    render();
    setStatus("已保存到本地数据库");
    let message = response.result?.message || "已保存到本地数据库";
    if (response.result?.warnings?.length)
      message += `；${readableDetails(response.result.warnings)}`;
    if (response.result?.safetyBackupPath)
      message += " 恢复前的账本已自动保存，位置见下方“安全快照路径”。";
    notify(message);
  } catch (error) {
    if (errorBox)
      errorBox.textContent =
        error.message +
        (error.details ? "\n" + readableDetails(error.details) : "");
    else notify(error.message);
    setStatus("本次未保存，请检查提示", true);
  } finally {
    delete form.dataset.pending;
    if (button) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  event.preventDefault();
  const values = Object.fromEntries(new FormData(form));
  if (form.id === "material-form")
    submitAction(form, "material.save", {
      ...values,
      ...(form.dataset.id ? { id: form.dataset.id } : {}),
    });
  else if (form.id === "movement-form") {
    if (!values.locationCode) delete values.locationCode;
    submitAction(form, "stock.record", {
      ...values,
      quantity: int(values.quantity),
    });
  } else if (form.id === "purchase-form")
    submitAction(form, "purchase.add", {
      ...values,
      quantity: int(values.quantity),
    });
  else if (form.id === "project-form") {
    const bom = [...form.querySelectorAll("#bom-rows tr")].map((row) => ({
      materialId: row.querySelector('[name="bom-material"]').value,
      quantityPerUnit: int(row.querySelector('[name="bom-quantity"]').value),
      designatorsText: row.querySelector('[name="bom-designators"]').value,
    }));
    submitAction(form, "project.save", {
      name: values.name,
      plannedUnits: int(values.plannedUnits),
      bom,
      ...(form.dataset.id ? { id: form.dataset.id } : {}),
    });
  } else if (form.id === "issue-form")
    submitAction(form, "project.issue", {
      projectId: form.dataset.id,
      units: int(values.units),
    });
  else if (form.id === "restore-form" && pendingBackup)
    submitAction(form, "backup.restore", { backup: pendingBackup });
});
document.addEventListener("click", (event) => {
  const page = event.target.closest("[data-page]");
  if (page) {
    currentPage = page.dataset.page;
    render();
    return;
  }
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action,
    id = target.dataset.id;
  if (action === "close-dialog") dialog.close();
  else if (action === "reload") load();
  else if (action === "new-material") editMaterial();
  else if (action === "edit-material") editMaterial(id);
  else if (action === "detail") showDetail(id);
  else if (action === "movement") editMovement(id);
  else if (action === "purchase") editPurchase(id);
  else if (action === "new-project") editProject();
  else if (action === "edit-project") editProject(id);
  else if (action === "issue-project") issueProject(id);
  else if (action === "add-bom")
    document
      .getElementById("bom-rows")
      .insertAdjacentHTML("beforeend", bomRow());
  else if (action === "remove-bom") target.closest("tr").remove();
  else if (action === "import-bom") {
    const errorBox = target.closest("form").querySelector(".form-error");
    errorBox.textContent = "";
    try {
      const parsed = JSON.parse(document.getElementById("bom-json").value);
      const bom = Array.isArray(parsed) ? parsed : parsed.bom;
      if (!Array.isArray(bom) || !bom.length)
        throw new Error("需要一个非空 BOM 数组。");
      const seen = new Set();
      const rows = bom.map((b) => {
        const candidates = b.materialId
          ? materials().filter((m) => m.id === b.materialId)
          : materials().filter(
              (m) =>
                m.supplierPartNumber &&
                m.supplierPartNumber === b.supplierPartNumber &&
                (!b.supplier || b.supplier === m.supplier),
            );
        if (candidates.length > 1)
          throw new Error(
            "同一编号对应多个供应商，请补充 supplier 或使用 materialId。",
          );
        const m = candidates[0];
        if (!m)
          throw new Error(
            `找不到元件：${b.materialId || b.supplierPartNumber || "未提供编号"}，请先新增该元件。`,
          );
        if (seen.has(m.id))
          throw new Error(`同一元件 ${m.name} 出现两次，请合并每板用量。`);
        if (!Number.isSafeInteger(b.quantityPerUnit) || b.quantityPerUnit < 1)
          throw new Error("每板用量必须为正整数。");
        seen.add(m.id);
        return { ...b, materialId: m.id };
      });
      document.getElementById("bom-rows").innerHTML = rows.map(bomRow).join("");
      notify("BOM 已载入编辑区；点击“保存项目与预留”后生效");
    } catch (error) {
      errorBox.textContent = `BOM 校验失败：${error.message}`;
    }
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "search") {
    filters.q = event.target.value;
    renderRows();
  }
  if (event.target.id === "history-search") {
    historyQuery = event.target.value;
    renderHistoryRows();
  }
});
document.addEventListener("change", async (event) => {
  const el = event.target;
  if (el.id === "category-filter") {
    filters.category = el.value;
    renderRows();
  }
  if (el.id === "package-filter") {
    filters.package = el.value;
    renderRows();
  }
  if (el.id === "status-filter") {
    filters.status = el.value;
    renderRows();
  }
  if (el.name === "materialId" && el.closest("#movement-form")) {
    el.closest("form").querySelector('[name="locationCode"]').value =
      stock(el.value).locationCode || "";
  }
  if (el.name === "type" && el.closest("#movement-form")) {
    const tips = {
      receipt:
        "填写本次实际收到的数量；第一次收货结束待点数状态。确认本次为 0 时也可填写 0。",
      count:
        "数量填写“此刻实际在手的总数”，不是增加多少。可用于确认 0 个或纠正错误；原流水仍保留。",
      return:
        "填写实际回到仓库的数量。归还会增加实物余额，但不会回退项目已领套数。",
      issue:
        "填写本次拿走的数量。不能超出实物自由数；已预留的用料会受到保护，需要时先调整相关项目计划。",
      damage: "填写本次焊坏的数量。不能超出实物在手数；损耗可能造成计划缺料。",
      loss: "填写本次损耗的数量。不能超出实物在手数；损耗可能造成计划缺料。",
    };
    document.getElementById("movement-tip").textContent = tips[el.value];
  }
  if (el.id === "backup-file") {
    const errorBox = document.getElementById("backup-error"),
      previewEl = document.getElementById("backup-preview");
    errorBox.textContent = "";
    previewEl.innerHTML = "";
    pendingBackup = null;
    if (!el.files[0]) return;
    try {
      if (el.files[0].size > 64 * 1024 * 1024)
        throw new Error("文件超过 64 MB，请确认它是本软件导出的 JSON。");
      const backup = JSON.parse(await el.files[0].text());
      const response = await api("/api/backup/preview", {
        method: "POST",
        body: JSON.stringify(backup),
      });
      pendingBackup = backup;
      const preview = response.preview;
      const sum = preview.summary || preview;
      previewEl.innerHTML = `<div class="backup-preview"><strong>文件校验通过 · 准备替换恢复</strong><p>文件：${esc(el.files[0].name)}<br>导出时间：${esc(date(backup.exportedAt))}</p><p>元件 ${fmt(sum.materialKinds ?? preview.materialKinds)} 种 · 项目 ${fmt(sum.projectKinds ?? preview.projectKinds)} 个<br>采购记录 ${fmt(sum.orderedPieces)} 个 · 待领预留 ${fmt(sum.reservedPieces)} 个<br>预计自由余料 ${fmt(sum.forecastFreePieces)} 个<br>实物在手 ${fmt(sum.actualOnHandPieces)} · 实物流水 ${fmt(preview.movementCount)} 笔</p><form id="restore-form"><div class="error-box form-error"></div><label class="restore-confirm"><input type="checkbox" required>我已备份现有数据，确认用此文件替换整个仓库。</label><div class="form-actions"><button class="primary" type="submit">确认替换恢复</button></div></form></div>`;
    } catch (error) {
      errorBox.textContent = `无法恢复：${error.message}`;
    }
  }
  if (el.id === "seed-file" && el.files[0]) {
    try {
      const seed = JSON.parse(await el.files[0].text());
      const response = await api("/api/action", {
        method: "POST",
        body: JSON.stringify({
          action: "seed.import",
          payload: { seed },
          requestId: uuid(),
        }),
      });
      snapshot = response.snapshot || (await api("/api/state"));
      notify(response.result?.message || "初始化数据已检查");
      render();
    } catch (error) {
      notify(error.message);
      el.value = "";
    }
  }
});
load();
