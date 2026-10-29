/* 群唤醒增强 Pro · 规则面板
 *
 * 数据模型：一条规则 = 一批设置 + 两条条件流水线。
 * ① 接收消息（rule.条件）：消息进来时判断，第一条命中的决定唤醒还是屏蔽；
 * ② LLM 请求前（rule.LLM前条件）：进入模型前再判一次，只放屏蔽类条件。
 * 唤醒类条件可以带自己的 system_reminder 提示词。
 *
 * 性能：全部静态 DOM + CSS，无 canvas / 无动画循环 / 无 backdrop-filter。
 */

(() => {
  "use strict";

  const BRIDGE_WAIT_MS = 4000;

  function getBridge() {
    return typeof window !== "undefined" ? window.AstrBotPluginPage || null : null;
  }

  function waitForBridge(timeout = BRIDGE_WAIT_MS) {
    return new Promise((resolve) => {
      const startedAt = Date.now();
      const tick = () => {
        const bridge = getBridge();
        if (bridge) return resolve(bridge);
        if (Date.now() - startedAt > timeout) return resolve(null);
        setTimeout(tick, 50);
      };
      tick();
    });
  }

  const $ = (sel) => document.querySelector(sel);
  const clone = (v) => JSON.parse(JSON.stringify(v));

  const S = {
    data: null,
    selected: "",
    draft: null,
    dirty: false,
    saving: false,
    inited: false,
    openDD: null,
  };

  const FALLBACK_OPTIONS = {
    概率方式: ["纯文本", "图片", "卡片分享", "任何消息"],
    活跃方式: ["唤醒时", "llm请求后", "发送消息后"],
    活跃范围: ["群聊级", "用户级"],
  };

  /* ---------------------------------------------------------------- 基础工具 */

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = String(v);
      else if (k === "html") node.innerHTML = v;
      else if (k === "value" && (tag === "textarea" || tag === "input")) node.value = String(v);
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) node.setAttribute(k, "");
      else node.setAttribute(k, String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  function toast(message, kind = "") {
    const node = el("div", { class: `toast ${kind}`, text: message });
    $("#toasts").append(node);
    setTimeout(() => {
      node.style.opacity = "0";
      node.style.transition = "0.2s ease";
      setTimeout(() => node.remove(), 220);
    }, kind === "error" ? 5200 : 2600);
  }

  async function apiGet(path) {
    const bridge = getBridge();
    if (!bridge) throw new Error("页面桥接未就绪，请从「插件 → 群唤醒增强 Pro → 规则面板」进入");
    return bridge.apiGet(path, {});
  }

  async function apiPost(path, body) {
    const bridge = getBridge();
    if (!bridge) throw new Error("页面桥接未就绪，请从「插件 → 群唤醒增强 Pro → 规则面板」进入");
    return bridge.apiPost(path, body || {});
  }

  function setBadge(text, cls) {
    const badge = $("#statusBadge");
    badge.textContent = text;
    badge.className = `badge ${cls || ""}`;
  }

  function markDirty() {
    S.dirty = true;
    setBadge("有未保存修改", "dirty");
    const hint = $("#saveHint");
    if (hint) hint.textContent = "有未保存的修改";
  }

  const isFallback = (rule) => !!rule && rule.id === "fallback";

  // 兜底规则下面没有更下层可继承，-1（继承兜底）对它没有意义：读进来先收敛成 0
  const 时间键 = ["持续活跃", "活跃间隔", "唤醒冷却"];
  function 收敛兜底时间(rule) {
    for (const key of 时间键) {
      const value = Number(rule[key]);
      rule[key] = Number.isFinite(value) && value > 0 ? value : 0;
    }
  }

  const typeMeta = (type) =>
    (S.data.条件类型 || []).find((i) => i.类型 === type) || { 动作: "唤醒", 说明: "", 参数: [] };
  const opt = (key) => (S.data.选项 || {})[key] || FALLBACK_OPTIONS[key] || [];

  // 条件类型 -> 全局设置里对应的默认提示词
  const DEFAULT_PROMPT_KEY = {
    概率唤醒: "主动唤醒提示词",
    艾特唤醒: "提及唤醒提示词",
    唤醒正则: "提及唤醒提示词",
    无艾特引用唤醒: "引用唤醒提示词",
    引用唤醒: "引用唤醒提示词",
  };

  function defaultPrompt(type) {
    const key = DEFAULT_PROMPT_KEY[type];
    const glob = (S.data && S.data.全局) || {};
    return (key && glob[key]) || "";
  }

  // 全局提示词字段 -> 内置默认文案的键（「恢复默认」用）
  const BUILTIN_PROMPT_KEY = {
    主动唤醒提示词: "主动唤醒",
    提及唤醒提示词: "提及唤醒",
    引用唤醒提示词: "引用唤醒",
  };

  /* ---------------------------------------------------------------- 两段流水线 */

  const STAGES = {
    recv: {
      key: "条件",
      icon: "⇅",
      title: "接收消息 · 判断流水线",
      sub: "消息进来时逐条判断，第一条命中的决定唤醒或屏蔽",
      empty:
        "还没有条件：插件不唤醒也不屏蔽，完全交给框架默认判定" +
        "（框架自己仍会把 @机器人、引用机器人当成唤醒）。",
      add: "＋ 添加条件",
      cls: "",
      picker: "接收消息流水线",
    },
    llm: {
      key: "LLM前条件",
      icon: "⌛",
      title: "LLM 请求前 · 判断流水线",
      sub: "进入模型前再判一次，只做屏蔽：不出声，也不打断其他插件",
      empty: "留空 = 不额外拦截，按接收流水线与框架的判定进入模型。",
      add: "＋ 添加 LLM 前条件",
      cls: "llm",
      picker: "LLM 请求前流水线",
    },
  };

  function conditionsOf(rule, stage = "recv") {
    const list = rule[STAGES[stage].key];
    return Array.isArray(list) ? list : [];
  }

  function stageTypes(stage) {
    const types = (S.data && S.data.条件类型) || [];
    const flag = stage === "llm" ? "LLM" : "接收";
    return types.filter((t) => (t.阶段 || ["接收"]).includes(flag));
  }

  // @ / 引用类唤醒：开启 = 命中即唤醒，关闭 = 命中即屏蔽（框架也会按这两类唤醒）
  const OFF_HINT = {
    艾特唤醒: "已关闭：@ 了机器人的消息会被直接屏蔽（不再唤醒）",
    引用唤醒: "已关闭：@机器人且引用了它的消息会被直接屏蔽（不再唤醒）",
    无艾特引用唤醒: "已关闭：只引用机器人、没 @ 的消息会被直接屏蔽（不再唤醒）",
  };

  /* ---------------------------------------------------------------- 数据 */

  async function load(opts = {}) {
    setBadge("加载中…");
    closeDropdown();
    try {
      const data = await apiGet("api/overview");
      S.data = data;
      if (!S.data.规则列表) S.data.规则列表 = [];
      renderRuntime();
      const keep = opts.keepSelection ? S.selected : "";
      const exists = keep === "fallback" || S.data.规则列表.some((r) => r.id === keep);
      if (exists) select(keep, { force: true });
      else if (S.data.规则列表.length) select(S.data.规则列表[0].id, { force: true });
      else select("fallback", { force: true });
      setBadge("已同步", "ok");
    } catch (e) {
      setBadge("加载失败", "dirty");
      toast(`加载失败：${e.message || e}`, "error");
    }
  }

  function renderRuntime() {
    const rt = (S.data && S.data.运行时) || {};
    const active = rt.活跃 || {};
    const prefixes = rt.指令前缀 || [];
    $("#boundGroups").textContent = (rt.已绑定群 || []).length;
    $("#activeCount").textContent =
      (active.群聊 || []).length + (active.用户 || []).length + (active.自定义 || []).length;

    const listOfPrefixes = prefixes.length
      ? el(
          "div",
          { class: "ri-list" },
          prefixes.map((p) => el("code", { class: "ri-chip", text: p }))
        )
      : el("div", { class: "ri-empty", text: "未配置" });

    const info = $("#runtimeInfo");
    info.replaceChildren(
      el(
        "div",
        { class: "ri-block" },
        el("div", { class: "ri-key", text: `指令前缀 · ${prefixes.length}` }),
        listOfPrefixes
      ),
      el(
        "div",
        { class: "ri-row" },
        el("span", { text: "指令总数" }),
        el("b", { text: `${(S.data.指令 || []).length} 个` })
      ),
      el(
        "div",
        { class: "ri-row" },
        el("span", { text: "运行时长" }),
        el("b", { text: `${Math.round(rt.运行时长 || 0)} 秒` })
      ),
      el(
        "div",
        { class: "ri-block" },
        el("div", { class: "ri-key", text: "数据目录" }),
        el("div", { class: "ri-path", text: rt.数据目录 || "-" })
      )
    );
  }

  async function select(id, opts = {}) {
    if (S.dirty && !opts.force) {
      const ok = await confirmDialog("当前修改尚未保存，切换后会丢弃这些修改。确定要切换规则吗？", {
        title: "有未保存的修改",
        okText: "丢弃并切换",
        danger: true,
      });
      if (!ok) return;
    }
    const source =
      id === "fallback" ? S.data.兜底规则 : S.data.规则列表.find((r) => r.id === id);
    if (!source) return;
    S.selected = id;
    S.draft = clone(source);
    if (isFallback(S.draft)) 收敛兜底时间(S.draft);
    S.dirty = false;
    setBadge("已同步", "ok");
    renderSidebar();
    renderEditor();
  }

  /* ---------------------------------------------------------------- 侧栏 */

  function ownership() {
    const map = new Map();
    for (const rule of S.data.规则列表 || []) {
      for (const g of rule.群号 || []) {
        if (!g || g === "*") continue;
        if (!map.has(g)) map.set(g, rule);
      }
    }
    return map;
  }

  function renderSidebar() {
    const list = $("#ruleList");
    const rules = S.data.规则列表 || [];
    $("#ruleCount").textContent = String(rules.length);
    const owner = ownership();
    list.replaceChildren();

    if (!rules.length) {
      // 兜底规则开着，没配置的群还能被兜底接管；一旦关掉，插件对任何消息都不做处理
      const 兜底开 = !!(S.data.兜底规则 && S.data.兜底规则.开关);
      list.append(
        el("div", {
          class: 兜底开 ? "rule-note" : "rule-note is-warn",
          text: 兜底开
            ? "还没有自定义规则，未配置的群走「兜底规则」。"
            : "还没有自定义规则，且「兜底规则」已关闭，插件将不会处理任何消息",
        })
      );
    }
    rules.forEach((rule, index) => list.append(ruleItem(rule, owner, false, index, rules.length)));
    list.append(ruleItem(S.data.兜底规则, owner, true, -1, rules.length));
  }

  function ruleItem(rule, owner, fallback, index, total) {
    const conflicts = (rule.群号 || []).filter((g) => {
      if (!g || g === "*") return false;
      const o = owner.get(g);
      return o && o.id !== rule.id;
    });

    const toggle = el("input", {
      type: "checkbox",
      onchange: (e) => toggleRule(rule, e.target.checked),
    });
    toggle.checked = !!rule.开关;

    const groups =
      (rule.群号 || []).map((g) => (g === "*" ? "全部群" : g)).join("、") || "未绑定群号（不生效）";
    const condCount = conditionsOf(rule, "recv").length;
    const llmCount = conditionsOf(rule, "llm").length;
    const condText = llmCount ? `${condCount} 条条件 · ${llmCount} 条 LLM 前` : `${condCount} 条条件`;

    // 只有一条规则时也把按钮画出来（置灰），不然新建规则后完全看不到调整入口
    const move = fallback
      ? null
      : el(
          "div",
          { class: "rule-move", onclick: (e) => e.stopPropagation() },
          el("button", {
            class: "mv-btn",
            type: "button",
            text: "▲",
            title: "上移（更优先）",
            disabled: index <= 0,
            onclick: () => moveRule(rule, -1),
          }),
          el("button", {
            class: "mv-btn",
            type: "button",
            text: "▼",
            title: "下移",
            disabled: index >= total - 1,
            onclick: () => moveRule(rule, 1),
          })
        );

    // 「复制」不常用：只留一个小图标，挂在条目右下角，平时不抢位置
    // 兜底规则也能复制——复制出来是一条普通规则，正好拿来当模板
    const copyBtn = el("button", {
      class: "rule-copy",
      type: "button",
      title: fallback
        ? "把兜底规则复制成一条普通规则：设置与条件照抄，群号与备注留空"
        : "复制这条规则：设置与条件照抄，群号与备注会清空",
      "aria-label": fallback ? "复制兜底规则" : "复制规则",
      html:
        '<svg viewBox="0 0 12 12" width="10" height="10" aria-hidden="true">' +
        '<rect x="4" y="1.3" width="6.7" height="6.7" rx="1.6" />' +
        '<rect x="1.3" y="4" width="6.7" height="6.7" rx="1.6" />' +
        "</svg>",
      onclick: (e) => {
        e.stopPropagation();
        duplicateRule(rule);
      },
    });

    return el(
      "div",
      {
        class: `rule-item ${S.selected === rule.id ? "active" : ""} ${
          rule.开关 ? "" : "is-off"
        } ${fallback ? "is-fallback" : ""}`,
        role: "listitem",
        tabindex: "0",
        onclick: () => select(rule.id),
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            select(rule.id);
          }
        },
      },
      el(
        "label",
        { class: "switch", onclick: (e) => e.stopPropagation() },
        toggle,
        el("span", { class: "switch-track" })
      ),
      el(
        "div",
        { class: "rule-main" },
        el(
          "span",
          { class: "rule-name" },
          rule.备注 || (fallback ? "兜底规则" : "未命名规则"),
          fallback ? el("span", { class: "pill", text: "兜底" }) : null
        ),
        el("div", { class: "rule-sub", text: `${groups} · ${condText}` }),
        conflicts.length
          ? el("div", {
              class: "rule-flag",
              text: `已被接管：${conflicts.join("、")}`,
              title: "这些群号已被更靠前的规则占用，本条规则对它们不生效",
            })
          : null
      ),
      move,
      copyBtn
    );
  }

  async function toggleRule(rule, next) {
    try {
      const copy = clone(rule);
      copy.开关 = !!next;
      if (isFallback(copy)) await apiPost("api/settings/save", { 兜底规则: copy });
      else await apiPost("api/rule/save", { 规则: copy });
      await load({ keepSelection: true });
      toast(next ? "已启用" : "已关闭", "ok");
    } catch (e) {
      toast(`切换失败：${e.message || e}`, "error");
      renderSidebar();
    }
  }

  async function moveRule(rule, delta) {
    if (S.dirty) {
      const ok = await confirmDialog(
        "有未保存的修改，移动规则会重新载入并丢弃它们。确定继续吗？",
        { title: "有未保存的修改", okText: "丢弃并移动", danger: true }
      );
      if (!ok) return;
    }
    S.dirty = false;
    try {
      await apiPost("api/rule/move", { id: rule.id, 方向: delta });
      await load({ keepSelection: true });
    } catch (e) {
      toast(`移动失败：${e.message || e}`, "error");
    }
  }

  /**
   * 复制规则：设置与条件照抄，群号与备注（每条规则专属）由后端清空。
   * 群号留空的规则不进群号索引，等于「存着但哪儿都不生效」，下次补上群号就能用。
   */
  async function duplicateRule(rule) {
    if (S.dirty) {
      const ok = await confirmDialog(
        "有未保存的修改，复制会重新载入并丢弃它们。确定继续吗？",
        { title: "有未保存的修改", okText: "丢弃并复制", danger: true }
      );
      if (!ok) return;
    }
    S.dirty = false;
    try {
      const data = await apiPost("api/rule/duplicate", { id: rule.id });
      await load({ keepSelection: true });
      if (data && data.规则) select(data.规则.id, { force: true });
      toast(
        isFallback(rule)
          ? "已把兜底规则复制成一条普通规则：群号、备注留空，填好就能用"
          : "已复制为新规则：群号、备注已清空，填好备注再保存",
        "ok"
      );
    } catch (e) {
      toast(`复制失败：${e.message || e}`, "error");
    }
  }

  /* ---------------------------------------------------------------- 编辑器 */

  function renderEditor() {
    closeDropdown();
    const editor = $("#editor");
    const rule = S.draft;
    if (!rule) {
      editor.replaceChildren(
        el(
          "div",
          { class: "empty-state" },
          el("div", { class: "empty-mark", text: "◍" }),
          el("h2", { text: "选择左侧规则开始编辑" }),
          el("p", { text: "也可以新建一条规则，为指定群号配置唤醒与屏蔽条件。" })
        )
      );
      return;
    }

    const fallback = isFallback(rule);
    const view = el("div", {
      class: "rule-view",
      style: "display:flex;flex-direction:column;gap:14px",
    });
    view.append(renderHero(rule, fallback));
    view.append(renderPipeline(rule, "recv"));
    view.append(renderPipeline(rule, "llm"));
    for (const section of SETTING_SECTIONS) {
      const rows = section.rows.filter((r) => !(fallback && r.hideInFallback));
      if (!rows.length) continue;
      view.append(renderSettingsSection(section, rows, rule));
    }
    view.append(renderSaveBar(rule, fallback));
    editor.replaceChildren(view);
  }

  function renderHero(rule, fallback) {
    // 「待命名」和提示条会跟着输入框实时变化，所以先建好节点再按当前值刷新
    const nameChip = el("span", { class: "chipstat warn", text: "待命名" });
    const nameNote = el("div", {
      class: "hero-note",
      text: "还没填备注：备注用来区分每条规则，保存前必须填写。",
    });
    const notes = el("div", { class: "hero-notes" }, nameNote);

    const refreshHints = () => {
      const empty = !fallback && !String(rule.备注 || "").trim();
      nameChip.style.display = empty ? "" : "none";
      nameNote.style.display = empty ? "" : "none";
      notes.style.display = Array.from(notes.children).some((n) => n.style.display !== "none")
        ? ""
        : "none";
    };

    const title = el("input", {
      class: "hero-title",
      value: fallback ? "兜底规则" : rule.备注 || "",
      placeholder: "规则名称（必填）",
      readonly: fallback ? true : null,
      title: fallback ? "兜底规则名称固定" : "点击修改规则名称",
      oninput: (e) => {
        if (fallback) return;
        rule.备注 = e.target.value;
        markDirty();
        refreshHints();
        const active = document.querySelector(".rule-item.active .rule-name");
        if (active) {
          const pill = active.querySelector(".pill");
          active.textContent = rule.备注 || "未命名规则";
          if (pill) active.append(pill);
        }
      },
    });

    const switchInput = el("input", {
      type: "checkbox",
      onchange: (e) => {
        rule.开关 = e.target.checked;
        markDirty();
      },
    });
    switchInput.checked = !!rule.开关;

    const 群数 = (rule.群号 || []).length;
    const 通配群 = (rule.群号 || []).includes("*");
    if (!fallback && !群数) {
      notes.append(
        el("div", {
          class: "hero-note",
          text: "还没绑定群号：这条规则会照常保存，但不会在任何群生效，填上群号就生效。",
        })
      );
    }
    refreshHints();

    return el(
      "section",
      { class: "hero" },
      el(
        "div",
        { class: "hero-top" },
        title,
        el(
          "div",
          { class: "hero-meta" },
          el("span", { class: "chipstat", text: `${conditionsOf(rule, "recv").length} 条条件` }),
          conditionsOf(rule, "llm").length
            ? el("span", {
                class: "chipstat",
                text: `${conditionsOf(rule, "llm").length} 条 LLM 前`,
              })
            : null,
          el("span", {
            class: `chipstat ${群数 ? "" : "warn"}`,
            text: 群数 ? (通配群 ? "全部群" : `${群数} 个群`) : "未绑定群号",
          }),
          nameChip,
          el("span", { class: "chipstat", text: fallback ? "内置" : `id ${rule.id}` })
        ),
        el(
          "label",
          { class: "switch" },
          switchInput,
          el("span", { class: "switch-track" }),
          el("span", { class: "switch-text", text: "启用" })
        )
      ),
      notes
    );
  }

  function renderPipeline(rule, stage = "recv") {
    const meta = STAGES[stage];
    const list = conditionsOf(rule, stage);
    const body = el("div", { class: "panel-body" });
    const pipe = el("div", { class: "pipeline" });

    if (!list.length) {
      pipe.append(el("div", { class: "rule-sub", text: meta.empty }));
    }

    list.forEach((条件, index) =>
      pipe.append(renderCondition(rule, 条件, index + 1, list.length, stage))
    );

    const addBtn = el("button", {
      class: "add-cond",
      type: "button",
      text: meta.add,
      onclick: () => openTypePicker(rule, stage),
    });

    body.append(pipe, addBtn);

    return el(
      "section",
      { class: `panel ${meta.cls}` },
      el(
        "div",
        { class: "panel-head" },
        el("span", { class: "panel-icon", text: meta.icon }),
        el(
          "div",
          {},
          el("div", { class: "panel-title", text: meta.title }),
          el("div", { class: "panel-sub", text: meta.sub })
        ),
        el(
          "div",
          { class: "panel-tools" },
          el("span", { class: "pill", text: `${list.length} 条` }),
          el("span", { class: "panel-tip", text: "↑↓ 调整顺序" })
        )
      ),
      body
    );
  }

  function moveCondition(rule, stage, index, delta) {
    const list = rule[STAGES[stage].key] || [];
    const to = index + delta;
    if (to < 0 || to >= list.length) return;
    const [item] = list.splice(index, 1);
    list.splice(to, 0, item);
    markDirty();
    renderEditor();
  }

  function removeCondition(rule, stage, 条件) {
    const key = STAGES[stage].key;
    rule[key] = (rule[key] || []).filter((c) => c.id !== 条件.id);
    markDirty();
    renderEditor();
  }

  function renderCondition(rule, 条件, index, total, stage = "recv") {
    const meta = typeMeta(条件.类型);
    // LLM 前流水线只做屏蔽：同一类型在这里按「屏蔽」呈现
    const isWake = stage === "recv" && meta.动作 === "唤醒";
    const params = meta.参数 || [];

    const card = el("article", {
      class: `cond ${isWake ? "wake" : "block"} ${条件.开关 ? "" : "off"}`,
    });

    const sw = el("input", {
      type: "checkbox",
      onchange: (e) => {
        条件.开关 = e.target.checked;
        card.classList.toggle("off", !e.target.checked);
        markDirty();
        // 关闭/开启都要切换下方提示（红色屏蔽提示 或 灰色跳过提示），直接重绘
        renderEditor();
      },
    });
    sw.checked = !!条件.开关;

    const del = el("button", {
      class: "btn icon ghost",
      type: "button",
      text: "×",
      title: "删除此条件",
      onclick: () => removeCondition(rule, stage, 条件),
    });

    const moveBtns = el(
      "div",
      { class: "move-btns" },
      el("button", {
        class: "mv-btn",
        type: "button",
        text: "▲",
        title: "上移（更先判断）",
        disabled: index <= 1,
        onclick: () => moveCondition(rule, stage, index - 1, -1),
      }),
      el("button", {
        class: "mv-btn",
        type: "button",
        text: "▼",
        title: "下移",
        disabled: index >= total,
        onclick: () => moveCondition(rule, stage, index - 1, 1),
      })
    );

    card.append(
      el(
        "div",
        { class: "cond-head" },
        el("span", { class: "cond-idx", text: String(index).padStart(2, "0") }),
        el("span", { class: "cond-type", text: 条件.类型 }),
        el("span", {
          class: `cond-tag ${isWake ? "wake" : "block"}`,
          text: isWake ? "唤醒" : "屏蔽",
        }),
        el("span", { class: "cond-desc", text: meta.说明 || "" }),
        el(
          "div",
          { class: "cond-ops" },
          moveBtns,
          el("label", { class: "switch", title: "启用/停用此条件" }, sw, el("span", { class: "switch-track" })),
          del
        )
      )
    );

    const offHint = stage === "recv" && !条件.开关 ? OFF_HINT[条件.类型] : "";
    if (offHint) {
      card.append(el("div", { class: "cond-note", text: offHint }));
    } else if (!条件.开关) {
      card.append(
        el("div", {
          class: "cond-note muted",
          text: "已关闭：这条不参与判断，流水线会继续往下执行",
        })
      );
    }

    if (params.includes("正则") || params.includes("概率") || isWake) {
      const body = el("div", { class: "cond-body" });

      if (params.includes("正则")) {
        body.append(
          renderField(
            "正则",
            linesControl({
              values: 条件.正则 || (条件.正则 = []),
              placeholder: "一行一条正则，例如：机器人在吗\n^小助手",
              rows: 3,
              mono: true,
              validate: validateRegex,
            }),
            el("button", {
              class: "btn small ghost",
              type: "button",
              text: "试匹配",
              onclick: () => openRegexTester(条件),
            })
          )
        );
      }

      if (params.includes("概率")) {
        if (!Array.isArray(条件.概率方式)) 条件.概率方式 = ["纯文本"];
        const valueLabel = el("span", {
          class: "unit mono",
          text: `${Math.round((条件.概率 || 0) * 100)}%`,
        });
        const slider = el("input", {
          type: "range",
          min: "0",
          max: "1",
          step: "0.01",
          value: 条件.概率 || 0,
          oninput: (e) => {
            条件.概率 = Number(e.target.value);
            valueLabel.textContent = `${Math.round(条件.概率 * 100)}%`;
            markDirty();
          },
        });
        body.append(
          renderField(
            "触发概率",
            el("div", { class: "slider-row" }, slider, valueLabel)
          )
        );
        body.append(
          renderField(
            "生效消息",
            multiControl(条件.概率方式, FALLBACK_OPTIONS.概率方式, (v) => {
              条件.概率方式 = v;
              markDirty();
            })
          )
        );

        body.append(
          renderField(
            "概率冷却",
            el(
              "div",
              { class: "slider-stack" },
              sliderControl({
                value: 条件.概率冷却 ?? 0,
                min: 0,
                max: 120,
                step: 1,
                unit: "分钟",
                onInput: (v) => {
                  条件.概率冷却 = Math.max(0, Math.min(10080, Math.round(v * 10) / 10));
                  markDirty();
                },
              }),
              el("span", {
                class: "unit",
                text: "命中后这段时间内不再判断这条概率条件（0 = 不冷却）",
              })
            )
          )
        );
      }

      if (isWake) {
        body.append(
          renderField(
            "提示词",
            promptEditor({
              value: 条件.提示词,
              temp: 条件.提示词临时 !== false,
              placeholder: "留空 = 这条条件不注入",
              hint: "",
              onInput: (v) => {
                条件.提示词 = v;
                markDirty();
              },
              onTemp: (v) => {
                条件.提示词临时 = v;
                markDirty();
              },
              onRestore: (area) => {
                const text = defaultPrompt(条件.类型);
                if (!text) {
                  toast("全局设置里没有这类唤醒的默认提示词", "error");
                  return;
                }
                条件.提示词 = text;
                area.value = text;
                markDirty();
              },
            })
          )
        );
      }

      card.append(body);
    }

    return card;
  }

  function renderField(label, control, extra) {
    return el(
      "div",
      { class: "cond-field" },
      el("span", { class: "cond-field-label", text: label }),
      el("div", { class: "cond-field-body" }, control),
      extra || null
    );
  }

  /* ---------------------------------------------------------------- 设置区 */

  const SETTING_SECTIONS = [
    {
      key: "base",
      title: "基础",
      icon: "◈",
      sub: "这条规则管哪些群",
      rows: [
        {
          key: "群号",
          type: "groups",
          label: "生效群号",
          hint: "一行一个群号；* 表示所有群；留空 = 先存着，这条规则不生效",
          hideInFallback: true,
        },
      ],
    },
    {
      key: "time",
      title: "时间控制",
      icon: "◷",
      sub: "活跃窗口与冷却，-1 表示继承兜底规则",
      fallbackSub: "活跃窗口与冷却；兜底规则没有更下层可继承，没有 -1",
      rows: [
        {
          key: "持续活跃",
          type: "float",
          unit: "秒",
          slider: { min: -1, max: 600, step: 5, inheritText: "秒 · 继承兜底" },
          label: "持续活跃",
          hint: "唤醒后保持活跃的时长，活跃期内无需再艾特；-1 = 继承兜底",
          fallbackHint: "唤醒后保持活跃的时长，活跃期内无需再艾特；0 = 不开启持续活跃",
        },
        {
          key: "活跃间隔",
          type: "float",
          unit: "秒",
          slider: { min: -1, max: 300, step: 1, inheritText: "秒 · 继承兜底" },
          label: "活跃间隔",
          hint: "活跃期内两次唤醒的最小间隔；-1 = 继承兜底",
          fallbackHint: "活跃期内两次唤醒的最小间隔；0 = 不限制",
        },
        {
          key: "唤醒冷却",
          type: "float",
          unit: "秒",
          slider: { min: -1, max: 600, step: 5, inheritText: "秒 · 继承兜底" },
          label: "唤醒冷却",
          hint: "同一用户唤醒后的冷却时间；-1 = 继承兜底",
          fallbackHint: "同一用户唤醒后的冷却时间；0 = 不冷却",
        },
        {
          key: "活跃提示词",
          type: "prompt",
          tempKey: "活跃提示词临时",
          restoreKey: "主动唤醒提示词",
          label: "持续活跃提示词",
          placeholder: "留空 = 不注入",
          hint: "活跃期内被唤醒时注入；留空 = 不注入（和条件提示词一致）",
        },
        { key: "活跃方式", type: "select", optionsKey: "活跃方式", label: "活跃方式", hint: "从哪一刻开始计时" },
        {
          key: "活跃范围",
          type: "select",
          optionsKey: "活跃范围",
          label: "活跃范围",
          hint: "群聊级：一人唤醒全群活跃；用户级：各自独立",
        },
      ],
    },
    {
      key: "cmd",
      title: "指令",
      icon: "⌘",
      sub: "指令黑白名单与唤醒前缀",
      rows: [
        {
          key: "指令黑白名单",
          type: "lines",
          acl: true,
          label: "指令黑白名单",
          hint: "* 放行全部；/help 禁止；help 允许。只对真正的指令生效",
          placeholder: "一行一条，例如：\n*\n/help",
          rows: 4,
        },
        {
          key: "禁用系统指令",
          type: "switch",
          label: "禁用系统指令",
          hint: "禁止 help、reset、plugin 等内置指令",
        },
        {
          key: "禁前唤醒",
          type: "switch",
          label: "禁止前缀唤醒",
          hint: "输入不存在的指令（如 /asdf）时不进 LLM",
        },
        {
          key: "指令不唤醒",
          type: "switch",
          label: "指令不唤醒",
          hint: "指令已处理但没发消息时，不再继续走 LLM",
        },
      ],
    },
    {
      key: "misc",
      title: "其他",
      icon: "✦",
      sub: "用户黑白名单与管理员豁免",
      rows: [
        {
          key: "用户黑白名单",
          type: "lines",
          acl: true,
          label: "用户黑白名单",
          hint: "唯一特殊值 *；/123456 禁止该用户",
          placeholder: "一行一条，例如：\n*\n/123456",
          rows: 4,
        },
        {
          key: "管理员豁免",
          type: "switch",
          label: "管理员豁免",
          hint: "管理员不受名单、冷却、屏蔽条件限制",
        },
      ],
    },
  ];

  function renderSettingsSection(section, rows, rule) {
    const srows = el("div", { class: "srows" });
    for (const row of rows) srows.append(renderSettingRow(row, rule));
    const fallback = isFallback(rule);

    return el(
      "section",
      { class: "panel" },
      el(
        "div",
        { class: "panel-head" },
        el("span", { class: "panel-icon", text: section.icon }),
        el(
          "div",
          {},
          el("div", { class: "panel-title", text: section.title }),
          el("div", {
            class: "panel-sub",
            text: (fallback && section.fallbackSub) || section.sub,
          })
        )
      ),
      el("div", { class: "panel-body" }, srows)
    );
  }

  function renderSettingRow(row, rule) {
    const fallback = isFallback(rule);
    let control;
    switch (row.type) {
      case "switch":
        control = switchControl(rule, row.key);
        break;
      case "int":
        control = numberControl(rule, row.key, { step: 1 });
        break;
      case "float":
        control = row.slider
          ? sliderControl({
              value: rule[row.key],
              // 兜底规则没有更下层可继承，滑块下限拉到 0，也不显示「继承兜底」
              min: fallback ? Math.max(0, row.slider.min) : row.slider.min,
              max: row.slider.max,
              step: row.slider.step || 1,
              unit: row.unit || "",
              inheritText: fallback ? "" : row.slider.inheritText || "",
              onInput: (v) => {
                rule[row.key] = Math.round(v * 10) / 10;
                markDirty();
              },
            })
          : numberControl(rule, row.key, { step: 0.5, unit: row.unit });
        break;
      case "select":
        control = selectControl(rule, row.key, opt(row.optionsKey || row.key));
        break;
      case "groups":
        control = groupsControl(rule, row);
        break;
      case "lines":
        control = linesRow(rule, row);
        break;
      case "prompt":
        control = promptEditor({
          value: rule[row.key],
          temp: rule[row.tempKey] !== false,
          hint: "",
          placeholder: row.placeholder,
          onInput: (v) => {
            rule[row.key] = v;
            markDirty();
          },
          onTemp: (v) => {
            rule[row.tempKey] = v;
            markDirty();
          },
          onRestore: (area) => {
            const text = String(((S.data.全局 || {})[row.restoreKey] || "")).trim();
            if (!text) {
              toast("全局设置里这段提示词是空的，先去全局设置里填写", "error");
              return;
            }
            rule[row.key] = text;
            area.value = text;
            markDirty();
          },
        });
        break;
      case "textarea":
        control = el("textarea", {
          class: "textarea",
          placeholder: row.placeholder || "",
          value: rule[row.key] || "",
          oninput: (e) => {
            rule[row.key] = e.target.value;
            markDirty();
          },
        });
        break;
      default:
        control = el("span", { text: String(rule[row.key] ?? "") });
    }

    return el(
      "div",
      { class: "srow" },
      el(
        "div",
        { class: "srow-label" },
        el("b", { text: row.label }),
        row.hint ? el("span", { text: (fallback && row.fallbackHint) || row.hint }) : null
      ),
      el("div", { class: `srow-control ${row.type === "switch" ? "end" : ""}` }, control)
    );
  }

  function switchControl(rule, key) {
    const input = el("input", {
      type: "checkbox",
      onchange: (e) => {
        rule[key] = e.target.checked;
        markDirty();
      },
    });
    input.checked = !!rule[key];
    return el(
      "label",
      { class: "switch" },
      input,
      el("span", { class: "switch-track" }),
      el("span", { class: "switch-text", text: "开启" })
    );
  }

  /**
   * 数字 + 滑块联动：滑块负责拖拽，输入框负责精确值。
   * 输入框里填了超出滑块上限的数字时，滑块上限会自动跟着放大，两者不会打架。
   */
  function sliderControl({
    value,
    min = 0,
    max = 100,
    step = 1,
    unit = "",
    inheritText = "",
    onInput,
  }) {
    let current = Number(value);
    if (!Number.isFinite(current)) current = min;

    const top = () => Math.max(max, current);
    const num = el("input", {
      class: "input num",
      type: "number",
      step: String(step),
      value: String(current),
    });
    const range = el("input", { type: "range", step: String(step) });
    const tail = el("span", { class: "unit" });

    const paintTail = () => {
      tail.textContent = inheritText && current < 0 ? inheritText : unit;
    };
    const refresh = () => {
      range.min = String(min);
      range.max = String(top());
      range.value = String(Math.min(Math.max(current, min), top()));
      num.value = String(current);
      paintTail();
    };

    range.addEventListener("input", (e) => {
      current = Number(e.target.value);
      num.value = String(current);
      paintTail();
      if (onInput) onInput(current);
    });

    num.addEventListener("input", (e) => {
      const raw = e.target.value;
      if (raw === "" || raw === "-") return;
      const n = Number(raw);
      if (!Number.isFinite(n)) return;
      current = n;
      range.max = String(top());
      range.value = String(Math.min(Math.max(n, min), top()));
      paintTail();
      if (onInput) onInput(n);
    });

    num.addEventListener("blur", () => {
      if (num.value === "" || num.value === "-") {
        current = min;
        if (onInput) onInput(current);
      }
      refresh();
    });

    refresh();
    return el("div", { class: "slider-row" }, range, num, unit || inheritText ? tail : null);
  }

  function numberControl(rule, key, { step = 0.5, unit } = {}) {
    const input = el("input", {
      class: "input num",
      type: "number",
      step: String(step),
      value: rule[key] ?? 0,
      oninput: (e) => {
        const raw = e.target.value;
        if (raw === "" || raw === "-") return;
        const n = Number(raw);
        if (!Number.isFinite(n)) return;
        rule[key] = step === 1 ? Math.round(n) : n;
        markDirty();
      },
      onblur: (e) => {
        if (e.target.value === "" || e.target.value === "-") e.target.value = rule[key] ?? 0;
      },
    });
    return unit
      ? el("div", { class: "inline-unit" }, input, el("span", { class: "unit", text: unit }))
      : input;
  }

  function selectControl(rule, key, options) {
    const list = [...options];
    if (!list.includes(rule[key])) list.unshift(rule[key]);
    return dropdown({
      value: rule[key],
      options: list.filter((v) => v !== undefined && v !== null),
      minWidth: 168,
      onChange: (v) => {
        rule[key] = v;
        markDirty();
      },
    });
  }

  function linesRow(rule, row) {
    if (!Array.isArray(rule[row.key])) rule[row.key] = [];
    const values = rule[row.key];
    const tools = el(
      "div",
      { class: "lines-tools" },
      linesControl({
        values,
        placeholder: row.placeholder,
        rows: row.rows || 3,
        mono: !row.acl,
        validate: row.acl ? null : null,
      })
    );
    if (row.acl) {
      tools.append(
        el("button", {
          class: "btn small ghost acl-star",
          type: "button",
          text: "插入 *",
          title: "插入通配符（放行全部）",
          onclick: () => {
            if (!values.includes("*")) values.unshift("*");
            markDirty();
            renderEditor();
          },
        })
      );
    }
    return tools;
  }

  function groupsControl(rule, row) {
    if (!Array.isArray(rule.群号)) rule.群号 = [];
    const values = rule.群号;
    return el(
      "div",
      { class: "lines-tools" },
      linesControl({
        values,
        placeholder: "一行一个群号，例如：\n123456789\n987654321",
        rows: 3,
        mono: true,
      }),
      el("button", {
        class: "btn small ghost acl-star",
        type: "button",
        text: "从群列表选择",
        onclick: () =>
          openGroupPicker((gid) => {
            if (!values.includes(gid)) values.push(gid);
            markDirty();
            renderEditor();
          }),
      })
    );
  }

  /* ---------------------------------------------------------------- 通用控件 */

  function validateRegex(pattern) {
    if (!pattern || pattern.trim().startsWith("#")) return "";
    try {
      new RegExp(pattern);
      return "";
    } catch (e) {
      return `正则不合法：${e.message}`;
    }
  }

  function multiControl(values, options, onChange) {
    const box = el("div", { class: "chips" });
    for (const option of options) {
      const on = values.includes(option);
      box.append(
        el("button", {
          class: `chip ${on ? "on" : ""}`,
          type: "button",
          text: option,
          onclick: (e) => {
            const index = values.indexOf(option);
            if (index >= 0) values.splice(index, 1);
            else values.push(option);
            e.target.classList.toggle("on", index < 0);
            onChange(values);
          },
        })
      );
    }
    return box;
  }

  /** 一行一条的列表编辑器：回车即下一行，不再把多条挤在一行。 */
  function linesControl({ values, placeholder, rows = 3, mono = true, validate }) {
    const area = el("textarea", {
      class: `lines ${mono ? "mono" : ""}`,
      rows: String(rows),
      spellcheck: "false",
      placeholder: placeholder || "一行一条",
    });
    area.value = values.join("\n");

    const count = el("span", { class: "lines-count" });
    const err = el("span", { class: "lines-err" });

    const refresh = () => {
      count.textContent = `${values.length} 条`;
      if (!validate) {
        err.textContent = "";
        err.classList.remove("show");
        return;
      }
      for (const v of values) {
        const msg = validate(v);
        if (msg) {
          err.textContent = `⚠ ${msg}`;
          err.classList.add("show");
          return;
        }
      }
      err.textContent = "";
      err.classList.remove("show");
    };

    const sync = () => {
      const seen = [];
      for (const raw of area.value.split(/\r?\n/)) {
        const v = raw.trim();
        if (v && !seen.includes(v)) seen.push(v);
      }
      values.length = 0;
      for (const v of seen) values.push(v);
      markDirty();
      refresh();
    };

    area.addEventListener("input", sync);
    refresh();

    return el("div", { class: "lines-wrap" }, area, el("div", { class: "lines-foot" }, count, err));
  }

  /** 提示词编辑器：文本 + 「临时」开关 + 恢复默认。 */
  // 临时开关旁边那行说明：讲的是开关本身的作用
  const TEMP_HINT = "临时：开 = 只对本次请求生效（不写入历史）；关 = 持久写入上下文";

  function promptEditor({ value, temp, hint, placeholder, onInput, onTemp, onRestore }) {
    const area = el("textarea", {
      class: "textarea prompt-area",
      rows: 3,
      placeholder: placeholder || "留空 = 不注入提示词",
      value: value || "",
      oninput: (e) => onInput(e.target.value),
    });

    const sw = el("input", {
      type: "checkbox",
      onchange: (e) => onTemp(e.target.checked),
    });
    sw.checked = temp !== false;

    return el(
      "div",
      { class: "prompt-wrap" },
      area,
      el(
        "div",
        { class: "prompt-foot" },
        el(
          "label",
          {
            class: "switch",
            title: "开启：只对本次请求生效，不写入对话历史；关闭：持久写入上下文",
          },
          sw,
          el("span", { class: "switch-track" }),
          el("span", { class: "switch-text", text: "临时" })
        ),
        el("span", { class: "prompt-hint", text: hint || TEMP_HINT }),
        el("button", {
          class: "btn tiny ghost",
          type: "button",
          text: "恢复默认",
          onclick: () => onRestore(area),
        })
      )
    );
  }

  /** 自绘下拉：不用原生 select，样式与主题一致。 */
  function dropdown({ value, options, onChange, minWidth }) {
    // 选中后必须更新这份「当前值」，否则再次打开菜单时高亮的还是初始那个
    let current = value;
    const wrap = el("div", { class: "dd" });
    const label = el("span", { class: "dd-label", text: String(current ?? "") });
    const btn = el("button", {
      class: "dd-btn",
      type: "button",
      "aria-haspopup": "listbox",
    });
    btn.append(label, el("span", { class: "dd-caret", text: "▾" }));

    const menu = el("div", { class: "dd-menu", role: "listbox" });

    // 选完之后按钮上的文字和高亮都要跟着变，否则界面看着还是旧值
    const setLabel = (next) => {
      current = next;
      label.textContent = String(next ?? "");
      for (const item of menu.querySelectorAll(".dd-item")) {
        item.classList.toggle("on", item.textContent === String(next));
      }
    };

    const close = () => {
      if (S.openDD && S.openDD.menu === menu) S.openDD = null;
      menu.remove();
      wrap.classList.remove("open");
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };

    const onDown = (e) => {
      if (!wrap.contains(e.target) && !menu.contains(e.target)) close();
    };

    const place = () => {
      const rect = btn.getBoundingClientRect();
      const width = Math.max(minWidth || 0, rect.width);
      menu.style.minWidth = `${Math.round(width)}px`;
      const height = menu.offsetHeight;
      const below = rect.bottom + 6;
      const top =
        below + height > window.innerHeight - 8 ? Math.max(8, rect.top - height - 6) : below;
      menu.style.top = `${Math.round(top)}px`;
      menu.style.left = `${Math.round(Math.max(8, Math.min(rect.left, window.innerWidth - width - 12)))}px`;
    };

    const open = () => {
      if (S.openDD) S.openDD.close();
      menu.replaceChildren();
      for (const option of options) {
        menu.append(
          el("button", {
            class: `dd-item ${option === current ? "on" : ""}`,
            type: "button",
            text: option,
            onclick: () => {
              setLabel(option);
              onChange(option);
              close();
            },
          })
        );
      }
      document.body.append(menu);
      wrap.classList.add("open");
      menu.style.visibility = "hidden";
      place();
      menu.style.visibility = "";
      S.openDD = { close, menu };
      document.addEventListener("pointerdown", onDown, true);
      window.addEventListener("scroll", close, true);
      window.addEventListener("resize", close);
    };

    btn.addEventListener("click", () => (menu.isConnected ? close() : open()));
    wrap.append(btn);
    return wrap;
  }

  function closeDropdown() {
    if (S.openDD) S.openDD.close();
  }

  /* ---------------------------------------------------------------- 底部操作条 */

  function renderSaveBar(rule, fallback) {
    return el(
      "div",
      { class: "savebar" },
      el("span", { id: "saveHint", class: "hint", text: "修改后记得保存，保存即生效" }),
      el("button", {
        class: "btn ghost",
        type: "button",
        text: "还原修改",
        onclick: () => select(S.selected, { force: true }),
      }),
      fallback
        ? null
        : el("button", {
            class: "btn danger",
            type: "button",
            text: "删除规则",
            onclick: () => removeRule(rule),
          }),
      el("button", { class: "btn primary", type: "button", text: "保存", onclick: () => save() })
    );
  }

  async function save() {
    if (!S.draft || S.saving) return;
    // 备注必填（规则靠它区分）；群号可以留空——留空的规则存着但哪儿都不生效
    if (!isFallback(S.draft) && !String(S.draft.备注 || "").trim()) {
      toast("请先填写备注：备注用来区分每条规则", "error");
      const title = $(".hero-title");
      if (title) title.focus();
      return;
    }
    const 无群号 = !isFallback(S.draft) && !(S.draft.群号 || []).length;
    S.saving = true;
    setBadge("保存中…");
    try {
      if (isFallback(S.draft)) await apiPost("api/settings/save", { 兜底规则: S.draft });
      else await apiPost("api/rule/save", { 规则: S.draft });
      S.dirty = false;
      await load({ keepSelection: true });
      toast(无群号 ? "已保存：还没填群号，这条规则暂时不会生效" : "已保存", "ok");
    } catch (e) {
      setBadge("保存失败", "dirty");
      toast(`保存失败：${e.message || e}`, "error");
    } finally {
      S.saving = false;
    }
  }

  async function removeRule(rule) {
    const ok = await confirmDialog(`确定删除规则「${rule.备注 || rule.id}」吗？该操作不可撤销。`, {
      title: "删除规则",
      okText: "删除",
      danger: true,
    });
    if (!ok) return;
    try {
      await apiPost("api/rule/delete", { id: rule.id });
      S.dirty = false;
      S.selected = "";
      await load();
      toast("已删除", "ok");
    } catch (e) {
      toast(`删除失败：${e.message || e}`, "error");
    }
  }

  /* ---------------------------------------------------------------- 弹层 */

  function openModal(content, opts = {}) {
    closeDropdown();
    const modal = $("#modal");
    const card = el("div", { class: `modal-card ${opts.narrow ? "narrow" : ""}` });
    const nodes =
      content instanceof Element && !content.className && content.childNodes.length
        ? Array.from(content.childNodes)
        : [content];
    card.append(...nodes);
    modal.replaceChildren(card);
    modal.classList.remove("hidden");
    modal.onclick = (e) => {
      if (e.target === modal) closeModal();
    };
    document.addEventListener("keydown", escClose);
  }

  function escClose(e) {
    if (e.key === "Escape") {
      if (S.openDD) {
        closeDropdown();
        return;
      }
      closeModal();
    }
  }

  function closeModal() {
    closeDropdown();
    const modal = $("#modal");
    modal.classList.add("hidden");
    modal.replaceChildren();
    document.removeEventListener("keydown", escClose);
    // 确认框被 Esc / 点背景关掉时按「取消」处理，避免 Promise 一直挂着
    if (S.pendingConfirm) {
      const cancel = S.pendingConfirm;
      S.pendingConfirm = null;
      cancel();
    }
  }

  /* 面板自己的确认框：插件页跑在 iframe 里，window.confirm 可能被浏览器静默拦掉 */
  function confirmDialog(message, opts = {}) {
    return new Promise((resolve) => {
      const answer = (ok) => {
        S.pendingConfirm = null;
        closeModal();
        resolve(ok);
      };
      S.pendingConfirm = () => resolve(false);
      openModal(
        modalShell(
          opts.title || "请确认",
          el("div", { class: "modal-body" }, el("p", { class: "confirm-text", text: message })),
          el(
            "div",
            { class: "modal-foot" },
            el("button", {
              class: "btn ghost",
              type: "button",
              text: "取消",
              onclick: () => answer(false),
            }),
            el("button", {
              class: `btn ${opts.danger ? "danger" : "primary"}`,
              type: "button",
              text: opts.okText || "确定",
              onclick: () => answer(true),
            })
          )
        ),
        { narrow: true }
      );
    });
  }

  function modalShell(title, body, footer) {
    return el(
      "div",
      {},
      el(
        "div",
        { class: "modal-head" },
        el("h3", { text: title }),
        el("button", { class: "btn icon ghost", type: "button", text: "×", onclick: closeModal })
      ),
      body,
      footer || null
    );
  }

  function openTypePicker(rule, stage = "recv") {
    const 阶段 = STAGES[stage];
    // LLM 前流水线只做屏蔽：同一类型在这里归到「屏蔽」组
    const types = stageTypes(stage).map((t) => ({
      ...t,
      动作: stage === "llm" ? "屏蔽" : t.动作,
    }));
    const groups = [
      { 动作: "唤醒", 类型: types.filter((t) => t.动作 === "唤醒") },
      { 动作: "屏蔽", 类型: types.filter((t) => t.动作 === "屏蔽") },
    ].filter((g) => g.类型.length);

    const body = el("div", { class: "modal-body" });
    for (const group of groups) {
      const grid = el("div", { class: "type-grid" });
      for (const meta of group.类型) {
        grid.append(
          el(
            "button",
            {
              class: `type-card ${group.动作 === "屏蔽" ? "block" : ""}`,
              type: "button",
              onclick: () => {
                const list = rule[阶段.key] || (rule[阶段.key] = []);
                list.push(newCondition(meta.类型));
                markDirty();
                closeModal();
                renderEditor();
                toast(`已添加到${阶段.picker}，默认排在最后`, "ok");
              },
            },
            el("b", { text: meta.类型 }),
            el("span", { text: meta.说明 })
          )
        );
      }
      body.append(el("div", { class: "type-group" }, el("h4", { text: group.动作 }), grid));
    }

    openModal(modalShell(`添加条件 · ${阶段.picker}`, body));
  }

  function newCondition(type) {
    const suffix = Math.random().toString(36).slice(2, 12);
    return {
      id: `c${suffix}`,
      类型: type,
      开关: true,
      提示词: "",
      正则: [],
      概率: 0,
      概率方式: ["纯文本"],
      概率冷却: 0,
    };
  }

  function openGroupPicker(onPick) {
    const groups = (S.data.群目录 || []).slice();
    const listBox = el("div", { class: "group-picker" });
    const search = el("input", {
      class: "input",
      placeholder: "搜索群号或群名",
      oninput: (e) => render(e.target.value.trim()),
    });

    const render = (keyword) => {
      const filtered = keyword
        ? groups.filter((g) => g.群号.includes(keyword) || (g.名称 || "").includes(keyword))
        : groups;
      listBox.replaceChildren();
      if (!filtered.length) {
        listBox.append(
          el("div", {
            class: "rule-sub",
            text: keyword
              ? "没有匹配的群"
              : "还没有记录到群信息。机器人收到群消息后会自动记录，也可以直接手填群号。",
          })
        );
        return;
      }
      for (const g of filtered) {
        listBox.append(
          el(
            "button",
            {
              class: "group-row",
              type: "button",
              onclick: () => {
                onPick(g.群号);
                closeModal();
              },
            },
            el(
              "div",
              {},
              el("div", { class: "gid", text: g.群号 }),
              el("div", { class: "gname", text: g.名称 || "（无群名）" })
            ),
            el("span", { class: "pill", text: g.时间 ? timeAgo(g.时间) : "" })
          )
        );
      }
    };

    render("");
    openModal(modalShell("选择群聊", el("div", { class: "modal-body" }, search, listBox)));
    setTimeout(() => search.focus(), 30);
  }

  function timeAgo(timestamp) {
    const diff = Date.now() / 1000 - timestamp;
    if (diff < 90) return "刚刚";
    if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
    if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
    return `${Math.round(diff / 86400)} 天前`;
  }

  function openRegexTester(条件) {
    const meta = typeMeta(条件.类型);
    const patterns = 条件.正则 || [];
    const textarea = el("textarea", {
      class: "textarea",
      rows: 4,
      placeholder: "粘贴一条消息，例如：你好 @猫猫(123456) 在吗",
    });
    const result = el("div", { class: "result-box", text: "输入文本后点击「测试」" });

    const run = async () => {
      result.className = "result-box";
      result.textContent = "测试中…";
      try {
        const isWake = meta.动作 === "唤醒";
        const data = await apiPost("api/test", {
          文本: textarea.value,
          唤醒正则: isWake ? patterns : [],
          屏蔽正则: isWake ? [] : patterns,
        });
        const key = isWake ? "唤醒正则" : "屏蔽正则";
        const hit = data[key] || {};
        result.textContent = hit.命中
          ? `命中 → ${hit.模式}\n片段：「${hit.片段}」`
          : `未命中（共 ${patterns.length} 条正则）`;
        result.className = `result-box ${hit.命中 ? "hit" : ""}`;
      } catch (e) {
        result.className = "result-box";
        result.textContent = `测试失败：${e.message || e}`;
      }
    };

    openModal(
      modalShell(
        `${条件.类型} · 正则试匹配`,
        el(
          "div",
          { class: "modal-body" },
          el("div", {
            class: "panel-sub",
            text: "匹配对象是「完整消息文本」：@某人(QQ) 会以 name(qq) 形式保留，@机器人 记为 (myself)。",
          }),
          textarea,
          result
        ),
        el(
          "div",
          { class: "modal-foot" },
          el("button", { class: "btn ghost", type: "button", text: "关闭", onclick: closeModal }),
          el("button", { class: "btn primary", type: "button", text: "测试", onclick: run })
        )
      ),
      { narrow: true }
    );
    setTimeout(() => textarea.focus(), 30);
  }

  function openSettings() {
    const settings = clone(S.data.全局 || {});
    const grid = el("div", { class: "srows" });

    const row = (label, control, hint) =>
      el(
        "div",
        { class: "srow" },
        el(
          "div",
          { class: "srow-label" },
          el("b", { text: label }),
          hint ? el("span", { text: hint }) : null
        ),
        el("div", { class: "srow-control" }, control)
      );

    const lines = (key, placeholder, hint, rows = 3) => {
      if (!Array.isArray(settings[key])) settings[key] = [];
      return row(
        key,
        linesControl({ values: settings[key], placeholder, rows, mono: true }),
        hint
      );
    };

    grid.append(
      lines("黑名单群聊", "一行一个群号", "这些群的消息会被直接终止，管理员也受影响"),
      lines("额外指令", "一行一个指令名", "框架没扫描到的指令，补充给指令黑白名单使用"),
      row(
        "提示词注入",
        switchControl(settings, "提示词注入"),
        "总开关：关掉后条件与活跃两类提示词都不再注入"
      )
    );

    grid.append(
      el(
        "div",
        { class: "srow" },
        el(
          "div",
          { class: "srow-label" },
          el("b", { text: "提示词模板" }),
          el("span", {
            text: "下面 3 条只给规则里的「恢复默认」取用；规则留空就是不注入",
          })
        )
      )
    );

    for (const [key, hint] of [
      ["主动唤醒提示词", "持续活跃 / 概率唤醒条件的「恢复默认」取这段"],
      ["提及唤醒提示词", "艾特 / 唤醒正则条件的「恢复默认」取这段"],
      ["引用唤醒提示词", "引用类条件的「恢复默认」取这段"],
    ]) {
      const tempKey = `${key}临时`;
      if (settings[tempKey] === undefined) settings[tempKey] = true;
      grid.append(
        row(
          key,
          promptEditor({
            value: settings[key],
            temp: settings[tempKey] !== false,
            hint: "",
            onInput: (v) => {
              settings[key] = v;
            },
            onTemp: (v) => {
              settings[tempKey] = v;
            },
            onRestore: (area) => {
              const builtin = (S.data.默认提示词 || {})[BUILTIN_PROMPT_KEY[key]] || "";
              if (!builtin) {
                toast("没有内置默认文案", "error");
                return;
              }
              settings[key] = builtin;
              area.value = builtin;
            },
          }),
          hint
        )
      );
    }

    openModal(
      modalShell(
        "全局设置",
        el("div", { class: "modal-body" }, grid),
        el(
          "div",
          { class: "modal-foot" },
          el("button", { class: "btn ghost", type: "button", text: "取消", onclick: closeModal }),
          el("button", {
            class: "btn primary",
            type: "button",
            text: "保存设置",
            onclick: async (e) => {
              e.target.disabled = true;
              try {
                await apiPost("api/settings/save", { 全局: settings });
                closeModal();
                await load({ keepSelection: true });
                toast("全局设置已保存", "ok");
              } catch (err) {
                toast(`保存失败：${err.message || err}`, "error");
              } finally {
                e.target.disabled = false;
              }
            },
          })
        )
      )
    );
  }

  function openNewRule() {
    const groups = [];
    let 备注 = "";

    const groupEditor = linesControl({
      values: groups,
      placeholder: "一行一个群号，例如：\n123456789（可以先留空）",
      rows: 3,
      mono: true,
    });
    const nameInput = el("input", {
      class: "input",
      placeholder: "必填，例如：主群 / 测试群 / 备用模板",
      oninput: (e) => {
        备注 = e.target.value;
      },
    });

    const submit = async (e) => {
      const 名称 = 备注.trim();
      // 备注必填（规则靠它区分）；群号允许留空——空群号的规则存着但不生效
      if (!名称) {
        toast("请填写备注：备注用来区分每条规则", "error");
        nameInput.focus();
        return;
      }
      e.target.disabled = true;
      try {
        const data = await apiPost("api/rule/new", { 群号: groups, 备注: 名称 });
        closeModal();
        await load();
        if (data && data.规则) select(data.规则.id, { force: true });
        toast(groups.length ? "已创建规则" : "已创建规则（还没填群号，暂时不会生效）", "ok");
      } catch (err) {
        toast(`创建失败：${err.message || err}`, "error");
      } finally {
        e.target.disabled = false;
      }
    };

    openModal(
      modalShell(
        "新建规则",
        el(
          "div",
          { class: "modal-body" },
          el("div", { class: "modal-field-label" }, el("b", { text: "备注" }), el("span", { text: "必填" })),
          nameInput,
          el(
            "div",
            { class: "modal-field-label" },
            el("b", { text: "群号" }),
            el("span", { text: "一行一个；* 表示所有群；可以留空" })
          ),
          groupEditor,
          el("div", {
            class: "panel-sub",
            text:
              "群号留空的规则会照样保存，但不会在任何群生效，下次填上就能用。" +
              "新规则追加到列表末尾；规则顺序就是优先级，可在左侧用 ▲▼ 调整。",
          })
        ),
        el(
          "div",
          { class: "modal-foot" },
          el("button", { class: "btn ghost", type: "button", text: "取消", onclick: closeModal }),
          el("button", { class: "btn primary", type: "button", text: "创建", onclick: submit })
        )
      ),
      { narrow: true }
    );
    setTimeout(() => nameInput.focus(), 30);
  }

  /* ---------------------------------------------------------------- 启动 */

  function bindChrome() {
    $("#btnSave").addEventListener("click", save);
    $("#btnReload").addEventListener("click", async () => {
      if (S.dirty) {
        const ok = await confirmDialog("有未保存的修改，重新载入会丢弃它们。确定继续吗？", {
          title: "有未保存的修改",
          okText: "丢弃并重载",
          danger: true,
        });
        if (!ok) return;
      }
      S.dirty = false;
      await load({ keepSelection: true });
      toast("已重新载入", "ok");
    });
    $("#btnNew").addEventListener("click", openNewRule);
    $("#btnSettings").addEventListener("click", openSettings);

    $("#btnRefreshCmds").addEventListener("click", async () => {
      try {
        const data = await apiPost("api/commands/refresh", {});
        await load({ keepSelection: true });
        toast(`指令表已刷新，共 ${(data.指令 || []).length} 个`, "ok");
      } catch (e) {
        toast(`刷新失败：${e.message || e}`, "error");
      }
    });

    $("#btnClearState").addEventListener("click", async () => {
      const ok = await confirmDialog("清空活跃窗口与冷却状态？不影响规则配置。", {
        title: "清空运行时状态",
        okText: "清空",
        danger: true,
      });
      if (!ok) return;
      try {
        await apiPost("api/state/clear", {});
        await load({ keepSelection: true });
        toast("运行时状态已清空", "ok");
      } catch (e) {
        toast(`操作失败：${e.message || e}`, "error");
      }
    });

    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        save();
      }
    });

    window.addEventListener("beforeunload", (e) => {
      if (!S.dirty) return;
      e.preventDefault();
      e.returnValue = "";
    });
  }

  async function init() {
    if (S.inited) return;
    S.inited = true;
    bindChrome();

    const bridge = await waitForBridge();
    if (bridge && typeof bridge.onContext === "function") {
      bridge.onContext(() => {
        document.documentElement.setAttribute("data-theme", "light");
      });
    }
    if (bridge && typeof bridge.ready === "function") {
      try {
        await bridge.ready();
      } catch (e) {
        /* 只用于上下文，失败可忽略 */
      }
    }
    document.documentElement.setAttribute("data-theme", "light");
    await load();
  }

  document.addEventListener("DOMContentLoaded", init);
  if (document.readyState !== "loading") init();
})();
