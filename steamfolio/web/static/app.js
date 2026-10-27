"use strict";

const SF = (() => {
  // ---------- форматирование ----------

  const SIGN = { RUB: "₽", USD: "$", CNY: "¥", UAH: "₴" };
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
  function matches(filter, col, row) {
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

  // filters — общий объект страницы: значения фильтров переживают перезагрузку данных.
  function makeTable(container, columns, { storageKey, empty, filters = {} }) {
    let rows = [];
    let sort = JSON.parse(store.get(storageKey + ".sort", "null") || "null");

    function render() {
      const visible = rows.filter((r) => columns.every((c) => !c.filter || matches(filters[c.id] || "", c, r)));
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
      tbody.innerHTML = visible.length ? visible.map((r) => `<tr>${columns.map((c) =>
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
      <tr class="filters">${columns.map((c) => `<th class="${(c.cls || "").replace(/\b(num|icon|name|actions|comment)\b/g, "")}">${c.filter
        ? `<input data-id="${c.id}" placeholder="${c.numeric ? "&gt;0" : "фильтр"}" aria-label="Фильтр: ${esc(c.title)}">` : ""}</th>`).join("")}</tr>
      </thead><tbody></tbody></table>`;
    container.querySelectorAll("th[data-id]").forEach((th) => th.addEventListener("click", () => {
      const id = th.dataset.id;
      sort = sort && sort.id === id ? (sort.dir > 0 ? { id, dir: -1 } : null) : { id, dir: 1 };
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
    return { setRows(r) { rows = r; render(); } };
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
    const el = document.getElementById("last-update");
    if (el) el.textContent = "Цены обновлены: " + dateTime(meta.last_update);
    return meta;
  }
  const activeAccounts = () => meta.accounts.filter((a) => !a.hidden);
  const accountOptions = (exclude) => activeAccounts().filter((a) => a.id !== exclude).map((a) => ({ value: a.id, label: a.name }));
  const currencyOptions = () => meta.currencies.map((c) => ({ value: c, label: c }));

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
      { id: "account", title: "Аккаунт", key: "account", filter: true, html: (r) => esc(r.account) },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true,
        html: (r) => collapse.checked || closed.checked ? esc(r.comment)
          : `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`,
        bind: (td, r) => !collapse.checked && !closed.checked && bindComment(td, r) },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => closed.checked ? "" : '<button class="small" data-a="sell">Продано</button><button class="small" data-a="move">Перенести</button>',
        bind: (td, r) => {
          td.querySelectorAll("button").forEach((b) => b.onclick = () => b.dataset.a === "sell" ? sellForm(r) : moveForm(r));
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

    collapse.addEventListener("change", () => { store.set("collapse", collapse.checked ? "1" : "0"); reload(); });
    closed.addEventListener("change", reload);
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
      { id: "account", title: "Аккаунт", key: "account", filter: true, html: (r) => esc(r.account) },
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

  return { portfolioPage, ordersPage };
})();
