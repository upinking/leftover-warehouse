"use strict";

// 导入只在内存中准备文件、列映射和匹配结果，点击最后的确认按钮才会写入账本。
// 数量检查和元件匹配仍由本机服务负责，页面不会自行把采购数当成库存。
window.BOMImport = (() => {
  const fields = [
    ["name", "元件名称"],
    ["manufacturerPartNumber", "厂家型号"],
    ["supplierPartNumber", "供应商编号 / 立创 C 编号"],
    ["package", "封装"],
    ["quantityPerUnit", "每板用量 *"],
    ["designatorsText", "位号"],
    ["specification", "规格"],
  ];
  const statusNames = {
    matched: "已匹配",
    new: "将新建资料",
    unresolved: "待选择元件",
    conflict: "资料冲突",
    skipped: "已跳过",
    invalid: "数据有误",
  };
  let state = null;

  function open(projectId = "") {
    if (state?.busy === "import") return;
    const project = projects().find((p) => p.id === projectId);
    state = {
      token: uuid(),
      step: 0,
      projectId: project?.id || "",
      name: project?.name || "",
      plannedUnits: project?.plannedUnits ?? 0,
      filename: "",
      contentBase64: "",
      sheet: "",
      headerRow: 1,
      parsed: null,
      mapping: {},
      resolutions: {},
      preview: null,
      previewInput: null,
      attempt: null,
      busy: "",
      error: "",
      dirty: false,
    };
    draw();
  }

  function current() {
    return (
      state &&
      document.getElementById("bom-import-root")?.dataset.token ===
        state.token &&
      dialog.open
    );
  }

  function draw() {
    if (!state) return;
    const s = state;
    const body = `<div id="bom-import-root" data-token="${esc(s.token)}" aria-busy="${Boolean(s.busy)}">
      <ol class="bom-import-steps" aria-label="导入步骤">${["选择文件", "列映射", "匹配预览"].map((text, index) => `<li class="${s.step === index ? "active" : s.step > index ? "done" : ""}"><span>${index + 1}</span>${text}</li>`).join("")}</ol>
      <div class="notice"><span>导入的是<strong>每一块板</strong>的用料。不会登记采购、收货或实物领用；新元件的实物数量仍为“待点数”。</span></div>
      <div class="error-box bom-import-error" role="alert">${esc(s.error)}</div>
      ${projectFields()}
      ${s.step === 0 ? fileStep() : s.step === 1 ? mappingStep() : previewStep()}
      ${s.busy ? `<div class="bom-import-working" role="status">${s.busy === "parse" ? "正在读取文件…" : s.busy === "preview" ? "正在检查每一行元件和数量…" : "正在写入本地账本，请勿重复提交…"}</div>` : ""}
    </div>`;
    openDialog("导入 BOM", "CSV / XLSX 文件 · 先检查，再确认导入", body, true);
    dialog.classList.add("bom-import-dialog");
    if (s.busy) lockControls();
  }

  function projectFields() {
    const s = state;
    const project = projects().find((p) => p.id === s.projectId);
    return `<div class="form-grid bom-import-project">
      <label class="field full">导入到<select id="bom-import-project"><option value="">创建新项目</option>${projects()
        .map(
          (p) =>
            `<option value="${esc(p.id)}" ${p.id === s.projectId ? "selected" : ""}>${esc(p.name)}（替换 BOM）</option>`,
        )
        .join("")}</select></label>
      <label class="field">项目名称 *<input id="bom-import-name" value="${esc(s.name)}" maxlength="200" placeholder="填写电路 / 项目名称"></label>
      <label class="field">计划总块数 *<input id="bom-import-planned" type="number" min="${project?.issuedUnits ?? 0}" step="1" value="${esc(s.plannedUnits)}"></label>
      <div class="field full"><small id="bom-import-project-note">${project ? `将替换“${esc(project.name)}”的当前 BOM，重新计算剩余预留；已领 ${project.issuedUnits} 块及过去的领料用量保持原记录。` : "新项目默认计划 0 块；以后设置计划数时才产生待领预留。"}</small></div>
    </div>`;
  }

  function fileStep() {
    return `<label class="field bom-import-file">选择 BOM 文件<input id="bom-import-file" type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"><small>支持 CSV、XLSX，不支持旧版 .xls。文件内容只在本机解析。</small></label>
      ${state.filename ? `<p class="mini-note">已选择：${esc(state.filename)}</p>` : ""}
      <div class="form-actions"><button type="button" data-action="close-dialog">取消</button>${state.contentBase64 ? '<button type="button" class="primary" data-bom-action="parse">读取文件</button>' : ""}</div>`;
  }

  function columnOptions(value) {
    return `<option value="">不导入此项</option>${state.parsed.columns.map((column) => `<option value="${esc(column.index)}" ${column.index === value ? "selected" : ""}>第 ${Number(column.index) + 1} 列 · ${esc(column.label || "无标题")}</option>`).join("")}`;
  }

  function mappingStep() {
    const s = state,
      parsed = s.parsed;
    return `<div class="bom-import-file-info"><strong>${esc(s.filename)}</strong><span>${parsed.rowCount} 行数据</span><button type="button" class="text-button" data-bom-action="choose-file">更换文件</button></div>
      <div class="bom-import-source"><label class="field">工作表<select id="bom-import-sheet">${(parsed.sheets || []).map((sheet) => `<option value="${esc(sheet.id)}" ${String(sheet.id) === String(s.sheet) ? "selected" : ""}>${esc(sheet.name)}</option>`).join("")}</select></label><label class="field">标题行（从 1 开始）<input id="bom-import-header" type="number" min="1" step="1" value="${esc(s.headerRow)}"></label><button type="button" data-bom-action="reparse">重新读取</button></div>
      <h3 class="bom-import-heading">对应哪些列</h3><div class="bom-import-mappings">${fields.map(([key, label]) => `<label class="field">${label}<select data-bom-mapping="${key}">${columnOptions(s.mapping[key])}</select></label>`).join("")}</div>
      <p class="mini-note">每板用量必须是正整数。如果文件写的是整批总用量，请先换算成每板用量。位号数量与用量不一致时会提示；不需要导入位号可选“不导入此项”。</p>
      <details class="bom-import-sample" open><summary>文件内容示例</summary><div class="table-wrap bom-import-scroll"><table><thead><tr><th>源行</th>${parsed.columns.map((column) => `<th>${esc(column.label || `第 ${Number(column.index) + 1} 列`)}</th>`).join("")}</tr></thead><tbody>${(
        parsed.sampleRows || []
      )
        .slice(0, 8)
        .map(
          (row) =>
            `<tr><td>${esc(row.rowNumber)}</td>${parsed.columns.map((column) => `<td>${esc(row.cells?.[column.index])}</td>`).join("")}</tr>`,
        )
        .join("")}</tbody></table></div></details>
      <div class="form-actions"><button type="button" data-bom-action="choose-file">上一步</button><button type="button" class="primary" data-bom-action="preview">检查匹配</button></div>`;
  }

  function rowOptions(row) {
    const resolution = state.resolutions[row.rowNumber];
    const value =
      resolution?.mode === "match"
        ? `match:${resolution.materialId}`
        : resolution?.mode || "";
    const currentMaterial = material(row.materialId);
    const automatic =
      row.status === "matched" && currentMaterial
        ? `自动匹配：${currentMaterial.name}`
        : "请选择处理方式";
    return `<option value="" ${value === "" ? "selected" : ""}>${esc(automatic)}</option><optgroup label="已有元件">${materials()
      .map(
        (m) =>
          `<option value="match:${esc(m.id)}" ${value === `match:${m.id}` ? "selected" : ""}>${esc([m.name, m.supplierPartNumber || m.manufacturerPartNumber, m.package].filter(Boolean).join(" · "))}</option>`,
      )
      .join(
        "",
      )}</optgroup><option value="new" ${value === "new" ? "selected" : ""}>新建元件资料（实物待点数）</option><option value="skip" ${value === "skip" ? "selected" : ""}>跳过此行</option>`;
  }

  function issueList(items, warning = false) {
    if (!items?.length) return "";
    return `<div class="${warning ? "notice warning" : "error-box"}"><div>${items.map((item) => `<div>${esc(typeof item === "string" ? item : `${item.rowNumber == null ? "" : `第 ${item.rowNumber} 行：`}${item.message || item.reason || "请检查此项"}`)}</div>`).join("")}</div></div>`;
  }

  function previewStep() {
    const s = state,
      preview = s.preview,
      counts = preview.counts || {};
    const ready = validPreview();
    return `<div class="bom-import-file-info"><strong>${esc(s.filename)}</strong><span>${esc(preview.rows.length)} 行</span></div>
      <div class="bom-import-counts">${[
        ["已匹配", counts.matched],
        ["新建资料", counts.new],
        [
          "待处理",
          (counts.unresolved || 0) +
            (counts.conflict || 0) +
            (counts.invalid || 0),
        ],
        ["跳过", counts.skipped],
        ["每板合计", counts.unitsPerBoard],
      ]
        .map(
          ([label, count]) =>
            `<span>${label}<strong>${esc(count ?? 0)}</strong>${label === "每板合计" ? " 个" : " 行"}</span>`,
        )
        .join("")}</div>
      <div id="bom-import-dirty" class="notice warning" ${s.dirty ? "" : "hidden"}>内容已调整，尚未重新检查。请点“重新匹配预览”后再确认导入。</div>
      ${issueList(preview.errors)}${issueList(preview.warnings, true)}
      <div class="notice">每行都可以选择已有元件、新建资料或跳过。型号、封装有冲突时请核对实物身份，不要仅凭相似名称合并。</div>
      <div class="table-wrap bom-import-scroll bom-import-preview"><table><thead><tr><th>源行</th><th>文件中的元件资料</th><th class="num">每板用量</th><th>位号</th><th>匹配结果</th><th>处理方式</th></tr></thead><tbody>${preview.rows.map((row) => `<tr><td>${esc(row.rowNumber)}</td><td><div class="material-name">${esc(row.name || "未填写名称")}</div><div class="mini-note">${esc(row.supplierPartNumber || "无供应商编号")} · ${esc(row.package || "未填写封装")}</div><div class="mono muted">${esc(row.manufacturerPartNumber)}</div></td><td class="num">${esc(row.quantityPerUnit ?? "—")}</td><td>${esc(row.designatorsText || "—")}</td><td><span class="pill ${["invalid", "conflict", "unresolved"].includes(row.status) ? "danger" : row.status === "new" ? "pending" : "ok"}">${esc(statusNames[row.status] || row.status)}</span><div class="mini-note">${esc(row.message)}</div></td><td><select data-bom-resolution="${esc(row.rowNumber)}" aria-label="第 ${esc(row.rowNumber)} 行处理方式">${rowOptions(row)}</select></td></tr>`).join("")}</tbody></table></div>
      <p class="mini-note">${s.projectId ? "确认后替换目标项目的当前 BOM；历史领料用量不会改变。" : "确认后创建新项目和明确选择的新元件资料。"}采购和实物在手数都不会增加。</p>
      <div class="form-actions"><button type="button" data-bom-action="mapping">返回列映射</button><button type="button" data-bom-action="preview">重新匹配预览</button><button id="bom-import-confirm" type="button" class="primary" data-bom-action="confirm" ${ready ? "" : "disabled"}>确认导入${s.projectId ? "并替换 BOM" : ""}</button></div>`;
  }

  function inputPayload() {
    return {
      filename: state.filename,
      contentBase64: state.contentBase64,
      sheet: state.sheet,
      headerRow: Number(state.headerRow),
      mapping: { ...state.mapping },
      resolutions: structuredClone(state.resolutions),
      projectId: state.projectId || null,
      name: state.name.trim(),
      plannedUnits: Number(state.plannedUnits),
    };
  }

  function validPreview() {
    return Boolean(
      state.preview?.canImport &&
      !state.dirty &&
      state.previewInput === JSON.stringify(inputPayload()),
    );
  }

  function markDirty() {
    state.dirty = true;
    state.attempt = null;
    state.error = "";
    const note = document.getElementById("bom-import-dirty");
    if (note) note.hidden = false;
    const confirm = document.getElementById("bom-import-confirm");
    if (confirm) confirm.disabled = true;
    const error = document.querySelector(".bom-import-error");
    if (error) error.textContent = "";
  }

  function validateProject() {
    if (!state.name.trim()) throw new Error("请填写项目名称。");
    if (String(state.plannedUnits).trim() === "")
      throw new Error("请填写计划总块数；暂不计划制作可以填写 0。");
    const planned = Number(state.plannedUnits);
    const project = projects().find((p) => p.id === state.projectId);
    if (
      !Number.isSafeInteger(planned) ||
      planned < (project?.issuedUnits ?? 0)
    ) {
      throw new Error(
        project
          ? `计划总块数必须为整数，且不能低于已领的 ${project.issuedUnits} 块。`
          : "计划总块数必须为非负整数。",
      );
    }
  }

  function lockControls() {
    const root = document.getElementById("bom-import-root");
    root?.querySelectorAll("input,select,button").forEach((control) => {
      control.disabled = true;
    });
    if (state.busy === "import")
      dialog.querySelector(".close-dialog").disabled = true;
  }

  function showError(error) {
    state.error =
      error.message +
      (error.details ? `\n${readableDetails(error.details)}` : "");
    if (current()) draw();
  }

  async function readFile(file) {
    if (!file || state.busy) return;
    if (!/\.(csv|xlsx)$/i.test(file.name)) {
      showError(
        new Error("请选择 CSV 或 XLSX 文件；旧版 .xls 请先另存为 .xlsx。"),
      );
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      showError(
        new Error("文件超过 8 MB，请先删去与 BOM 无关的工作表或内容。"),
      );
      return;
    }
    const active = state;
    active.busy = "parse";
    active.error = "";
    lockControls();
    try {
      const encoded = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1]);
        reader.onerror = () =>
          reject(new Error("文件读取失败，请重新选择文件。"));
        reader.readAsDataURL(file);
      });
      if (state !== active) return;
      active.filename = file.name;
      active.contentBase64 = encoded;
      active.sheet = "";
      active.headerRow = null;
      active.preview = null;
      active.attempt = null;
      active.resolutions = {};
      if (!active.projectId && !active.name)
        active.name = file.name.replace(/\.(csv|xlsx)$/i, "");
      active.busy = "";
      await parse();
    } catch (error) {
      active.busy = "";
      if (state === active) showError(error);
    }
  }

  async function parse() {
    if (!state?.contentBase64 || state.busy) return;
    const active = state;
    active.busy = "parse";
    active.error = "";
    draw();
    try {
      const payload = {
        filename: active.filename,
        contentBase64: active.contentBase64,
      };
      if (active.headerRow != null) payload.headerRow = Number(active.headerRow);
      if (active.sheet) payload.sheet = active.sheet;
      const parsed = await api("/api/bom/parse", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (state !== active) return;
      active.parsed = parsed;
      active.sheet = parsed.sheet;
      active.headerRow = parsed.headerRow;
      active.mapping = Object.fromEntries(
        fields.map(([key]) => [key, parsed.suggestedMapping?.[key] ?? null]),
      );
      active.resolutions = {};
      active.preview = null;
      active.previewInput = null;
      active.attempt = null;
      active.step = 1;
      active.dirty = true;
    } catch (error) {
      active.error =
        error.message +
        (error.details ? `\n${readableDetails(error.details)}` : "");
    } finally {
      active.busy = "";
      if (state === active && current()) draw();
    }
  }

  async function preview() {
    if (!state?.parsed || state.busy) return;
    try {
      validateProject();
      if (
        String(state.sheet) !== String(state.parsed.sheet) ||
        Number(state.headerRow) !== Number(state.parsed.headerRow)
      )
        throw new Error(
          "工作表或标题行已改变，请先点“重新读取”，再检查列映射。",
        );
      if (state.mapping.quantityPerUnit == null)
        throw new Error("请先选择“每板用量”对应的列。");
      const active = state,
        payload = inputPayload();
      active.busy = "preview";
      active.error = "";
      draw();
      try {
        const result = await api("/api/bom/preview", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        if (state !== active) return;
        active.preview = result;
        active.previewInput = JSON.stringify(payload);
        active.step = 2;
        active.dirty = false;
      } catch (error) {
        active.error =
          error.message +
          (error.details ? `\n${readableDetails(error.details)}` : "");
      } finally {
        active.busy = "";
        if (state === active && current()) draw();
      }
    } catch (error) {
      showError(error);
    }
  }

  async function confirm() {
    if (!state || state.busy) return;
    if (!validPreview()) {
      showError(new Error("文件、列映射或项目资料已改变，请重新匹配预览。"));
      return;
    }
    const active = state;
    const payload = {
      ...JSON.parse(active.previewInput),
      baseRevision: active.preview.baseRevision,
      baseFingerprint: active.preview.baseFingerprint,
    };
    const fingerprint = JSON.stringify(payload);
    if (!active.attempt || active.attempt.fingerprint !== fingerprint)
      active.attempt = { fingerprint, requestId: uuid() };
    active.busy = "import";
    active.error = "";
    draw();
    setStatus("正在保存导入的 BOM…");
    try {
      const result = await api("/api/action", {
        method: "POST",
        body: JSON.stringify({
          action: "bom.import",
          payload,
          requestId: active.attempt.requestId,
        }),
      });
      snapshot = result.snapshot || (await api("/api/state"));
      active.busy = "";
      dialog.close();
      dialog.classList.remove("bom-import-dialog");
      state = null;
      currentPage = "projects";
      render();
      setStatus("已保存到本地数据库");
      notify(result.result?.message || "BOM 已导入，采购与实物在手数未增加。");
    } catch (error) {
      active.busy = "";
      active.error = `${error.message}${error.details ? `\n${readableDetails(error.details)}` : ""}\n如连接中断，可再次点击确认；重试会使用同一请求标识，避免重复导入。`;
      setStatus("本次导入未确认，请检查提示", true);
      if (state === active && current()) draw();
    }
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-bom-action]");
    if (!button || !current() || state.busy) return;
    const action = button.dataset.bomAction;
    if (action === "parse" || action === "reparse") parse();
    else if (action === "preview") preview();
    else if (action === "confirm") confirm();
    else if (action === "choose-file") {
      state.step = 0;
      draw();
    } else if (action === "mapping") {
      state.step = 1;
      draw();
    }
  });

  function updateValue(target) {
    if (!current() || state.busy) return;
    if (target.id === "bom-import-name") {
      state.name = target.value;
      markDirty();
    } else if (target.id === "bom-import-planned") {
      state.plannedUnits = target.value;
      markDirty();
    } else if (target.id === "bom-import-header") {
      state.headerRow = target.value;
      markDirty();
    } else if (target.id === "bom-import-sheet") {
      state.sheet = target.value;
      markDirty();
    } else if (target.dataset.bomMapping) {
      state.mapping[target.dataset.bomMapping] =
        target.value === "" ? null : Number(target.value);
      markDirty();
    } else if (target.dataset.bomResolution) {
      const value = target.value,
        key = target.dataset.bomResolution;
      if (!value) delete state.resolutions[key];
      else if (value.startsWith("match:"))
        state.resolutions[key] = { mode: "match", materialId: value.slice(6) };
      else state.resolutions[key] = { mode: value };
      markDirty();
    }
  }
  document.addEventListener("input", (event) => updateValue(event.target));
  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!current() || state.busy) return;
    if (target.id === "bom-import-file") {
      readFile(target.files[0]);
      return;
    }
    if (target.id === "bom-import-project") {
      const project = projects().find((p) => p.id === target.value);
      state.projectId = project?.id || "";
      state.name =
        project?.name || state.filename.replace(/\.(csv|xlsx)$/i, "");
      state.plannedUnits = project?.plannedUnits ?? 0;
      markDirty();
      draw();
      return;
    }
    updateValue(target);
  });
  document.addEventListener(
    "click",
    (event) => {
      if (
        current() &&
        state.busy === "import" &&
        event.target.closest('[data-action="close-dialog"]')
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    },
    true,
  );
  document.addEventListener(
    "cancel",
    (event) => {
      if (event.target === dialog && current() && state.busy === "import")
        event.preventDefault();
    },
    true,
  );
  document.addEventListener(
    "close",
    (event) => {
      if (event.target === dialog) dialog.classList.remove("bom-import-dialog");
    },
    true,
  );
  return Object.freeze({ open });
})();
