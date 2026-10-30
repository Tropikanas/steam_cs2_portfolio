"use strict";

const SF = (() => {
  // ---------- форматирование ----------

  const SIGN = { RUB: "₽", USD: "$", CNY: "¥", UAH: "₴" };
  const TRASH_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
  const FX_SOURCE = { steam: "курс Steam", cbr: "курс ЦБ на дату", usd: "", manual: "курс задан вручную" };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const num = (v, d = 2) => v == null ? "—" : Number(v).toLocaleString("ru-RU", { minimumFractionDigits: d, maximumFractionDigits: d });
  // Средняя цена может быть дробнее копейки — показываем до 4 знаков, но не меньше 2.
  const numFlex = (v) => v == null ? "—" : Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: Math.abs(v) < 1 ? 4 : 2 });
  const money = (v, cur, flex = false) => {
    if (v == null) return "—";
    const n = flex ? numFlex(Math.abs(v)) : num(Math.abs(v));
    const minus = v < 0 ? "−" : "";
    return cur === "USD" ? minus + "$" + n : minus + n + " " + (SIGN[cur] || cur);
  };
  const pct = (v) => v == null ? '<span class="muted">—</span>'
    : `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${num(v, 1)}%</span>`;
  const date = (iso) => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : "—";
  const dateTime = (ts) => ts ? new Date(ts).toLocaleString("ru-RU", {
    timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  }) + " МСК" : "—";
  const icon = (url) => !url ? '<div class="noicon"></div>'
    : `<img loading="lazy" alt="" src="${esc(url.includes("/economy/image/") ? url + "/64fx64f" : url)}">`;
  const parseNum = (s) => {
    const v = parseFloat(String(s ?? "").replace(/\s/g, "").replace(",", "."));
    return Number.isFinite(v) ? v : null;
  };
  const store = {
    get(k, d) { try { return localStorage.getItem("sf." + k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem("sf." + k, v); } catch { /* приватный режим */ } },
  };

  // ---------- сеть и уведомления ----------

  async function api(method, url, body) {
    const res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) { location.href = "/login"; throw new Error("нужно войти"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || `Ошибка ${res.status}`);
      err.violations = data.violations || [];
      throw err;
    }
    return data;
  }

  function toast(text, bad = false) {
    const el = document.createElement("div");
    el.className = "toast" + (bad ? " bad" : "");
    el.textContent = text;
    document.getElementById("toasts").append(el);
    setTimeout(() => el.remove(), bad ? 7000 : 3500);
  }

  // ---------- таблица с сортировкой и фильтрами ----------

  // Фильтр числовой колонки: «>10», «<5», «10..20», «=3»; иначе — поиск подстроки.
  // Фильтр-список (filter: "list"): выбранные значения; строка подходит, если у неё есть хотя бы одно из них.
  function matches(filter, col, row) {
    if (Array.isArray(filter)) return !filter.length || col.values(row).some((v) => filter.includes(v));
    const f = filter.trim();
    if (!f) return true;
    const value = col.value ? col.value(row) : row[col.key];
    if (col.numeric && value != null) {
      let m;
      if ((m = f.match(/^([<>]=?|=)\s*(-?[\d\s.,]+)$/))) {
        const x = parseNum(m[2]);
        if (x != null) return { "<": value < x, "<=": value <= x, ">": value > x, ">=": value >= x, "=": value === x }[m[1]];
      }
      if ((m = f.match(/^(-?[\d\s.,]+)\.\.(-?[\d\s.,]+)$/))) {
        const a = parseNum(m[1]), b = parseNum(m[2]);
        if (a != null && b != null) return value >= a && value <= b;
      }
    }
    const text = (col.text ? col.text(row) : value ?? "").toString().toLowerCase();
    return text.includes(f.toLowerCase());
  }

  const filterSet = (v) => Array.isArray(v) ? v.length > 0 : !!(v || "").trim();
  const filterText = (v) => Array.isArray(v) ? v.join(", ") : (v || "").trim();
  const listLabel = (v) => !v || !v.length ? "Все" : v.length === 1 ? v[0] : `Выбрано: ${v.length}`;

  // Выпадающий список с галочками для фильтра колонки; открыт может быть только один.
  let openList = null;
  document.addEventListener("click", (e) => {
    if (openList && !openList.contains(e.target) && !openList.owner.contains(e.target)) closeList();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openList) closeList(); });
  window.addEventListener("scroll", (e) => openList && !openList.contains(e.target) && closeList(), true);
  function closeList() {
    openList.remove();
    openList = null;
  }

  function bindListFilter(wrap, col, filters, render) {
    const btn = wrap.querySelector("button");
    // Список живёт в <body>: внутри таблицы его перекрывают закреплённые колонки.
    const list = document.createElement("div");
    list.className = "multi-list";
    list.owner = btn;
    const sync = () => {
      btn.textContent = listLabel(filters[col.id]) + " ▾";
      btn.title = filters[col.id] && filters[col.id].length ? filterText(filters[col.id]) : "";
      btn.classList.toggle("set", filterSet(filters[col.id]));
    };
    btn.onclick = () => {
      if (openList === list) { closeList(); return; }
      if (openList) closeList();
      const chosen = filters[col.id] || [];
      list.innerHTML = col.options().map((o) =>
        `<label class="toggle"><input type="checkbox" value="${esc(o)}" ${chosen.includes(o) ? "checked" : ""}> ${esc(o)}</label>`).join("")
        + '<button type="button" class="link small" data-reset>Сбросить</button>';
      list.onchange = () => {
        const picked = [...list.querySelectorAll("input:checked")].map((i) => i.value);
        if (picked.length) filters[col.id] = picked; else delete filters[col.id];
        sync();
        render();
      };
      list.querySelector("[data-reset]").onclick = () => {
        list.querySelectorAll("input").forEach((i) => { i.checked = false; });
        list.onchange();
      };
      // Таблица прокручивается по горизонтали — список позиционируется относительно окна, чтобы не обрезался.
      const r = btn.getBoundingClientRect();
      list.style.top = `${r.bottom + 2}px`;
      list.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 228))}px`;
      document.body.append(list);
      openList = list;
    };
    sync();
    return sync;
  }

  // filters — общий объект страницы: значения фильтров переживают перезагрузку данных.
  function makeTable(container, columns, { storageKey, empty, filters = {}, rowClass, rowId, extraFilter, defaultSort = null }) {
    let rows = [];
    let sort = JSON.parse(store.get(storageKey + ".sort", "null") || "null") || defaultSort;

    function render() {
      const visible = rows.filter((r) => (!extraFilter || extraFilter(r))
        && columns.every((c) => !c.filter || matches(filters[c.id] || "", c, r)));
      if (sort) {
        const col = columns.find((c) => c.id === sort.id);
        if (col) {
          const get = col.value || ((r) => r[col.key]);
          visible.sort((a, b) => {
            const x = get(a), y = get(b);
            if (x == null && y == null) return 0;
            if (x == null) return 1;
            if (y == null) return -1;
            const res = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y), "ru");
            return sort.dir * res;
          });
        }
      }
      const tbody = container.querySelector("tbody");
      tbody.innerHTML = visible.length ? visible.map((r) => `<tr class="${rowClass ? rowClass(r) : ""}" ${rowId ? `data-row="${esc(rowId(r))}"` : ""}>${columns.map((c) =>
        `<td class="${c.cls || ""}">${c.html(r)}</td>`).join("")}</tr>`).join("")
        : `<tr><td colspan="${columns.length}" class="empty-state">${rows.length ? "Ничего не найдено — измените фильтры" : empty}</td></tr>`;
      visible.forEach((r, i) => columns.forEach((c, j) => c.bind && c.bind(tbody.rows[i].cells[j], r)));
      const counter = document.getElementById("rows-count");
      if (counter) counter.textContent = rows.length ? `Строк: ${visible.length}${visible.length !== rows.length ? " из " + rows.length : ""}` : "";
      container.querySelectorAll("th[data-id]").forEach((th) => {
        th.querySelector(".arrow").textContent = sort && sort.id === th.dataset.id ? (sort.dir > 0 ? "▲" : "▼") : "";
      });
    }

    container.innerHTML = `<table><thead>
      <tr>${columns.map((c) => `<th class="${(c.cls || "").replace(/\b(num|icon|name|actions|comment)\b/g, "")} ${c.sortable !== false ? "sortable" : ""} ${c.numeric ? "num" : ""}"
        ${c.sortable !== false ? `data-id="${c.id}"` : ""}>${esc(c.title)}<span class="arrow"></span></th>`).join("")}</tr>
      <tr class="filters">${columns.map((c) => `<th class="${(c.cls || "").replace(/\b(num|icon|name|actions|comment)\b/g, "")}">${
        c.filter === "list" ? `<div class="multi" data-list="${c.id}"><button type="button" aria-label="Фильтр: ${esc(c.title)}"></button></div>`
        : c.filter ? `<input data-id="${c.id}" placeholder="${c.numeric ? "&gt;0" : "фильтр"}" aria-label="Фильтр: ${esc(c.title)}">` : ""}</th>`).join("")}</tr>
      </thead><tbody></tbody></table>`;
    container.querySelectorAll("th[data-id]").forEach((th) => th.addEventListener("click", () => {
      const id = th.dataset.id;
      sort = sort && sort.id === id ? (sort.dir > 0 ? { id, dir: -1 } : defaultSort) : { id, dir: 1 };
      store.set(storageKey + ".sort", JSON.stringify(sort));
      render();
    }));
    container.querySelectorAll("tr.filters input").forEach((input) => {
      input.value = filters[input.dataset.id] || "";
      input.addEventListener("input", () => {
        filters[input.dataset.id] = input.value;
        render();
      });
    });
    const syncLists = [...container.querySelectorAll("tr.filters .multi")].map((wrap) =>
      bindListFilter(wrap, columns.find((c) => c.id === wrap.dataset.list), filters, render));
    return {
      setRows(r) { rows = r; render(); },
      render,
      clearFilters() {
        Object.keys(filters).forEach((k) => delete filters[k]);
        container.querySelectorAll("tr.filters input").forEach((i) => { i.value = ""; });
        syncLists.forEach((s) => s());
        render();
      },
    };
  }

  // ---------- формы в диалогах ----------

  function field(f) {
    const id = "f-" + f.name;
    const attrs = `id="${id}" name="${f.name}" ${f.required !== false ? "required" : ""} ${f.attrs || ""}`;
    let input;
    if (f.type === "select") {
      input = `<select ${attrs}>${f.options.map((o) => `<option value="${esc(o.value)}" ${String(o.value) === String(f.value) ? "selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
    } else if (f.type === "textarea") {
      input = `<textarea ${attrs} rows="2">${esc(f.value ?? "")}</textarea>`;
    } else if (f.type === "item") {
      input = `<div class="suggest"><input ${attrs} autocomplete="off" value="${esc(f.value ?? "")}" placeholder="Начните вводить название"><div class="suggest-list hidden"></div></div>`;
    } else if (f.type === "static") {
      input = `<div>${f.html}</div>`;
    } else {
      input = `<input ${attrs} type="${f.type || "text"}" value="${esc(f.value ?? "")}" ${f.inputmode ? `inputmode="${f.inputmode}"` : ""}>`;
    }
    return `<label class="field ${f.hidden ? "hidden" : ""}" data-field="${f.name}"><span>${esc(f.label)}</span>${input}${f.hint ? `<small class="muted">${f.hint}</small>` : ""}</label>`;
  }

  /**
   * Диалог с формой. layout — массив полей или массивов полей (строка из двух).
   * onSubmit(values) должен вернуть promise; ошибка сервера показывается внутри диалога.
   */
  function openForm({ title, layout, submit = "Сохранить", onSubmit, onInput, info = false }) {
    const dlg = document.createElement("dialog");
    dlg.innerHTML = `<form method="dialog" novalidate>
      <h2>${esc(title)}</h2>
      ${layout.map((x) => Array.isArray(x) ? `<div class="field-row">${x.map(field).join("")}</div>` : field(x)).join("")}
      ${info ? '<div class="info" data-info></div>' : ""}
      <div class="error hidden" data-error></div>
      <div class="dialog-actions"><button type="button" data-cancel>Отмена</button><button class="primary" data-submit>${esc(submit)}</button></div>
    </form>`;
    document.body.append(dlg);
    const form = dlg.querySelector("form");
    const errorBox = dlg.querySelector("[data-error]");
    const values = () => Object.fromEntries(new FormData(form).entries());
    const ctx = { dlg, form, values, info: dlg.querySelector("[data-info]"), el: (n) => form.elements[n] };
    dlg.querySelector("[data-cancel]").onclick = () => dlg.close();
    dlg.addEventListener("close", () => dlg.remove());
    form.querySelectorAll(".suggest").forEach((s) => itemSuggest(s, () => onInput && onInput(ctx)));
    if (onInput) {
      form.addEventListener("input", () => onInput(ctx));
      form.addEventListener("change", () => onInput(ctx));
    }
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!form.reportValidity()) return;
      const btn = dlg.querySelector("[data-submit]");
      btn.disabled = true;
      errorBox.classList.add("hidden");
      try {
        await onSubmit(values(), ctx);
        dlg.close();
      } catch (err) {
        errorBox.innerHTML = esc(err.message) + (err.violations && err.violations.length
          ? `<ul>${err.violations.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : "");
        errorBox.classList.remove("hidden");
      } finally {
        btn.disabled = false;
      }
    });
    dlg.showModal();
    if (onInput) onInput(ctx);
    return ctx;
  }

  function confirmDialog(title, text, okLabel) {
    return new Promise((resolve) => {
      openForm({
        title, submit: okLabel,
        layout: [{ name: "_", type: "static", label: "", html: `<p>${esc(text)}</p>`, required: false }],
        onSubmit: async () => resolve(true),
      }).dlg.addEventListener("close", () => resolve(false));
    });
  }

  // Вопрос с несколькими ответами; null — закрыли без выбора.
  function choiceDialog(title, html, choices) {
    return new Promise((resolve) => {
      const dlg = document.createElement("dialog");
      dlg.innerHTML = `<form method="dialog"><h2>${esc(title)}</h2>${html}
        <div class="dialog-actions"><button value="">Отмена</button>${choices.map((c, i) =>
          `<button value="${i}" class="${c.primary ? "primary" : ""}">${esc(c.label)}</button>`).join("")}</div></form>`;
      document.body.append(dlg);
      dlg.addEventListener("close", () => {
        dlg.remove();
        resolve(dlg.returnValue === "" ? null : choices[+dlg.returnValue].value);
      });
      dlg.showModal();
    });
  }

  async function download(url, body, fallbackName) {
    const res = await fetch(url, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (res.status === 401) { location.href = "/login"; return; }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `Ошибка ${res.status}`);
    const name = (res.headers.get("Content-Disposition") || "").match(/filename="([^"]+)"/);
    const link = document.createElement("a");
    link.href = URL.createObjectURL(await res.blob());
    link.download = name ? name[1] : fallbackName;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }

  // Подсказки предметов с иконками.
  function itemSuggest(wrap, onPick) {
    const input = wrap.querySelector("input");
    const list = wrap.querySelector(".suggest-list");
    let items = [], active = -1, timer, seq = 0;
    const close = () => { list.classList.add("hidden"); active = -1; };
    const pick = (i) => { input.value = items[i].name; input.dataset.picked = items[i].name; close(); onPick(); };
    const draw = () => {
      list.innerHTML = items.map((it, i) => `<div class="suggest-item ${i === active ? "active" : ""}" data-i="${i}">
        ${icon(it.icon)}<div>${esc(it.name)}<span class="type">${esc(it.type)}</span></div></div>`).join("");
      list.classList.toggle("hidden", !items.length);
    };
    input.addEventListener("input", () => {
      delete input.dataset.picked;
      clearTimeout(timer);
      const q = input.value.trim();
      if (q.length < 2) { items = []; draw(); return; }
      timer = setTimeout(async () => {
        const my = ++seq;
        const found = await api("GET", "/api/items?q=" + encodeURIComponent(q)).catch(() => []);
        if (my !== seq) return;
        items = found; active = -1; draw();
      }, 200);
    });
    input.addEventListener("keydown", (e) => {
      if (list.classList.contains("hidden")) return;
      if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); draw(); e.preventDefault(); }
      else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); draw(); e.preventDefault(); }
      else if (e.key === "Enter" && active >= 0) { pick(active); e.preventDefault(); }
      else if (e.key === "Escape") { close(); e.preventDefault(); e.stopPropagation(); }
    });
    list.addEventListener("mousedown", (e) => {
      const el = e.target.closest(".suggest-item");
      if (el) { e.preventDefault(); pick(+el.dataset.i); }
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
  }

  // Строка «сумма и эквивалент в USD» + ручной курс.
  function fxBlock(ctx, { currency, day, amount, extra = () => "" }) {
    let last = { key: null, rate: null, source: null, error: null };
    const manual = ctx.el("fx_rate");
    return async () => {
      const cur = currency(), d = day(), total = amount();
      const key = cur + "|" + d;
      if (key !== last.key) {
        last = { key, rate: null, source: null, error: null };
        try {
          const r = await api("GET", `/api/fx?currency=${cur}&on=${d}`);
          if (last.key === key) Object.assign(last, r);
        } catch (err) {
          if (last.key === key) last.error = err.message;
        }
      }
      const manualRate = parseNum(manual && manual.value);
      const rate = manualRate || last.rate;
      let html = `Сумма: <b>${total == null ? "—" : money(total, cur)}</b>`;
      if (cur !== "USD") {
        if (rate) {
          html += ` · ≈ <b>${total == null ? "—" : money(total / rate, "USD")}</b>
            <span class="muted">(${manualRate ? "курс задан вручную" : FX_SOURCE[last.source] || ""} ${num(rate, 4)} ${SIGN[cur]}/$)</span>`;
        } else if (last.error) {
          html += ` · <span class="down">${esc(last.error)}</span>`;
          ctx.form.querySelector('[data-field="fx_rate"]').classList.remove("hidden");
        }
        html += ` <button type="button" class="link small" data-fx-toggle>${manualRate ? "" : "изменить курс"}</button>`;
      }
      ctx.info.innerHTML = html + extra();
      const toggle = ctx.info.querySelector("[data-fx-toggle]");
      if (toggle) toggle.onclick = () => {
        ctx.form.querySelector('[data-field="fx_rate"]').classList.remove("hidden");
        manual.focus();
      };
    };
  }

  const fxField = { name: "fx_rate", label: "Курс к USD (единиц валюты за $1), если отличается", required: false, hidden: true, inputmode: "decimal" };

  // ---------- общие данные ----------

  let meta = null;
  async function loadMeta() {
    meta = await api("GET", "/api/meta");
    meta.currencies.forEach((c) => { SIGN[c.code] = c.symbol; });
    const el = document.getElementById("last-update");
    if (el) el.textContent = "Цены обновлены: " + dateTime(meta.last_update);
    return meta;
  }
  const activeAccounts = () => meta.accounts.filter((a) => !a.hidden);
  // Для фильтров — все аккаунты, включая скрытые: по ним могут быть закрытые позиции и операции.
  const accountNames = () => meta.accounts.map((a) => a.name);
  const accountOptions = (exclude) => activeAccounts().filter((a) => a.id !== exclude).map((a) => ({ value: a.id, label: a.name }));
  const currencyOptions = () => meta.currencies.map((c) => ({ value: c.code, label: `${c.code}, ${c.symbol} — ${c.name}` }));

  function requireAccounts(then) {
    if (activeAccounts().length) return then();
    toast("Сначала добавьте аккаунт Steam (кнопка «Аккаунты»)", true);
  }

  function accountsDialog(reload) {
    const dlg = document.createElement("dialog");
    document.body.append(dlg);
    dlg.addEventListener("close", () => { dlg.remove(); reload(); });
    async function draw() {
      await loadMeta();
      dlg.innerHTML = `<form method="dialog">
        <h2>Аккаунты Steam</h2>
        ${meta.accounts.map((a) => `<div class="account-row" data-id="${a.id}">
          <input type="text" value="${esc(a.name)}" aria-label="Название">
          <button type="button" class="small" data-act="rename">Сохранить</button>
          <label class="toggle small"><input type="checkbox" data-act="hide" ${a.hidden ? "checked" : ""}> скрыт</label>
          <button type="button" class="small" data-act="delete" ${a.used ? 'disabled title="По аккаунту есть операции — его можно только скрыть"' : ""}>Удалить</button>
        </div>`).join("") || '<p class="muted">Аккаунтов пока нет.</p>'}
        <div class="account-row"><input type="text" placeholder="Новый аккаунт" data-new><button type="button" class="primary small" data-act="add">Добавить</button></div>
        <p class="muted small">Скрытый аккаунт не предлагается в новых операциях, но его строки остаются в портфеле.</p>
        <div class="error hidden" data-error></div>
        <div class="dialog-actions"><button>Готово</button></div>
      </form>`;
      const err = dlg.querySelector("[data-error]");
      const run = async (fn) => {
        try { await fn(); await draw(); } catch (e) { err.textContent = e.message; err.classList.remove("hidden"); }
      };
      dlg.querySelectorAll("[data-act]").forEach((el) => {
        const row = el.closest(".account-row");
        const id = row && row.dataset.id;
        const act = el.dataset.act;
        const handler = () => run(async () => {
          if (act === "add") await api("POST", "/api/accounts", { name: dlg.querySelector("[data-new]").value });
          if (act === "rename") await api("POST", `/api/accounts/${id}`, { name: row.querySelector("input[type=text]").value });
          if (act === "hide") await api("POST", `/api/accounts/${id}`, { hidden: el.checked });
          if (act === "delete") await api("POST", `/api/accounts/${id}/delete`);
        });
        el.addEventListener(act === "hide" ? "change" : "click", handler);
      });
      // Enter в поле названия: сохранить или добавить, а не закрыть диалог.
      dlg.querySelector("form").addEventListener("keydown", (e) => {
        if (e.key !== "Enter" || !e.target.matches("input[type=text]")) return;
        e.preventDefault();
        const row = e.target.closest(".account-row");
        row.querySelector('[data-act="add"], [data-act="rename"]').click();
      });
    }
    draw().then(() => dlg.showModal());
  }

  const today = () => meta.today;
  const remember = (v) => { store.set("currency", v.currency); store.set("account", v.account_id); };

  // ---------- «Портфолио» ----------

  function portfolioPage() {
    const collapse = document.getElementById("opt-collapse");
    const closed = document.getElementById("opt-closed");
    collapse.checked = store.get("collapse", "0") === "1";
    const filters = {};
    let table;

    const columns = () => [
      { id: "icon", title: "", cls: "icon sticky-1", sortable: false, html: (r) => icon(r.icon) },
      { id: "item", title: "Название", key: "item", cls: "name sticky-2", filter: true, text: (r) => r.item + " " + r.type,
        html: (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.item)}</a><span class="type">${esc(r.type)}</span>` },
      { id: "quantity", title: "Кол-во", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0) },
      { id: "avg", title: "Средняя цена", key: "avg_price", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="${r.avg_price_usd != null ? "в USD по курсам дат покупок: " + money(r.avg_price_usd, "USD", true) : ""}">${money(r.avg_price, r.currency, true)}</span>` },
      { id: "first", title: "Первая покупка", key: "first_buy_date", filter: true, text: (r) => date(r.first_buy_date), html: (r) => date(r.first_buy_date) },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency },
      { id: "price", title: "Текущая цена", key: "price", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="Лучший ордер на покупку, снят ${esc(dateTime(r.price_at))}">${money(r.price, r.currency)}</span>` },
      { id: "pct_avg", title: "% к средней", key: "pct_avg", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_avg) },
      { id: "pct_1d", title: "% сутки", key: "pct_1d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_1d) },
      { id: "pct_7d", title: "% неделя", key: "pct_7d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_7d) },
      { id: "pct_30d", title: "% месяц", key: "pct_30d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_30d) },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames,
        values: (r) => r.account_id != null ? [r.account] : r.accounts.map((a) => a.name), html: (r) => esc(r.account) },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true,
        html: (r) => collapse.checked || closed.checked ? esc(r.comment)
          : `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`,
        bind: (td, r) => !collapse.checked && !closed.checked && bindComment(td, r) },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => closed.checked ? "" : '<button class="small" data-a="sell">Продано</button><button class="small" data-a="move">Перенести</button>'
          + `<button class="small danger icon-btn" data-a="remove" title="Удалить позицию" aria-label="Удалить позицию">${TRASH_ICON}</button>`,
        bind: (td, r) => {
          const actions = { sell: sellForm, move: moveForm, remove: removeForm };
          td.querySelectorAll("button").forEach((b) => b.onclick = () => actions[b.dataset.a](r));
        } },
    ];

    function bindComment(td, r) {
      const span = td.querySelector(".comment-edit");
      const edit = () => {
        const input = document.createElement("input");
        input.value = r.comment;
        input.maxLength = 500;
        td.replaceChildren(input);
        input.focus();
        let done = false;
        const save = async (commit) => {
          if (done) return;
          done = true;
          if (commit && input.value.trim() !== r.comment) {
            try {
              await api("POST", "/api/position-comment", { item: r.item, account_id: r.account_id, currency: r.currency, comment: input.value });
              r.comment = input.value.trim();
            } catch (e) { toast(e.message, true); }
          }
          td.innerHTML = `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`;
          bindComment(td, r);
        };
        input.addEventListener("keydown", (e) => {
          if (e.key === "Enter") save(true);
          if (e.key === "Escape") save(false);
        });
        input.addEventListener("blur", () => save(true));
      };
      span.addEventListener("click", edit);
      span.addEventListener("keydown", (e) => { if (e.key === "Enter") edit(); });
    }

    async function reload() {
      const data = await api("GET", `/api/portfolio?collapse=${collapse.checked}&closed=${closed.checked}`);
      const s = data.summary;
      document.getElementById("summary").innerHTML = closed.checked ? "" : `
        <div class="card stat"><div class="label">Вложено</div><div class="value">${money(s.invested_usd, "USD")}</div></div>
        <div class="card stat"><div class="label">Текущая стоимость</div><div class="value">${money(s.value_usd, "USD")}</div></div>
        <div class="card stat"><div class="label">Нереализованная прибыль</div><div class="value ${s.profit_usd > 0 ? "up" : s.profit_usd < 0 ? "down" : ""}">${s.profit_usd > 0 ? "+" : ""}${money(s.profit_usd, "USD")}</div>
          ${s.unpriced ? `<div class="small muted">без цены: ${s.unpriced} — учтены по себестоимости</div>` : ""}</div>`;
      table = makeTable(document.getElementById("table"), columns(), {
        storageKey: "portfolio",
        filters,
        empty: closed.checked ? "Закрытых позиций нет" : "Портфель пуст — нажмите «Добавить закупленное»",
      });
      table.setRows(data.rows);
    }

    function buyForm() {
      openForm({
        title: "Добавить закупленное",
        submit: "Добавить",
        info: true,
        layout: [
          { name: "item", label: "Предмет", type: "item" },
          [{ name: "quantity", label: "Количество", type: "number", attrs: 'min="1" step="1"', value: 1 },
            { name: "price", label: "Цена за штуку", inputmode: "decimal", attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' }],
          [{ name: "currency", label: "Валюта", type: "select", options: currencyOptions(), value: store.get("currency", "RUB") },
            { name: "account_id", label: "Аккаунт", type: "select", options: accountOptions(), value: store.get("account", "") }],
          { name: "date", label: "Дата покупки", type: "date", value: today(), attrs: `max="${today()}"` },
          fxField,
          { name: "comment", label: "Комментарий", type: "textarea", required: false },
        ],
        onInput: (ctx) => {
          ctx.fx = ctx.fx || fxBlock(ctx, {
            currency: () => ctx.el("currency").value,
            day: () => ctx.el("date").value || today(),
            amount: () => { const p = parseNum(ctx.el("price").value), q = parseInt(ctx.el("quantity").value, 10); return p != null && q ? p * q : null; },
          });
          ctx.fx();
        },
        onSubmit: async (v) => {
          await api("POST", "/api/buy", { ...v, account_id: +v.account_id, quantity: +v.quantity, fx_rate: v.fx_rate || null });
          remember(v);
          toast(`Добавлено: ${v.item}, ${v.quantity} шт.`);
          await reload();
        },
      });
    }

    function accountField(r, label) {
      // В свёрнутом виде строка объединяет аккаунты — нужно выбрать, с какого.
      if (r.account_id != null) return { name: "account_id", label, type: "select", options: [{ value: r.account_id, label: r.account }] };
      return { name: "account_id", label, type: "select",
        options: r.accounts.map((a) => ({ value: a.id, label: `${a.name} — ${a.quantity} шт.` })) };
    }
    const available = (r, accountId) => r.account_id != null ? r.quantity : (r.accounts.find((a) => a.id === +accountId) || {}).quantity;
    // При смене аккаунта в свёрнутом виде количество подстраивается под его остаток.
    function syncQuantity(ctx, r) {
      const acc = ctx.el("account_id").value;
      const max = available(r, acc);
      const q = ctx.el("quantity");
      q.max = max;
      if (ctx.lastAccount !== acc) {
        q.value = max;
        ctx.form.querySelector('[data-field="quantity"] > span').textContent = `Количество (есть ${num(max, 0)})`;
        ctx.lastAccount = acc;
      }
    }

    function sellForm(r) {
      openForm({
        title: `Продано: ${r.item}`,
        submit: "Записать продажу",
        info: true,
        layout: [
          accountField(r, "С аккаунта"),
          [{ name: "quantity", label: `Количество (есть ${num(r.quantity, 0)})`, type: "number", attrs: `min="1" max="${r.quantity}" step="1"`, value: r.quantity },
            { name: "price", label: `Чистая цена за штуку после комиссии, ${r.currency}`, inputmode: "decimal", attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' }],
          { name: "date", label: "Дата продажи", type: "date", value: today(), attrs: `max="${today()}"` },
          fxField,
        ],
        onInput: (ctx) => {
          syncQuantity(ctx, r);
          const price = () => parseNum(ctx.el("price").value);
          const qty = () => parseInt(ctx.el("quantity").value, 10);
          ctx.fx = ctx.fx || fxBlock(ctx, {
            currency: () => r.currency,
            day: () => ctx.el("date").value || today(),
            amount: () => price() != null && qty() ? price() * qty() : null,
            // В свёрнутом виде у аккаунтов разная средняя — прибыль не прогнозируем.
            extra: () => r.account_id != null && price() != null && qty()
              ? `<br>Прибыль: <b class="${price() >= r.avg_price ? "up" : "down"}">${money((price() - r.avg_price) * qty(), r.currency)}</b> <span class="muted">(средняя ${money(r.avg_price, r.currency, true)})</span>` : "",
          });
          ctx.fx();
        },
        onSubmit: async (v) => {
          await api("POST", "/api/sell", { item: r.item, currency: r.currency, account_id: +v.account_id, quantity: +v.quantity,
            price: v.price, date: v.date, fx_rate: v.fx_rate || null });
          toast(`Продажа записана: ${r.item}, ${v.quantity} шт.`);
          await reload();
        },
      });
    }

    function moveForm(r) {
      const from = r.account_id ?? r.accounts[0].id;
      if (!accountOptions(from).length) { toast("Нет другого аккаунта — добавьте его кнопкой «Аккаунты»", true); return; }
      openForm({
        title: `Перенести на другой аккаунт: ${r.item}`,
        submit: "Перенести",
        layout: [
          accountField(r, "С аккаунта"),
          { name: "to_account_id", label: "На аккаунт", type: "select", options: accountOptions(from) },
          [{ name: "quantity", label: `Количество (есть ${num(r.quantity, 0)})`, type: "number", attrs: `min="1" max="${r.quantity}" step="1"`, value: r.quantity },
            { name: "date", label: "Дата переноса", type: "date", value: today(), attrs: `max="${today()}"` }],
          { name: "comment", label: "Комментарий", type: "textarea", required: false },
          { name: "_note", type: "static", label: "", required: false,
            html: '<span class="muted small">Средняя цена и дата первой покупки переносятся вместе с предметами. «Вложено» и прибыль не меняются.</span>' },
        ],
        onInput: (ctx) => {
          const src = ctx.el("account_id").value;
          syncQuantity(ctx, r);
          const to = ctx.el("to_account_id");
          const keep = to.value;
          to.innerHTML = accountOptions(+src).map((o) => `<option value="${o.value}" ${String(o.value) === keep ? "selected" : ""}>${esc(o.label)}</option>`).join("");
        },
        onSubmit: async (v) => {
          await api("POST", "/api/transfer", { item: r.item, currency: r.currency, from_account_id: +v.account_id,
            to_account_id: +v.to_account_id, quantity: +v.quantity, date: v.date, comment: v.comment });
          toast(`Перенесено: ${r.item}, ${v.quantity} шт.`);
          await reload();
        },
      });
    }

    // Удаление позиции как ошибочно внесённой: «Вложено» и «Всего вложено» уменьшаются, прибыли нет.
    function removeForm(r) {
      openForm({
        title: `Удалить позицию: ${r.item}`,
        submit: "Удалить позицию",
        layout: [
          accountField(r, "Аккаунт"),
          { name: "_note", type: "static", label: "", required: false, html: '<div class="info" data-remove-info></div>' },
        ],
        onInput: (ctx) => {
          const acc = r.accounts.find((a) => a.id === +ctx.el("account_id").value) || r.accounts[0];
          ctx.form.querySelector("[data-remove-info]").innerHTML =
            `<p>Будет удалено <b>${num(acc.quantity, 0)} шт.</b> <b>${esc(r.item)}</b> с аккаунта <b>${esc(acc.name)}</b>
            в валюте <b>${esc(r.currency)}</b> — как ошибочно внесённые.</p>
            <p>Объём вложенных средств уменьшится на <b>${money(acc.cost, r.currency)}</b>.<br>Прошлые продажи не изменятся.</p>`;
        },
        onSubmit: async (v) => {
          await api("POST", "/api/remove-position", { item: r.item, currency: r.currency, account_id: +v.account_id });
          toast(`Позиция удалена: ${r.item}`);
          await reload();
        },
      });
    }

    // Выгрузка в Excel: портфель всегда по аккаунтам; фильтры таблицы — по выбору пользователя.
    async function exportExcel(btn) {
      const cols = columns();
      const active = cols.filter((c) => c.filter && filterSet(filters[c.id]));
      let body = {};
      if (active.length) {
        const list = active.map((c) => `<li>${esc(c.title)}: <b>${esc(filterText(filters[c.id]))}</b></li>`).join("");
        const useFilters = await choiceDialog("Выгрузка в Excel", `<p>Учитывать выставленные фильтры?</p><ul>${list}</ul>
          <p class="muted small">Фильтры применяются к листам «Портфель» и «Закрытые позиции». Ордера и журнал выгружаются полностью.</p>`,
          [{ label: "Нет, выгрузить всё", value: false }, { label: "Да, с фильтрами", value: true, primary: true }]);
        if (useFilters === null) return;
        if (useFilters) {
          const pass = (r) => active.every((c) => matches(filters[c.id], c, r));
          const keys = (rows) => rows.filter(pass).map((r) => [r.item, r.account_id, r.currency]);
          const [open, shut] = await Promise.all([api("GET", "/api/portfolio?closed=false"), api("GET", "/api/portfolio?closed=true")]);
          body = { open: keys(open.rows), closed: keys(shut.rows) };
        }
      }
      btn.disabled = true;
      try {
        await download("/api/export", body, "portfolio.xlsx");
      } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
    }

    collapse.addEventListener("change", () => { store.set("collapse", collapse.checked ? "1" : "0"); reload(); });
    closed.addEventListener("change", reload);
    document.getElementById("btn-excel").onclick = (e) => exportExcel(e.currentTarget);
    document.getElementById("btn-buy").onclick = () => requireAccounts(buyForm);
    document.getElementById("btn-accounts").onclick = () => accountsDialog(reload);
    loadMeta().then(reload).catch((e) => toast(e.message, true));
  }

  // ---------- «Ордера» ----------

  function ordersPage() {
    let table;
    const columns = [
      { id: "icon", title: "", cls: "icon sticky-1", sortable: false, html: (r) => icon(r.icon) },
      { id: "item", title: "Название", key: "item", cls: "name sticky-2", filter: true,
        html: (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.item)}</a>` },
      { id: "quantity", title: "Кол-во", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0) },
      { id: "price", title: "Цена за шт.", key: "price", cls: "num", numeric: true, filter: true, html: (r) => money(r.price, r.currency) },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames, values: (r) => [r.account],
        html: (r) => esc(r.account) },
      { id: "market", title: "Лучший ордер на площадке", key: "market_best", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="снят ${esc(dateTime(r.market_at))}">${money(r.market_best, r.currency)}</span>${r.outbid ? '<span class="badge">перебит</span>' : ""}` },
      { id: "total", title: "Сумма", key: "total", cls: "num", numeric: true, filter: true, html: (r) => money(r.total, r.currency) },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true, html: (r) => esc(r.comment) },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => '<button class="small" data-a="all">Куплено всё</button><button class="small" data-a="part">Частично</button><button class="small" data-a="change">Изменить</button><button class="small" data-a="remove">Убрать</button>',
        bind: (td, r) => td.querySelectorAll("button").forEach((b) => b.onclick = () => ({
          all: () => fillForm(r, false), part: () => fillForm(r, true), change: () => changeForm(r), remove: () => removeOrder(r),
        })[b.dataset.a]()) },
    ];

    async function reload() {
      const data = await api("GET", "/api/orders");
      document.getElementById("summary").innerHTML = `
        <div class="card stat"><div class="label">Зарезервировано в ордерах</div><div class="value">${money(data.summary.reserved_usd, "USD")}</div>
        <div class="small muted">по текущему курсу Steam</div></div>`;
      table = table || makeTable(document.getElementById("table"), columns, { storageKey: "orders", empty: "Выставленных ордеров нет" });
      table.setRows(data.rows);
    }

    function orderForm() {
      openForm({
        title: "Новый ордер",
        submit: "Выставить",
        info: true,
        layout: [
          { name: "item", label: "Предмет", type: "item" },
          [{ name: "quantity", label: "Количество", type: "number", attrs: 'min="1" step="1"', value: 1 },
            { name: "price", label: "Цена за штуку", inputmode: "decimal", attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' }],
          [{ name: "currency", label: "Валюта", type: "select", options: currencyOptions(), value: store.get("currency", "RUB") },
            { name: "account_id", label: "Аккаунт", type: "select", options: accountOptions(), value: store.get("account", "") }],
          { name: "comment", label: "Комментарий", type: "textarea", required: false },
        ],
        onInput: (ctx) => {
          const p = parseNum(ctx.el("price").value), q = parseInt(ctx.el("quantity").value, 10);
          ctx.info.innerHTML = `Сумма ордера: <b>${p != null && q ? money(p * q, ctx.el("currency").value) : "—"}</b>`;
        },
        onSubmit: async (v) => {
          await api("POST", "/api/orders", { ...v, account_id: +v.account_id, quantity: +v.quantity });
          remember(v);
          toast(`Ордер выставлен: ${v.item}`);
          await reload();
        },
      });
    }

    function fillForm(r, partial) {
      openForm({
        title: `${partial ? "Куплено частично" : "Куплено всё"}: ${r.item}`,
        submit: "Записать покупку",
        info: true,
        layout: [
          partial ? [{ name: "quantity", label: `Сколько куплено (в ордере ${r.quantity})`, type: "number", attrs: `min="1" max="${r.quantity}" step="1"` },
            { name: "date", label: "Дата", type: "date", value: today(), attrs: `max="${today()}"` }]
            : { name: "date", label: "Дата", type: "date", value: today(), attrs: `max="${today()}"` },
          { name: "price", label: `Цена за штуку, ${r.currency} (если фактическая отличалась)`, inputmode: "decimal", value: String(r.price).replace(".", ","),
            attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' },
          fxField,
        ],
        onInput: (ctx) => {
          ctx.fx = ctx.fx || fxBlock(ctx, {
            currency: () => r.currency,
            day: () => ctx.el("date").value || today(),
            amount: () => {
              const p = parseNum(ctx.el("price").value);
              const q = partial ? parseInt(ctx.el("quantity").value, 10) : r.quantity;
              return p != null && q ? p * q : null;
            },
          });
          ctx.fx();
        },
        onSubmit: async (v) => {
          await api("POST", `/api/orders/${r.order_id}/fill`, {
            quantity: partial ? +v.quantity : null, date: v.date, price: v.price, fx_rate: v.fx_rate || null,
          });
          toast(`Покупка записана в портфель: ${r.item}`);
          await reload();
        },
      });
    }

    function changeForm(r) {
      openForm({
        title: `Изменить ордер: ${r.item}`,
        submit: "Перевыставить",
        layout: [
          [{ name: "price", label: `Новая цена, ${r.currency}`, inputmode: "decimal", value: String(r.price).replace(".", ","),
            attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' },
            { name: "quantity", label: "Количество", type: "number", attrs: 'min="1" step="1"', value: r.quantity }],
          { name: "comment", label: "Комментарий", type: "textarea", required: false, value: r.comment },
        ],
        onSubmit: async (v) => {
          await api("POST", `/api/orders/${r.order_id}/change`, { price: v.price, quantity: +v.quantity, comment: v.comment });
          toast("Ордер перевыставлен");
          await reload();
        },
      });
    }

    async function removeOrder(r) {
      const ok = await confirmDialog("Убрать ордер?", `${r.item}: ${r.quantity} шт. по ${money(r.price, r.currency)}. Операция попадёт в журнал, её можно будет откатить.`, "Убрать");
      if (!ok) return;
      try {
        await api("POST", `/api/orders/${r.order_id}/remove`);
        toast("Ордер убран");
        await reload();
      } catch (e) { toast(e.message, true); }
    }

    document.getElementById("btn-order").onclick = () => requireAccounts(orderForm);
    document.getElementById("btn-accounts").onclick = () => accountsDialog(reload);
    loadMeta().then(reload).catch((e) => toast(e.message, true));
  }

  // ---------- «Журнал операций» ----------

  function journalPage() {
    const FX_SHORT = { steam: "Steam", cbr: "ЦБ", manual: "вручную", usd: "" };
    const mskDate = (ts) => new Date(ts).toLocaleDateString("sv-SE", { timeZone: "Europe/Moscow" });
    const opLink = (id) => `<a class="oplink" data-op="${id}">#${id}</a>`;
    const el = (id) => document.getElementById(id);
    let rows = [];
    let accountsById = {};

    function notes(r) {
      const out = [];
      if (r.target_id) out.push(`к операции ${opLink(r.target_id)} (${esc(r.target_kind || "")})`);
      if (r.order_id && r.kind !== "order_placed") out.push(`ордер ${opLink(r.order_id)}`);
      if (r.edited_by.length) out.push(`изменена: ${r.edited_by.map(opLink).join(", ")}`);
      if (r.deleted_by.length) out.push(`удалена: ${r.deleted_by.map(opLink).join(", ")}`);
      const last = r.history[r.history.length - 1];
      if (r.cancelled && last) out.push(`отменена ${esc(dateTime(last.at))}`);
      return out.map((n) => `<span class="note">${n}</span>`).join("");
    }

    function historyTitle(r) {
      return r.history.map((h) => `${h.action === "cancel" ? "откат" : "возврат"}: ${dateTime(h.at)}`).join("\n");
    }

    const columns = [
      { id: "id", title: "№", key: "id", numeric: true, filter: true, cls: "num", html: (r) => `#${r.id}` },
      { id: "op_date", title: "Дата операции", key: "op_date", filter: true, text: (r) => date(r.op_date), html: (r) => date(r.op_date) },
      { id: "created_at", title: "Действие в системе", key: "created_at", filter: true, text: (r) => dateTime(r.created_at),
        html: (r) => `<span class="small" title="${esc(historyTitle(r))}">${esc(dateTime(r.created_at))}</span>` },
      { id: "kind", title: "Тип", key: "kind_name", filter: true, cls: "kind", html: (r) => esc(r.kind_name) + notes(r) },
      { id: "item", title: "Предмет", key: "item", filter: true, cls: "name",
        html: (r) => `${esc(r.item || "")}${r.changes.length ? `<span class="changes">${r.changes.map((c) =>
          `${esc(c.field)}: ${esc(c.before)} → <b>${esc(c.after)}</b>`).join("<br>")}</span>` : ""}` },
      { id: "quantity", title: "Кол-во", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0) },
      { id: "price", title: "Цена", key: "price", cls: "num", numeric: true, filter: true, html: (r) => r.price == null ? "—" : money(r.price, r.currency) },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency || "" },
      { id: "fx", title: "Курс к USD", key: "fx_rate", cls: "num", numeric: true, filter: true,
        html: (r) => r.fx_rate == null ? '<span class="muted">—</span>'
          : `${num(r.fx_rate, 4)}<span class="note">${FX_SHORT[r.fx_source] || ""}</span>` },
      { id: "sum_usd", title: "Сумма, $", key: "sum_usd", cls: "num", numeric: true, filter: true, html: (r) => money(r.sum_usd, "USD") },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames,
        values: (r) => [r.account, r.to_account].filter(Boolean),
        html: (r) => esc(r.account) + (r.to_account ? ` → ${esc(r.to_account)}` : "") },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true, html: (r) => esc(r.comment) },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: (r) => (r.cancelled ? '<button class="small" data-a="restore">Вернуть</button>' : '<button class="small" data-a="cancel">Откатить</button>')
          + (r.can_edit ? '<button class="small" data-a="edit">Изменить</button><button class="small" data-a="delete">Удалить</button>' : ""),
        bind: (td, r) => td.querySelectorAll("button").forEach((b) => b.onclick = () => ({
          cancel: () => toggle(r, "cancel"), restore: () => toggle(r, "restore"), edit: () => editForm(r), delete: () => deleteForm(r),
        })[b.dataset.a]()) },
    ];

    const table = makeTable(el("table"), columns, {
      storageKey: "journal",
      empty: "Операций пока нет",
      defaultSort: { id: "id", dir: -1 },
      rowId: (r) => r.id,
      rowClass: (r) => r.cancelled ? "cancelled" : "",
      extraFilter: (r) => {
        if (el("f-kind").value && r.kind_name !== el("f-kind").value) return false;
        if (el("f-hide-cancelled").checked && r.cancelled) return false;
        const d = el("f-basis").value === "op_date" ? r.op_date : mskDate(r.created_at);
        if (el("f-from").value && d < el("f-from").value) return false;
        if (el("f-to").value && d > el("f-to").value) return false;
        return true;
      },
    });
    ["f-kind", "f-from", "f-to", "f-basis", "f-hide-cancelled"].forEach((id) => el(id).addEventListener("change", table.render));
    el("f-reset").onclick = () => {
      el("f-kind").value = el("f-from").value = el("f-to").value = "";
      el("f-hide-cancelled").checked = false;
      table.clearFilters();
    };

    // Переход к операции по ссылке #N: если её скрывают фильтры — сбрасываем их.
    function goTo(id) {
      let tr = document.querySelector(`tr[data-row="${id}"]`);
      if (!tr) { el("f-reset").click(); tr = document.querySelector(`tr[data-row="${id}"]`); }
      if (!tr) return;
      tr.scrollIntoView({ block: "center", behavior: "smooth" });
      tr.classList.remove("flash");
      void tr.offsetWidth;
      tr.classList.add("flash");
    }
    document.addEventListener("click", (e) => {
      const a = e.target.closest(".oplink");
      if (!a) return;
      e.preventDefault();
      const dlg = a.closest("dialog");
      if (dlg) dlg.close();
      goTo(+a.dataset.op);
    });

    async function reload() {
      const data = await api("GET", "/api/journal");
      rows = data.rows;
      accountsById = data.accounts;
      const kinds = [...new Set(rows.map((r) => r.kind_name))].sort((a, b) => a.localeCompare(b, "ru"));
      const keep = el("f-kind").value;
      el("f-kind").innerHTML = '<option value="">все</option>' + kinds.map((k) => `<option ${k === keep ? "selected" : ""}>${esc(k)}</option>`).join("");
      table.setRows(rows);
    }

    const linkify = (text) => esc(text).replace(/#(\d+)/g, (_, id) => opLink(id));

    function previewHtml(p) {
      const items = [];
      p.positions.forEach((c) => items.push(`${esc(c.item)} · ${esc(c.account)} · ${c.currency}: количество <b>${num(c.quantity_before, 0)} → ${num(c.quantity_after, 0)}</b>, средняя ${money(c.avg_before, c.currency, true)} → ${money(c.avg_after, c.currency, true)}`));
      p.sales.forEach((s) => {
        if (s.profit_after == null) items.push(`Продажа ${opLink(s.op_id)} (${esc(s.item)}) уйдёт из статистики: прибыль ${money(s.profit_before, s.currency)} (${money(s.profit_usd_before, "USD")})`);
        else if (s.profit_before == null) items.push(`Продажа ${opLink(s.op_id)} (${esc(s.item)}) вернётся в статистику: прибыль ${money(s.profit_after, s.currency)} (${money(s.profit_usd_after, "USD")})`);
        else items.push(`Продажа ${opLink(s.op_id)} (${esc(s.item)}): прибыль <b>${money(s.profit_before, s.currency)} → ${money(s.profit_after, s.currency)}</b> (${money(s.profit_usd_before, "USD")} → ${money(s.profit_usd_after, "USD")})`);
      });
      return items.length ? `<ul class="preview-list">${items.map((i) => `<li>${i}</li>`).join("")}</ul>`
        : '<p class="muted">Остатки в портфеле и прибыль не изменятся.</p>';
    }

    async function toggle(r, action) {
      let p;
      try { p = await api("GET", `/api/journal/${r.id}/preview?action=${action}`); } catch (e) { toast(e.message, true); return; }
      const verb = action === "cancel" ? "Откатить" : "Вернуть";
      const html = p.allowed ? previewHtml(p)
        : `<div class="error">${action === "cancel" ? "Откат" : "Возврат"} невозможен — сначала откатите:<ul>${p.violations.map((v) => `<li>${linkify(v)}</li>`).join("")}</ul></div>`;
      const ctx = openForm({
        title: `${verb} операцию #${r.id}: ${r.kind_name}${r.item ? " · " + r.item : ""}`,
        submit: p.allowed ? verb : "Понятно",
        layout: [{ name: "_", type: "static", label: p.allowed ? "Что изменится" : "", required: false, html }],
        onSubmit: async () => {
          if (!p.allowed) return;
          await api("POST", `/api/journal/${r.id}/${action}`);
          toast(action === "cancel" ? `Операция #${r.id} откачена` : `Операция #${r.id} возвращена`);
          await reload();
        },
      });
      if (!p.allowed) ctx.dlg.querySelector("[data-cancel]").classList.add("hidden");
    }

    function editForm(r) {
      const c = r.current;
      const fill = r.kind === "order_filled";
      const accounts = Object.entries(accountsById).map(([id, name]) => ({ value: +id, label: name }));
      const layout = [];
      if (!fill) layout.push({ name: "item", label: "Предмет", type: "item", value: c.item });
      layout.push(
        [{ name: "quantity", label: "Количество", type: "number", attrs: 'min="1" step="1"', value: c.quantity },
          { name: "price", label: r.kind === "sell" ? "Чистая цена за штуку" : "Цена за штуку", inputmode: "decimal",
            value: String(c.price).replace(".", ","), attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' }],
      );
      if (!fill) layout.push(
        [{ name: "currency", label: "Валюта", type: "select", options: currencyOptions(), value: c.currency },
          { name: "account_id", label: "Аккаунт", type: "select", options: accounts, value: c.account_id }],
      );
      layout.push(
        { name: "date", label: "Дата", type: "date", value: c.date, attrs: `max="${meta.today}"` },
        { name: "fx_rate", label: "Курс к USD, если нужно задать вручную", required: false, inputmode: "decimal",
          hint: c.currency === "USD" ? "" : `Сейчас: ${num(c.fx_rate, 4)} (${FX_SHORT[c.fx_source] || ""}). Пусто — при смене даты курс подберётся заново${c.fx_source === "manual" ? ", ручной курс сохранится" : ""}.` },
        { name: "comment", label: "Комментарий", type: "textarea", required: false, value: c.comment },
      );
      openForm({
        title: `Изменить операцию #${r.id} (${r.kind_name})`,
        submit: "Сохранить изменение",
        layout,
        onSubmit: async (v) => {
          await api("POST", `/api/journal/${r.id}/edit`, {
            date: v.date, quantity: +v.quantity, price: v.price, comment: v.comment, fx_rate: v.fx_rate || null,
            ...(fill ? {} : { item: v.item, currency: v.currency, account_id: +v.account_id }),
          });
          toast(`Изменение записано в журнал`);
          await reload();
        },
      });
    }

    function deleteForm(r) {
      openForm({
        title: `Удалить операцию #${r.id} (${r.kind_name})?`,
        submit: "Удалить",
        layout: [{ name: "_", type: "static", label: "", required: false,
          html: `<p>${esc(r.item)}: ${num(r.current.quantity, 0)} шт. по ${money(r.current.price, r.current.currency)}, ${date(r.current.date)}.</p>
            <p class="muted small">Удаление запишется в журнал отдельной операцией — его можно будет откатить.</p>` }],
        onSubmit: async () => {
          await api("POST", `/api/journal/${r.id}/delete`);
          toast(`Операция #${r.id} удалена`);
          await reload();
        },
      });
    }

    loadMeta().then(reload).catch((e) => toast(e.message, true));
  }

  // ---------- «Статистика» ----------

  function statsPage() {
    const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    const alpha = (hex, a) => {
      const n = parseInt(hex.replace("#", ""), 16);
      return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
    };
    // Порядок цветов секторов фиксирован по типам — цвет типа не меняется от данных.
    const PALETTE = ["#2563eb", "#16a34a", "#f59e0b", "#db2777", "#7c3aed", "#0891b2", "#ea580c", "#65a30d", "#dc2626", "#64748b"];
    const VALUE = "#2563eb", INVESTED = "#f59e0b", PROFIT = "#16a34a";
    const el = (id) => document.getElementById(id);
    const signed = (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${money(v, "USD")}</span>`;
    let data, lineChart, pieChart, typeNames = [];
    let period = +store.get("stats.period", "0");
    let chartCurrency = store.get("stats.currency", "TOTAL");
    let bestSort = store.get("stats.best", "pct");
    let drill = null;

    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--border");

    const stat = (label, value, extra = "") => `<div class="card stat"><div class="label">${label}</div><div class="value">${value}</div>${extra}</div>`;

    function renderSummary() {
      const s = data.summary;
      el("summary-now").innerHTML = stat("Вложено в текущие позиции", money(s.invested_usd, "USD"))
        + stat("Текущая стоимость", money(s.value_usd, "USD"))
        + stat("Нереализованная прибыль", signed(s.profit_usd), s.unpriced ? `<div class="small muted">без цены: ${s.unpriced}</div>` : "");
      el("summary-all").innerHTML = stat("Всего вложено", money(s.total_invested_usd, "USD"))
        + stat("Всего продано", money(s.total_sold_usd, "USD"))
        + stat("Всего получено прибыли", signed(s.total_profit_usd));
    }

    function renderLine() {
      const s = data.series;
      const cur = s.currency || "USD";
      const min = period ? Date.now() - period * 86400000 : undefined;
      document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("active", +b.dataset.days === period));
      const axis = (v) => cur === "USD" ? "$" + num(v, 0) : `${num(v, 0)} ${SIGN[cur] || cur}`;
      // Оценка портфеля лежит слоем поверх реализованной прибыли: y = оценка + прибыль на этот момент,
      // настоящее значение — в v (его показывает подсказка). «Вложено» — отдельная линия без заливки.
      const profit = (s.profit || []).some(([, y]) => y) ? s.profit : [];
      const profitAt = (x) => {
        let p = 0;
        for (const [px, py] of profit) { if (px > x) break; p = py; }
        return p;
      };
      const ds = (label, points, color, extra = {}, stacked = false) => ({
        label, data: points.map(([x, v]) => ({ x, y: stacked ? v + profitAt(x) : v, v })),
        borderColor: color, backgroundColor: alpha(color, 0.18),
        fill: "origin", pointRadius: 0, pointHoverRadius: 3, borderWidth: 2, tension: 0, ...extra,
      });
      const onProfit = profit.length ? { target: 0 } : "origin";
      const datasets = [
        ds("Реализованная прибыль", profit, PROFIT, { stepped: "before", backgroundColor: alpha(PROFIT, 0.25) }),
        ds("Текущая оценка портфеля", s.value, VALUE, { fill: onProfit }, true),
        ds("Оценка по истории продаж Steam", s.value_estimate, VALUE, { fill: onProfit, borderDash: [5, 4], backgroundColor: alpha(VALUE, 0.08) }, true),
        ds("Вложено", s.invested, INVESTED, { stepped: "before", fill: false, borderWidth: 2.5 }),
      ].filter((d) => d.data.length);
      const fmtDay = (ms) => new Date(ms).toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "2-digit" });
      const config = {
        type: "line",
        data: { datasets },
        options: {
          maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
          interaction: { mode: "nearest", axis: "x", intersect: false },
          scales: {
            x: { type: "linear", min, ticks: { callback: fmtDay, maxTicksLimit: 8 }, grid: { display: false } },
            y: { beginAtZero: true, ticks: { callback: axis } },
          },
          plugins: {
            legend: { labels: { boxWidth: 12, usePointStyle: true } },
            tooltip: {
              itemSort: (a, b) => b.raw.v - a.raw.v,  // сверху — большее значение
              callbacks: {
                title: (items) => dateTime(new Date(items[0].parsed.x).toISOString()),
                label: (c) => `${c.dataset.label}: ${money(c.raw.v, cur)}`,
              },
            },
          },
        },
      };
      if (lineChart) lineChart.destroy();
      lineChart = new Chart(el("line"), config);
      const scope = chartCurrency === "TOTAL" ? "Все операции, суммы в USD по курсу на дату. "
        : `Только строки в ${cur}, суммы в ${cur} без пересчёта. `;
      el("line-note").textContent = !datasets.length
        ? (chartCurrency === "TOTAL" ? "Данных пока нет — внесите покупки на вкладке «Портфолио»." : `Позиций в ${cur} нет.`)
        : scope + (s.value_estimate.length ? `До ${date(s.launch)} оценка портфеля — по медианным ценам продаж Steam (пунктир); дальше — по лучшему ордеру на покупку, точка раз в 6 часов.`
        : "Оценка портфеля — по лучшему ордеру на покупку, точка раз в 6 часов.")
          + " Реализованная прибыль — нарастающим итогом по продажам: цена продажи минус средняя цена покупки."
          + (profit.length ? " Оценка портфеля нарисована слоем поверх прибыли: верхний край синей заливки — их сумма." : "");
    }

    async function loadSeries() {
      try {
        data.series = await api("GET", `/api/stats/series?currency=${chartCurrency}`);
      } catch (e) { toast(e.message, true); }
      renderLine();
    }

    function colorOf(type) {
      const i = typeNames.indexOf(type);
      return PALETTE[(i < 0 ? typeNames.length : i) % PALETTE.length];
    }

    function renderPie() {
      const types = data.types.types;
      const level = drill ? types.find((t) => t.type === drill) : null;
      if (drill && !level) drill = null;
      const slices = level ? level.items : types;
      const base = level ? colorOf(level.type) : null;
      const colors = slices.map((s, i) => level ? alpha(base, Math.max(0.25, 1 - i * (0.75 / Math.max(slices.length, 1)))) : colorOf(s.type));
      el("pie-title").textContent = level ? `${level.type}: ${money(level.value_usd, "USD")}` : `По типам предметов: ${money(data.types.total_usd, "USD")}`;
      el("pie-back").classList.toggle("hidden", !level);
      const config = {
        type: "doughnut",
        data: {
          labels: slices.map((s) => level ? s.item : s.type),
          datasets: [{ data: slices.map((s) => s.value_usd), backgroundColor: colors, borderColor: css("--surface"), borderWidth: 2 }],
        },
        options: {
          maintainAspectRatio: false, animation: { duration: 250 }, cutout: "55%",
          plugins: {
            legend: { display: false },
            tooltip: { callbacks: { label: (c) => `${c.label}: ${money(c.parsed, "USD")} (${num(slices[c.dataIndex].share, 1)}%)` } },
          },
          onClick: (_, els) => { if (!level && els.length) { drill = slices[els[0].index].type; renderPie(); } },
          onHover: (e, els) => { e.native.target.style.cursor = !level && els.length ? "pointer" : "default"; },
        },
      };
      if (pieChart) pieChart.destroy();
      pieChart = slices.length ? new Chart(el("pie"), config) : null;
      const sw = (c) => `<span class="swatch" style="background:${c}"></span>`;
      el("pie-table").innerHTML = !slices.length ? '<div class="empty-state">Портфель пуст</div>'
        : `<table><thead><tr><th>${level ? "Предмет" : "Тип"}</th>${level ? '<th class="num">Кол-во</th>' : ""}<th class="num">Стоимость</th><th class="num">Доля</th>${level ? "<th>Тип</th>" : ""}</tr></thead><tbody>
        ${slices.map((s, i) => level
          ? `<tr><td class="name">${sw(colors[i])}<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.item)}</a>${s.no_price ? '<span class="note">без цены — по себестоимости</span>' : ""}</td>
              <td class="num">${num(s.quantity, 0)}</td><td class="num">${money(s.value_usd, "USD")}</td><td class="num">${num(s.share, 1)}%</td>
              <td><select data-item="${esc(s.item)}" aria-label="Тип предмета">${typeNames.map((t) => `<option ${t === level.type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></td></tr>`
          : `<tr class="clickable" data-type="${esc(s.type)}"><td>${sw(colors[i])}${esc(s.type)}</td><td class="num">${money(s.value_usd, "USD")}</td><td class="num">${num(s.share, 1)}%</td></tr>`).join("")}
        </tbody></table>`;
      el("pie-table").querySelectorAll("tr[data-type]").forEach((tr) => tr.onclick = () => { drill = tr.dataset.type; renderPie(); });
      el("pie-table").querySelectorAll("select[data-item]").forEach((sel) => sel.onchange = async () => {
        try {
          await api("POST", "/api/items/type", { item: sel.dataset.item, type: sel.value });
          toast(`Тип «${sel.dataset.item}»: ${sel.value}`);
          await load();
        } catch (e) { toast(e.message, true); }
      });
    }

    function renderBest() {
      document.querySelectorAll("#best-sort button").forEach((b) => b.classList.toggle("active", b.dataset.sort === bestSort));
      const key = bestSort === "pct" ? "profit_pct" : "profit_usd";
      const all = [...data.sales].sort((a, b) => b[key] - a[key]);
      const rows = el("best-all").checked ? all : all.slice(0, 10);
      el("best").innerHTML = !rows.length ? '<div class="empty-state">Продаж пока нет</div>'
        : `<table><thead><tr><th>#</th><th>Предмет</th><th>Дата продажи</th><th class="num">Кол-во</th><th class="num">Прибыль, $</th><th class="num">Прибыль, %</th><th class="num">Срок, мес.</th></tr></thead><tbody>
          ${rows.map((s, i) => `<tr><td class="muted">${i + 1}</td><td class="name">${esc(s.item)}<span class="type">${money(s.profit, s.currency)} в валюте строки</span></td>
            <td>${date(s.date)}</td><td class="num">${num(s.quantity, 0)}</td><td class="num">${signed(s.profit_usd)}</td><td class="num">${pct(s.profit_pct)}</td><td class="num">${num(s.months, 1)}</td></tr>`).join("")}
          </tbody></table>`;
    }

    async function load() {
      data = await api("GET", "/api/stats");
      typeNames = data.type_names;
      renderSummary();
      // В списке только валюты, которые есть в портфеле, — по убыванию вложенного.
      const codes = data.chart_currencies.map((c) => c.code);
      if (!codes.includes(chartCurrency)) chartCurrency = "TOTAL";
      el("chart-currency").innerHTML = '<option value="TOTAL">Total</option>'
        + data.chart_currencies.map((c) => `<option value="${esc(c.code)}">${esc(c.code)}, ${esc(c.symbol)}</option>`).join("");
      el("chart-currency").value = chartCurrency;
      if (chartCurrency === "TOTAL") renderLine(); else await loadSeries();
      renderPie();
      renderBest();
    }

    document.querySelectorAll("#period button").forEach((b) => b.onclick = () => {
      period = +b.dataset.days;
      store.set("stats.period", String(period));
      renderLine();
    });
    el("chart-currency").onchange = (e) => {
      chartCurrency = e.target.value;
      store.set("stats.currency", chartCurrency);
      loadSeries();
    };
    document.querySelectorAll("#best-sort button").forEach((b) => b.onclick = () => {
      bestSort = b.dataset.sort;
      store.set("stats.best", bestSort);
      renderBest();
    });
    el("best-all").onchange = renderBest;
    el("pie-back").onclick = () => { drill = null; renderPie(); };
    loadMeta().then(load).catch((e) => toast(e.message, true));
  }

  // ---------- вкладка «Настройки» ----------

  function settingsPage() {
    const el = (id) => document.getElementById(id);
    const form = el("alerts-form");
    const errorBox = el("alerts-error");
    const NUMBERS = ["day_pct", "min_price_usd", "week2_pct", "week3_pct", "cooldown_hours"];
    let data = null;

    const fill = (a) => {
      form.elements.enabled.checked = a.enabled;
      NUMBERS.forEach((k) => { form.elements[k].value = String(a[k]).replace(".", ","); });
    };
    const size = (n) => n < 1048576 ? `${num(n / 1024, 0)} КБ` : `${num(n / 1048576, 1)} МБ`;

    async function action(btn, url, ok) {
      btn.disabled = true;
      try {
        await api("POST", url);
        toast(ok);
        await load();
      } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
    }

    function renderTelegram() {
      const t = data.telegram;
      const box = el("telegram");
      if (t.chat) {
        box.innerHTML = `<p>Бот подключён, сообщения приходят: <b>${esc(t.chat)}</b>.</p>
          <div class="settings-actions"><button id="tg-test">Отправить тестовое сообщение</button></div>`;
        el("tg-test").onclick = (e) => action(e.target, "/api/settings/telegram-test", "Тестовое сообщение отправлено");
      } else {
        box.innerHTML = `<p>Бот ещё не подключён${t.token ? " (токен задан, чат не привязан)" : ""}. Токен вводится только на сервере:</p>
          <ol><li>Подключитесь к серверу по SSH и выполните <code>steamfolio telegram setup</code>.</li>
          <li>Вставьте токен бота от @BotFather — при вводе он не отображается.</li>
          <li>Напишите боту в Telegram любое сообщение и подтвердите, что это вы.</li></ol>`;
      }
    }

    function renderBackups() {
      const b = data.backups;
      const last = b.local[0];
      el("backups").innerHTML = `<p>Каждый день в 01:00 МСК база копируется на сервер, хранятся последние ${b.keep} копий.
          Сейчас копий: ${b.local.length}${last ? `, последняя — ${esc(last.name)} (${size(last.size)})` : ""}.</p>
        <p>По понедельникам в 01:00 МСК сжатая копия уходит в Telegram (без сессий входа на сайт).
          Последняя отправка: ${b.sent_at ? dateTime(b.sent_at) : "ещё не было"}.</p>
        <div class="settings-actions"><button id="backup-send" ${data.telegram.chat ? "" : "disabled"}>Отправить копию в Telegram сейчас</button></div>`;
      el("backup-send").onclick = (e) => action(e.target, "/api/settings/backup-send", "Копия отправлена в Telegram");
    }

    async function load() {
      data = await api("GET", "/api/settings");
      fill(data.alerts);
      renderTelegram();
      renderBackups();
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      errorBox.classList.add("hidden");
      const body = { enabled: form.elements.enabled.checked };
      for (const k of NUMBERS) {
        body[k] = parseNum(form.elements[k].value);
        if (body[k] == null) {
          errorBox.textContent = "Заполните все поля числами";
          errorBox.classList.remove("hidden");
          return;
        }
      }
      try {
        data.alerts = await api("POST", "/api/settings/alerts", body);
        fill(data.alerts);
        toast("Настройки уведомлений сохранены");
      } catch (err) {
        errorBox.textContent = err.message;
        errorBox.classList.remove("hidden");
      }
    });
    el("alerts-reset").onclick = () => { fill(data.defaults); toast("Значения по умолчанию подставлены — нажмите «Сохранить»"); };
    loadMeta().then(load).catch((e) => toast(e.message, true));
  }

  return { portfolioPage, ordersPage, journalPage, statsPage, settingsPage };
})();
