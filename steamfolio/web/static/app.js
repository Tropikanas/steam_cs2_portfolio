"use strict";

const SF = (() => {
  // ---------- форматирование ----------

  const SIGN = { RUB: "₽", USD: "$", CNY: "¥", UAH: "₴" };
  // ---------- поле даты «дд.мм.гггг» ----------
  // Каждое input[type=date] на сайте заменяется текстовым полем с неизменяемыми точками: щелчок выделяет
  // блок (день, месяц или год), цифры меняют только его, стрелки ←/→ — соседний блок, ↑/↓ — ±1.
  // Календарь — кнопкой справа. Само input[type=date] остаётся скрытым: в нём значение (ГГГГ-ММ-ДД)
  // для форм и фильтров, и из него открывается календарь браузера.
  const CALENDAR_ICON = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/></svg>';
  const DATE_BLANKS = ["дд", "мм", "гггг"];
  const DATE_WIDTHS = [2, 2, 4];
  const DATE_BLANK = DATE_BLANKS.join(".");
  const valueProp = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");

  function enhanceDate(native) {
    if (native.dataset.dateField) return native._dateText;
    native.dataset.dateField = "1";
    const wrap = document.createElement("span");
    wrap.className = "date-field";
    const text = document.createElement("input");
    text.type = "text";
    text.inputMode = "numeric";
    text.autocomplete = "off";
    text.placeholder = DATE_BLANK;
    text.className = native.className;
    text.required = native.required;
    native.required = false;  // проверяет текстовое поле: скрытое браузер не может подсветить
    text.setAttribute("aria-label", native.getAttribute("aria-label") || "Дата, дд.мм.гггг");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "date-btn";
    btn.tabIndex = -1;
    btn.title = "Выбрать в календаре";
    btn.setAttribute("aria-label", "Выбрать в календаре");
    btn.innerHTML = CALENDAR_ICON;
    native.replaceWith(wrap);
    native.classList.add("date-native");
    native.tabIndex = -1;
    native.setAttribute("aria-hidden", "true");
    wrap.append(text, btn, native);
    native._dateText = text;
    text._dateNative = native;

    let seg = 0, typed = "";  // выбранный блок и набранные в нём цифры
    const fromIso = (iso) => iso ? `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}` : "";
    const show = () => { text.value = fromIso(valueProp.get.call(native)); typed = ""; };
    // Программная запись в скрытое поле (сброс фильтров, подстановка значения) — видна и в тексте.
    Object.defineProperty(native, "value", {
      configurable: true,
      get: () => valueProp.get.call(native),
      set: (v) => { valueProp.set.call(native, v); show(); },
    });
    show();

    // Блоки разделены точками и могут быть короче полной ширины, пока их набирают («2» в году, а не «0002»).
    const parts = () => {
      const p = (text.value || DATE_BLANK).split(".");
      return p.length === 3 ? p : DATE_BLANKS.slice();
    };
    const range = (i) => {
      const p = parts();
      const start = p.slice(0, i).reduce((n, s) => n + s.length + 1, 0);
      return [start, start + p[i].length];
    };
    const select = () => text.setSelectionRange(...range(seg));
    const part = (i) => parts()[i];
    const put = (i, s) => {
      const p = parts();
      p[i] = s;
      text.value = p.join(".");
    };
    // Уходя из дня или месяца, набранную одну цифру дополняем нулём: «5» → «05».
    const finish = () => {
      if (typed && seg < 2 && typed.length === 1) put(seg, typed.padStart(2, "0"));
      typed = "";
    };
    // Текст → скрытое поле: полная правильная дата — записать и сообщить формам; иначе пусто.
    const commit = () => {
      const [d, m, y] = [0, 1, 2].map(part);
      let iso = "";
      if (/^\d{2}$/.test(d) && /^\d{2}$/.test(m) && /^\d{4}$/.test(y)) {
        const dt = new Date(Date.UTC(+y, +m - 1, +d));
        if (dt.getUTCDate() === +d && dt.getUTCMonth() === +m - 1) iso = `${y}-${m}-${d}`;
      }
      const partial = text.value && text.value !== DATE_BLANK;
      text.setCustomValidity(partial && !iso ? "Введите дату полностью: дд.мм.гггг"
        : iso && native.max && iso > native.max ? `Дата не позже ${fromIso(native.max)}`
        : iso && native.min && iso < native.min ? `Дата не раньше ${fromIso(native.min)}` : "");
      if (iso !== valueProp.get.call(native)) {
        valueProp.set.call(native, iso);
        native.dispatchEvent(new Event("input", { bubbles: true }));
        native.dispatchEvent(new Event("change", { bubbles: true }));
      }
    };
    const pick = (i) => { finish(); commit(); seg = Math.max(0, Math.min(2, i)); select(); };
    text._pick = pick;
    const segAt = (pos) => [0, 1, 2].find((i) => pos <= range(i)[1]) ?? 2;

    function digit(ch) {
      if (!text.value) text.value = DATE_BLANK;
      typed += ch;
      put(seg, typed);  // только набранное, без нулей впереди
      // День: после «4»–«9» второй цифры не бывает; месяц — после «2»–«9»; год — после 4 цифр.
      const full = typed.length >= DATE_WIDTHS[seg] || (seg === 0 && +typed > 3) || (seg === 1 && +typed > 1);
      commit();
      if (full) { if (seg < 2) pick(seg + 1); else { typed = ""; select(); } } else select();
    }

    function step(delta) {
      if (!text.value || text.value === DATE_BLANK) text.value = fromIso(native.max && native.max < todayIso() ? native.max : todayIso());
      const v = parseInt(part(seg), 10);
      const [lo, hi] = [[1, 31], [1, 12], [1900, 2100]][seg];
      const next = isNaN(v) ? lo : v + delta > hi ? lo : v + delta < lo ? hi : v + delta;
      put(seg, String(next).padStart(seg === 2 ? 4 : 2, "0"));
      typed = "";
      commit();
      select();
    }

    let clicking = false, wasBlank = false;
    text.addEventListener("mousedown", () => { clicking = true; wasBlank = !text.value || text.value === DATE_BLANK; });
    text.addEventListener("focus", () => {
      text._pickerOpen = false;
      if (!text.value) text.value = DATE_BLANK;
      // Клавиатурой (Tab, двойной щелчок по ячейке) — с дня; щелчком — блок под курсором (его выделит mouseup).
      if (!clicking) setTimeout(() => { if (document.activeElement === text) select(); }, 0);
    });
    text.addEventListener("mouseup", () => {
      clicking = false;
      pick(wasBlank ? 0 : segAt(text.selectionStart ?? 0));
    });
    text.addEventListener("blur", () => { finish(); if (text.value === DATE_BLANK) text.value = ""; commit(); });
    text.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;  // копировать/вставить
      // Enter сохраняет форму или ячейку — набранная одна цифра дня/месяца должна успеть стать «05».
      if (e.key === "Enter") { finish(); commit(); return; }
      if (/^\d$/.test(e.key)) { e.preventDefault(); digit(e.key); return; }
      const moves = { ArrowLeft: -1, ArrowRight: 1, ".": 1, "/": 1, ",": 1, "-": 1 };
      if (e.key in moves) { e.preventDefault(); pick(seg + moves[e.key]); return; }
      if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); step(e.key === "ArrowUp" ? 1 : -1); return; }
      if (e.key === "Backspace" || e.key === "Delete") {
        e.preventDefault();
        if (!text.value) return;
        put(seg, DATE_BLANKS[seg]);
        typed = "";
        commit();
        select();
        return;
      }
      if (e.key.length === 1) e.preventDefault();  // буквы и прочее не вводятся; Tab, Enter, Esc — как обычно
    });
    // Ввод без keydown (экранная клавиатура, автозаполнение): цифры — как с клавиатуры.
    text.addEventListener("beforeinput", (e) => {
      if (e.inputType === "insertText" && e.data) {
        e.preventDefault();
        [...e.data].filter((c) => /\d/.test(c)).forEach(digit);
      } else if (e.inputType !== "insertFromPaste") e.preventDefault();
    });
    text.addEventListener("paste", (e) => {
      e.preventDefault();
      const s = (e.clipboardData.getData("text") || "").trim();
      const m = s.match(/^(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{4})$/) || s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
      if (!m) return;
      const [d, mo, y] = m[1].length === 4 ? [m[3], m[2], m[1]] : [m[1], m[2], m[3]];
      text.value = `${d.padStart(2, "0")}.${mo.padStart(2, "0")}.${y}`;
      commit();
      pick(0);
    });
    // Календарь: выбранная в нём дата приходит в скрытое поле.
    btn.addEventListener("mousedown", (e) => e.preventDefault());  // фокус остаётся в тексте
    btn.addEventListener("click", () => {
      text._pickerOpen = true;  // текст может потерять фокус, пока открыт календарь, — это не конец правки
      try { native.showPicker(); } catch { text._pickerOpen = false; text.focus(); }
    });
    native.addEventListener("change", (e) => {
      if (!e.isTrusted) return;
      text._pickerOpen = false;
      show();
      text.setCustomValidity("");
      text.dispatchEvent(new CustomEvent("datepicked"));
      if (document.activeElement !== text) text.focus();
      pick(0);
    });
    return text;
  }

  const todayIso = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Moscow" });
  const enhanceDatesIn = (root) => root.querySelectorAll?.('input[type="date"]:not([data-date-field])').forEach(enhanceDate);
  new MutationObserver((records) => records.forEach((r) => r.addedNodes.forEach((n) => {
    if (n.nodeType !== 1) return;
    if (n.matches('input[type="date"]')) enhanceDate(n); else enhanceDatesIn(n);
  }))).observe(document.documentElement, { childList: true, subtree: true });
  document.addEventListener("DOMContentLoaded", () => enhanceDatesIn(document));
  enhanceDatesIn(document);

  const TRASH_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/></svg>';
  const FX_SOURCE = { steam: "курс Steam", cbr: "курс ЦБ на дату", usd: "", manual: "курс задан вручную", transfer: "курс исходных покупок" };

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
  // Плитки сводки (Портфолио, Ордера, Статистика): одинаковый размер, подсказка при наведении.
  const TIPS = {
    invested: "Сумма инвестиций в предметы, находящиеся в данный момент в портфолио по курсу на дату покупки",
    value: "Текущая стоимость предметов, находящихся в данный момент в портфолио по текущему курсу (берётся по наивысшему ордеру на покупку на ТП Steam)",
    unrealized: "Разница между текущей стоимостью имеющихся предметов и суммой вложений в них",
    totalInvested: "Сумма инвестиций в Steam за всю историю портфеля по курсу на дату покупки. Учитывает все предметы, которые когда-либо были куплены, а затем проданы или находятся в портфолио. Не учитывает удалённые покупки.",
    totalSold: "Сумма продаж за всю историю портфеля по курсу на дату продажи (без исключения объёма вложенных средств)",
    totalProfit: "Разница между суммой продажи всех проданных предметов в портфеле и суммой их закупки.",
  };
  const signedUsd = (v) => `<span class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${v > 0 ? "+" : ""}${money(v, "USD")}</span>`;
  const statTile = (label, value, { tip = "", extra = "" } = {}) =>
    `<div class="card stat"><div class="label">${label}${tip ? `<span class="tip-icon" data-tip="${esc(tip)}" tabindex="0" aria-label="${esc(tip)}">?</span>` : ""}</div><div class="value">${value}</div>${extra}</div>`;
  // Текущие позиции: одни и те же три плитки на Портфолио и в Статистике.
  const holdingTiles = (s, unpricedNote) =>
    statTile("Вложено в текущие позиции", money(s.invested_usd, "USD"), { tip: TIPS.invested })
    + statTile("Текущая стоимость", money(s.value_usd, "USD"), { tip: TIPS.value })
    + statTile(s.profit_usd < 0 ? "Нереализованный убыток" : "Нереализованная прибыль", signedUsd(s.profit_usd),
      { tip: TIPS.unrealized, extra: s.unpriced ? `<div class="small muted">${unpricedNote(s.unpriced)}</div>` : "" });
  // Реализованная прибыль/убыток: одна и та же плитка в Статистике и в «Закрытых позициях».
  const realizedTile = (s) => statTile(s.total_profit_usd < 0 ? "Реализованный убыток" : "Реализованная прибыль",
    signedUsd(s.total_profit_usd), { tip: TIPS.totalProfit });
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
  // Фильтр даты (filter: "date"): {mode: "eq" | "range" | "from" | "to", from, to} — даты ГГГГ-ММ-ДД, границы включаются.
  function matches(filter, col, row) {
    if (Array.isArray(filter)) return !filter.length || col.values(row).some((v) => filter.includes(v));
    if (filter && typeof filter === "object") {
      if (!dateFilterSet(filter)) return true;
      const d = (row[col.key] || "").slice(0, 10);
      if (!d) return false;
      const { mode, from, to } = filter;
      if (mode === "eq") return d === from;
      return (!from || mode === "to" || d >= from) && (!to || mode === "from" || d <= to);
    }
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

  const dateFilterSet = (f) => !!(f && (f.mode === "to" ? f.to : f.mode === "range" ? f.from || f.to : f.from));
  const dateFilterLabel = (f) => {
    if (!dateFilterSet(f)) return "Все";
    const d = (iso) => iso ? date(iso) : "…";
    return { eq: d(f.from), from: "от " + d(f.from), to: "до " + d(f.to), range: !f.to ? "с " + d(f.from) : !f.from ? "по " + d(f.to) : `${d(f.from)} – ${d(f.to)}` }[f.mode];
  };
  const filterSet = (v) => Array.isArray(v) ? v.length > 0 : v && typeof v === "object" ? dateFilterSet(v) : !!(v || "").trim();
  const filterText = (v) => Array.isArray(v) ? v.join(", ") : v && typeof v === "object" ? dateFilterLabel(v) : (v || "").trim();
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
      showList(list, btn);
    };
    sync();
    return sync;
  }

  // Открыть выпадающую панель фильтра под кнопкой (в <body>, позиция — относительно окна).
  function showList(list, btn) {
    const r = btn.getBoundingClientRect();
    list.style.top = `${r.bottom + 2}px`;
    list.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - 228))}px`;
    document.body.append(list);
    openList = list;
  }

  // Фильтр даты: конкретная дата, период, «от» или «до».
  const DATE_MODES = [["eq", "Дата"], ["range", "Период"], ["from", "От"], ["to", "До"]];
  function bindDateFilter(wrap, col, filters, render) {
    const btn = wrap.querySelector("button");
    const list = document.createElement("div");
    list.className = "multi-list date-filter";
    list.owner = btn;
    // Крестик в поле активного фильтра (левее «▾») — сбросить фильтр.
    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "filter-clear hidden";
    clear.textContent = "×";
    clear.title = "Сбросить фильтр";
    clear.setAttribute("aria-label", `Сбросить фильтр: ${col.title}`);
    wrap.classList.add("has-clear");
    wrap.append(clear);
    clear.onclick = () => {
      delete filters[col.id];
      if (openList === list) closeList();
      sync();
      render();
    };
    const sync = () => {
      const set = filterSet(filters[col.id]);
      btn.innerHTML = `<span class="multi-label">${esc(dateFilterLabel(filters[col.id]))}</span><span class="multi-arrow">▾</span>`;
      btn.title = set ? filterText(filters[col.id]) : "";
      btn.classList.toggle("set", set);
      clear.classList.toggle("hidden", !set);
    };
    btn.onclick = () => {
      if (openList === list) { closeList(); return; }
      if (openList) closeList();
      const f = filters[col.id] || { mode: "eq", from: "", to: "" };
      list.innerHTML = `<select data-mode aria-label="Что выбрать">${DATE_MODES.map(([v, t]) =>
        `<option value="${v}" ${v === f.mode ? "selected" : ""}>${t}</option>`).join("")}</select>
        <label data-from><span></span><input type="date" value="${esc(f.from || "")}"></label>
        <label data-to><span></span><input type="date" value="${esc(f.to || "")}"></label>`;
      const mode = list.querySelector("[data-mode]");
      const [fromIn, toIn] = ["[data-from] input", "[data-to] input"].map((s) => list.querySelector(s));
      const layout = () => {
        const m = mode.value;
        list.querySelector("[data-from]").classList.toggle("hidden", m === "to");
        list.querySelector("[data-to]").classList.toggle("hidden", m === "eq" || m === "from");
        list.querySelector("[data-from] > span").textContent = { eq: "Дата", range: "С", from: "От" }[m] || "";
        list.querySelector("[data-to] > span").textContent = m === "range" ? "По" : "До";
      };
      const apply = () => {
        const v = { mode: mode.value, from: fromIn.value, to: toIn.value };
        if (dateFilterSet(v)) filters[col.id] = v; else delete filters[col.id];
        sync();
        render();
      };
      mode.onchange = () => { layout(); apply(); };
      [fromIn, toIn].forEach((i) => i.addEventListener("input", apply));
      layout();
      showList(list, btn);
      enhanceDatesIn(list);
    };
    sync();
    return sync;
  }

  // filters — общий объект страницы: значения фильтров переживают перезагрузку данных.
  function makeTable(container, columns, { storageKey, empty, filters = {}, rowClass, rowId, extraFilter, defaultSort = null, counterId = "rows-count" }) {
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
      const counter = document.getElementById(counterId);
      if (counter) counter.textContent = rows.length ? `Строк: ${visible.length}${visible.length !== rows.length ? " из " + rows.length : ""}` : "";
      container.querySelectorAll("th[data-id]").forEach((th) => {
        th.querySelector(".arrow").textContent = sort && sort.id === th.dataset.id ? (sort.dir > 0 ? "▲" : "▼") : "";
      });
    }

    container.innerHTML = `<table><thead>
      <tr>${columns.map((c) => `<th class="${(c.cls || "").replace(/\b(num|icon|name|actions|comment)\b/g, "")} ${c.sortable !== false ? "sortable" : ""} ${c.numeric ? "num" : ""}"
        ${c.sortable !== false ? `data-id="${c.id}"` : ""}>${esc(c.title)}<span class="arrow"></span></th>`).join("")}</tr>
      <tr class="filters">${columns.map((c) => `<th class="${(c.cls || "").replace(/\b(num|icon|name|actions|comment)\b/g, "")}">${
        c.filter === "list" || c.filter === "date" ? `<div class="multi" data-${c.filter}="${c.id}"><button type="button" aria-label="Фильтр: ${esc(c.title)}"></button></div>`
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
    const syncLists = [...container.querySelectorAll("tr.filters .multi")].map((wrap) => wrap.dataset.date
      ? bindDateFilter(wrap, columns.find((c) => c.id === wrap.dataset.date), filters, render)
      : bindListFilter(wrap, columns.find((c) => c.id === wrap.dataset.list), filters, render));
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
    toast("Сначала добавьте аккаунт Steam (кнопка «Список аккаунтов» на вкладке «Портфолио»)", true);
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
    // Вкладки страницы: «Текущий портфель», «Закрытые позиции», «Ордера» (выбранная — в адресе: #closed, #orders).
    const closed = { checked: false };
    const orders = ordersPage({ summary: "o-summary", table: "o-table", counter: "o-rows-count",
      btnOrder: "btn-order" });
    function showPane() {
      const pane = ["closed", "orders", "rates"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "positions";
      document.querySelectorAll("#subtabs a").forEach((a) => a.classList.toggle("active", a.dataset.pane === pane));
      document.getElementById("pane-positions").classList.toggle("hidden", pane === "orders" || pane === "rates");
      document.getElementById("pane-orders").classList.toggle("hidden", pane !== "orders");
      document.getElementById("pane-rates").classList.toggle("hidden", pane !== "rates");
      document.getElementById("btn-accounts").classList.toggle("hidden", pane === "rates");  // к курсам аккаунты не относятся
      if (pane === "orders") { orders.reload(); return; }
      if (pane === "rates") { ratesPane().catch((e) => toast(e.message, true)); return; }
      closed.checked = pane === "closed";
      document.getElementById("btn-buy").classList.toggle("hidden", closed.checked);
      document.getElementById("collapse-toggle").classList.toggle("hidden", closed.checked);
      document.getElementById("edit-hint").textContent = closed.checked
        ? "Строка — одна продажа. Двойной щелчок по количеству, цене или дате продажи — изменить продажу; портфель и статистика пересчитаются."
        : "Строка — одна покупка. Двойной щелчок по количеству, цене, дате покупки, валюте или аккаунту — изменить покупку.";
      reload();
    }
    window.addEventListener("hashchange", showPane);
    collapse.checked = store.get("collapse", "0") === "1";
    const filters = {};
    let table;

    const columns = () => closed.checked ? closedColumns() : openColumns();
    // «Закрытые позиции»: строка на каждую продажу. Количество, цена и дата продажи правятся двойным щелчком.
    const closedColumns = () => [
      { id: "icon", title: "", cls: "icon sticky-1", sortable: false, html: (r) => icon(r.icon) },
      { id: "item", title: "Название", key: "item", cls: "name sticky-2", filter: true, text: (r) => r.item + " " + r.type,
        html: (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.item)}</a><span class="type">${esc(r.type)}</span>` },
      { id: "quantity", title: "Продано, шт.", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0),
        bind: (td, r) => bindSaleEdit(td, r, "quantity") },
      { id: "avg", title: "Цена покупки", key: "avg_price", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="в USD по курсу на дату покупки: ${money(r.avg_price_usd, "USD", true)}">${money(r.avg_price, r.currency, true)}</span>` },
      { id: "sale_price", title: "Цена продажи", key: "sale_price", cls: "num", numeric: true, filter: true,
        html: (r) => money(r.sale_price, r.currency), bind: (td, r) => bindSaleEdit(td, r, "price") },
      { id: "first", title: "Дата покупки", key: "first_buy_date", filter: "date", text: (r) => date(r.first_buy_date), html: (r) => date(r.first_buy_date) },
      { id: "sale_date", title: "Дата продажи", key: "sale_date", filter: "date", text: (r) => date(r.sale_date), html: (r) => date(r.sale_date),
        bind: (td, r) => bindSaleEdit(td, r, "date") },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency },
      { id: "profit", title: "Прибыль", key: "profit", cls: "num", numeric: true, filter: true,
        html: (r) => `<span class="${r.profit > 0 ? "up" : r.profit < 0 ? "down" : ""}" title="В USD по курсам дат: ${money(r.profit_usd, "USD")}">${r.profit > 0 ? "+" : ""}${money(r.profit, r.currency)}</span>` },
      { id: "profit_pct", title: "% прибыли", key: "profit_pct", cls: "num", numeric: true, filter: true, html: (r) => pct(r.profit_pct) },
      { id: "annual_pct", title: "% годовых", key: "annual_pct", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="За ${num(r.days, 0)} дн. владения">${pct(r.annual_pct)}</span>` },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames, values: (r) => [r.account],
        html: (r) => esc(r.account) },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true,
        html: (r) => `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`,
        bind: (td, r) => bindComment(td, r, r.op_id) },  // комментарий продажи
      // Из какой позиции «Текущего портфеля» продано (не правится).
      { id: "position_no", title: "ID позиции", key: "position_no", cls: "num center", numeric: true, filter: true,
        html: (r) => r.position_no ?? "" },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => `<button class="small danger icon-btn" title="Удалить продажу" aria-label="Удалить продажу">${TRASH_ICON}</button>`,
        bind: (td, r) => { td.querySelector("button").onclick = () => deleteSaleForm(r); } },
    ];
    // «Текущий портфель»: строка на каждую покупку. «Объединить строки» — итоги по предмету и валюте, только просмотр.
    const openColumns = () => [
      { id: "icon", title: "", cls: "icon sticky-1", sortable: false, html: (r) => icon(r.icon) },
      { id: "item", title: "Название", key: "item", cls: "name sticky-2", filter: true, text: (r) => r.item + " " + r.type,
        html: (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.item)}</a><span class="type">${esc(r.type)}</span>${
          collapse.checked && r.lots > 1 ? `<span class="type">${r.lots} покупки</span>` : ""}` },
      { id: "quantity", title: "Кол-во", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0),
        bind: (td, r) => bindEdit(td, r, "quantity") },
      { id: "avg", title: collapse.checked ? "Средняя цена" : "Цена покупки", key: "avg_price", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="${r.avg_price_usd != null ? "в USD по курсу на дату покупки: " + money(r.avg_price_usd, "USD", true) : ""}">${money(r.avg_price, r.currency, true)}</span>`,
        bind: (td, r) => bindEdit(td, r, "price") },
      { id: "first", title: collapse.checked ? "Первая покупка" : "Дата покупки", key: "first_buy_date", filter: "date",
        text: (r) => date(r.first_buy_date), html: (r) => date(r.first_buy_date), bind: (td, r) => bindEdit(td, r, "date") },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency,
        bind: (td, r) => bindEdit(td, r, "currency") },
      { id: "price", title: "Текущая цена", key: "price", cls: "num", numeric: true, filter: true,
        html: (r) => r.price_stale
          ? `<span class="stale" title="Цена устарела: снята ${esc(dateTime(r.price_at))}. Обновится при ближайшем сборе цен (раз в 6 часов)">${money(r.price, r.currency)} ⏱</span>`
          : `<span title="Лучший ордер на покупку, снят ${esc(dateTime(r.price_at))}">${money(r.price, r.currency)}</span>` },
      { id: "pct_avg", title: collapse.checked ? "% к средней" : "% к цене покупки", key: "pct_avg", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_avg) },
      { id: "pct_1d", title: "% сутки", key: "pct_1d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_1d) },
      { id: "pct_7d", title: "% неделя", key: "pct_7d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_7d) },
      { id: "pct_30d", title: "% месяц", key: "pct_30d", cls: "num", numeric: true, filter: true, html: (r) => pct(r.pct_30d) },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames,
        values: (r) => r.account_id != null ? [r.account] : r.accounts.map((a) => a.name), html: (r) => esc(r.account),
        bind: (td, r) => bindEdit(td, r, "account_id") },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true,
        html: (r) => collapse.checked ? esc(r.comment)
          : `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`,
        bind: (td, r) => !collapse.checked && bindComment(td, r) },
      // «ID позиции» — номер строки; у объединённых строк его нет, колонка скрыта.
      ...(collapse.checked ? [] : [{ id: "position_no", title: "ID позиции", key: "position_no", cls: "num center", numeric: true,
        filter: true, html: (r) => r.position_no ?? "" }]),
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => collapse.checked ? "" : '<button class="small" data-a="sell">Продано</button><button class="small" data-a="move">Перенести</button>'
          + `<button class="small danger icon-btn" data-a="remove" title="Удалить позицию" aria-label="Удалить позицию">${TRASH_ICON}</button>`,
        bind: (td, r) => {
          const actions = { sell: sellForm, move: moveForm, remove: removeForm };
          td.querySelectorAll("button").forEach((b) => b.onclick = () => actions[b.dataset.a](r));
        } },
    ];

    // ---------- правка двойным щелчком ----------
    // Строка портфеля — одна покупка: правка меняет саму покупку (цена, дата, валюта, аккаунт, количество).
    // В ячейке «Кол-во» — остаток; если из строки уже продавали или переносили, в покупке их тоже учтём.
    const COLLAPSED_HINT = "Строки объединены — чтобы изменить покупку, снимите «Объединить строки»";

    function bindEdit(td, r, field) {
      td.classList.add("editable");
      td.title = td.title || "Двойной щелчок — изменить";
      td.ondblclick = (e) => {
        e.preventDefault();
        if (!r.single_op) { toast(COLLAPSED_HINT, true); return; }
        const b = r.single_op;
        const sold = r.bought - r.quantity;  // ушло из строки продажами и переносами
        editInCell(td, {
          values: { quantity: r.quantity, price: b.price, date: b.date, currency: b.currency, account_id: b.account_id },
          accounts: accountChoices(b),
          save: async (changes) => {
            if (r.has_sales && !(await confirmSoldEdit(r, field))) { table.render(); return; }
            await saveEdit(b.op_id, "quantity" in changes ? { ...changes, quantity: changes.quantity + sold } : changes,
              "Покупка изменена");
          },
        }, field);
      };
    }

    // Из строки уже продавали: правка количества, цены или даты покупки меняет и «Закрытые позиции» — переспросим.
    function confirmSoldEdit(r, field) {
      const head = `По данной позиции с ID ${r.position_no} уже есть продажи на вкладке «Закрытые позиции». `;
      const what = { price: "Цена покупки", date: "Дата покупки" }[field];
      if (field !== "quantity" && !what) return Promise.resolve(true);
      return confirmDialog("Позиция уже частично продана", field === "quantity"
        ? head + "Вы уверены, что с учётом проданного общее количество закупленного будет верным?"
        : head + `Если продолжить, ${what} на вкладке «Закрытые позиции» изменится, статистика пересчитается, продолжаем?`, "Да");
    }

    // Продажа в «Закрытых позициях»: количество, цена и дата продажи. Портфель и статистика пересчитываются.
    function bindSaleEdit(td, r, field) {
      td.classList.add("editable");
      td.title = td.title || "Двойной щелчок — изменить продажу";
      td.ondblclick = (e) => {
        e.preventDefault();
        editInCell(td, {
          values: { quantity: r.quantity, price: r.sale_price, date: r.sale_date },
          save: (changes) => saveEdit(r.op_id, changes, "Продажа изменена — портфель пересчитан"),
        }, field);
      };
    }

    // Активные аккаунты и текущий аккаунт покупки (даже если он скрыт) — чтобы список не подменил его молча.
    const accountChoices = (b) => meta.accounts.filter((a) => !a.hidden || a.id === b.account_id);

    async function saveEdit(opId, changes, done) {
      await api("POST", `/api/ops/${opId}/edit`, changes);
      toast(done);
      await reload();
    }

    function editInCell(td, { values, accounts = [], save }, field) {
      const shown = (f) => f === "price" ? String(values.price).replace(".", ",") : values[f];
      const control = field === "currency" || field === "account_id" ? document.createElement("select") : document.createElement("input");
      if (field === "currency") control.innerHTML = currencyOptions().map((o) => `<option value="${esc(o.value)}">${esc(o.value)}</option>`).join("");
      if (field === "account_id") control.innerHTML = accounts.map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
      if (field === "date") { control.type = "date"; control.max = today(); }
      if (field === "quantity") { control.type = "number"; control.min = 1; control.step = 1; }
      if (field === "price") control.inputMode = "decimal";
      control.value = shown(field);
      control.className = "cell-editor";
      td.ondblclick = null;
      td.replaceChildren(control);
      // Дата — поле «дд.мм.гггг»: блоки день/месяц/год правятся по отдельности, календарь — кнопкой.
      const input = field === "date" ? enhanceDate(control) : control;
      input.focus();
      if (field === "date") input._pick(0); else if (input.select) input.select();
      let done = false;
      const finish = async (commit) => {
        if (done) return;
        done = true;
        const value = control.value.trim();
        if (!commit || value === "" || String(value) === String(shown(field))) { table.render(); return; }
        try {
          await save({ [field]: field === "quantity" || field === "account_id" ? +value : value });
        } catch (e) {
          toast(e.message + (e.violations && e.violations.length ? ": " + e.violations.join("; ") : ""), true);
          table.render();
        }
      };
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
      });
      // Пока открыт календарь, текст может терять фокус — правку закончит выбор даты или уход из поля потом.
      input.addEventListener("blur", () => setTimeout(() => {
        if (document.activeElement !== input && !input._pickerOpen) finish(true);
      }, 0));
      input.addEventListener("datepicked", () => finish(true));
      if (control.tagName === "SELECT") control.addEventListener("change", () => finish(true));
    }

    // Комментарий строки — комментарий её покупки (в «Закрытых позициях» — продажи): opId — что правим.
    function bindComment(td, r, opId = r.lot_id) {
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
              await api("POST", `/api/ops/${opId}/edit`, { comment: input.value });
              r.comment = input.value.trim();
            } catch (e) { toast(e.message, true); }
          }
          td.innerHTML = `<span class="comment-edit ${r.comment ? "" : "empty"}" tabindex="0">${esc(r.comment)}</span>`;
          bindComment(td, r, opId);
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
      document.getElementById("summary").innerHTML = closed.checked ? realizedTile(s)
        : holdingTiles(s, (n) => `без цены: ${n} — учтены по себестоимости`);
      // Продажи всегда построчно — «Объединить строки» к ним не относится.
      collapse.disabled = closed.checked;
      table = makeTable(document.getElementById("table"), columns(), {
        storageKey: closed.checked ? "closed" : "portfolio",
        filters,
        defaultSort: closed.checked ? { id: "sale_date", dir: -1 } : null,
        empty: closed.checked ? "Продаж пока нет — они появятся после кнопки «Продано»" : "Портфель пуст — нажмите «Добавить закупленное»",
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

    const rowLabel = (r) => `${esc(r.item)} · ${esc(r.account)} · покупка от ${date(r.first_buy_date)} по ${money(r.avg_price, r.currency, true)}`;

    // Продажа — из этой строки (покупки): при удалении продажи штуки вернутся сюда же.
    function sellForm(r) {
      openForm({
        title: `Продано: ${r.item}`,
        submit: "Записать продажу",
        info: true,
        layout: [
          { name: "_row", type: "static", label: "", required: false, html: `<span class="muted small">${rowLabel(r)}</span>` },
          [{ name: "quantity", label: `Количество (есть ${num(r.quantity, 0)})`, type: "number", attrs: `min="1" max="${r.quantity}" step="1"`, value: r.quantity },
            { name: "price", label: `Чистая цена за штуку после комиссии, ${r.currency}`, inputmode: "decimal", attrs: 'pattern="[0-9 ]+([.,][0-9]{1,2})?"' }],
          { name: "date", label: "Дата продажи", type: "date", value: today(), attrs: `max="${today()}"` },
          fxField,
          // Свой комментарий продажи — он будет в «Закрытых позициях»; комментарий строки портфеля не переносится.
          { name: "comment", label: "Комментарий к продаже", type: "textarea", required: false },
        ],
        onInput: (ctx) => {
          const price = () => parseNum(ctx.el("price").value);
          const qty = () => parseInt(ctx.el("quantity").value, 10);
          ctx.fx = ctx.fx || fxBlock(ctx, {
            currency: () => r.currency,
            day: () => ctx.el("date").value || today(),
            amount: () => price() != null && qty() ? price() * qty() : null,
            extra: () => price() != null && qty()
              ? `<br>Прибыль: <b class="${price() >= r.avg_price ? "up" : "down"}">${money((price() - r.avg_price) * qty(), r.currency)}</b> <span class="muted">(цена покупки ${money(r.avg_price, r.currency, true)})</span>` : "",
          });
          ctx.fx();
        },
        onSubmit: async (v) => {
          await api("POST", "/api/sell", { lot_id: r.lot_id, quantity: +v.quantity, price: v.price, date: v.date,
            fx_rate: v.fx_rate || null, comment: v.comment || "" });
          toast(`Продажа записана: ${r.item}, ${v.quantity} шт.`);
          await reload();
        },
      });
    }

    function moveForm(r) {
      if (!accountOptions(r.account_id).length) { toast("Нет другого аккаунта — добавьте его кнопкой «Список аккаунтов»", true); return; }
      openForm({
        title: `Перенести на другой аккаунт: ${r.item}`,
        submit: "Перенести",
        layout: [
          { name: "_row", type: "static", label: "", required: false, html: `<span class="muted small">${rowLabel(r)}</span>` },
          { name: "to_account_id", label: "На аккаунт", type: "select", options: accountOptions(r.account_id) },
          { name: "quantity", label: `Количество (есть ${num(r.quantity, 0)})`, type: "number", attrs: `min="1" max="${r.quantity}" step="1"`, value: r.quantity },
          // Комментарий исходной строки по умолчанию переезжает в новую строку.
          { name: "comment", label: "Комментарий", type: "textarea", required: false, value: r.comment || "" },
          { name: "_note", type: "static", label: "", required: false,
            html: '<span class="muted small">На новом аккаунте появится строка — покупка по той же цене и с той же датой покупки, её можно править, как любую. «Вложено» и прибыль не меняются.</span>' },
        ],
        onSubmit: async (v) => {
          await api("POST", "/api/transfer", { lot_id: r.lot_id, to_account_id: +v.to_account_id, quantity: +v.quantity,
            comment: v.comment });
          toast(`Перенесено: ${r.item}, ${v.quantity} шт.`);
          await reload();
        },
      });
    }

    // Удаление позиции как ошибочно внесённой: остаток строки стирается из её покупки, прибыли нет.
    function removeForm(r) {
      openForm({
        title: `Удалить позицию: ${r.item}`,
        submit: "Удалить позицию",
        layout: [{ name: "_note", type: "static", label: "", required: false, html: `<div class="info">
          <p>Будет удалено <b>${num(r.quantity, 0)} шт.</b> <b>${esc(r.item)}</b> с аккаунта <b>${esc(r.account)}</b>
          в валюте <b>${esc(r.currency)}</b> — как ошибочно внесённые.</p>
          <p>Объём вложенных средств уменьшится на <b>${money(r.accounts[0].cost, r.currency)}</b>.<br>Прошлые продажи не изменятся.</p></div>` }],
        onSubmit: async () => {
          await api("POST", "/api/remove-position", { lot_id: r.lot_id });
          toast(`Позиция удалена: ${r.item}`);
          await reload();
        },
      });
    }

    // Удаление строки «Закрытых позиций» = удаление продажи: штуки возвращаются в строку, из которой проданы.
    function deleteSaleForm(r) {
      openForm({
        title: `Удалить продажу: ${r.item}`,
        submit: "Удалить и вернуть в портфолио",
        layout: [{ name: "_note", type: "static", label: "", required: false, html: `<div class="info">
          <p><b>В портфолио вернётся:</b> ${num(r.quantity, 0)} шт. <b>${esc(r.item)}</b>
          в строку покупки от ${date(r.first_buy_date)} по ${money(r.avg_price, r.currency, true)} на аккаунте <b>${esc(r.account)}</b>.</p>
          <p>Продажа от ${date(r.sale_date)} по ${money(r.sale_price, r.currency)} будет удалена, прибыль по ней
          (${r.profit > 0 ? "+" : ""}${money(r.profit, r.currency)}) исчезнет из статистики.<br>
          <span class="muted small">Продажа удаляется насовсем — вернуть её можно, только внеся заново.</span></p></div>` }],
        onSubmit: async () => {
          await api("POST", `/api/ops/${r.op_id}/delete`);
          toast(`Продажа удалена, ${r.item} — снова в портфолио`);
          await reload();
        },
      });
    }

    collapse.addEventListener("change", () => { store.set("collapse", collapse.checked ? "1" : "0"); reload(); });
    document.getElementById("btn-buy").onclick = () => requireAccounts(buyForm);
    // «Список аккаунтов» — один на всю страницу: после правок обновляются и портфель, и ордера.
    document.getElementById("btn-accounts").onclick = () => accountsDialog(() => Promise.all([reload(), orders.reload()]));
    loadMeta().then(showPane).catch((e) => toast(e.message, true));
  }

  // ---------- вкладка «Курс валют» страницы «Портфолио» ----------

  async function ratesPane() {
    const data = await api("GET", "/api/rates");
    const el = (id) => document.getElementById(id);
    el("rates-time").textContent = data.taken_at ? "Рассчитан: " + dateTime(data.taken_at) : "";
    if (!data.rates.length) {
      el("rates-table").innerHTML = '<div class="empty-state">Курсов пока нет — они появятся после первого сбора цен</div>';
      return;
    }
    // Лоты, из которых посчитан курс, пользователю не показываются (они есть в /api/rates и в `steamfolio report`).
    const rate = (v) => Number(v).toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
    el("rates-table").innerHTML = `<table><thead><tr><th>Валюта</th><th class="num">За $1</th></tr></thead><tbody>
      ${data.rates.map((r) => `<tr><td><b>${esc(r.code)}</b>, ${esc(r.symbol)} <span class="muted">— ${esc(r.name)}</span></td>
        <td class="num"><b>${rate(r.rate)} ${esc(r.symbol)}</b></td></tr>`).join("")}
      </tbody></table>`;
  }

  // ---------- вкладка «Ордера» страницы «Портфолио» ----------

  function ordersPage(ids) {
    let table;
    const columns = [
      { id: "icon", title: "", cls: "icon sticky-1", sortable: false, html: (r) => icon(r.icon) },
      { id: "item", title: "Название", key: "item", cls: "name sticky-2", filter: true,
        html: (r) => `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.item)}</a>` },
      { id: "quantity", title: "Кол-во", key: "quantity", cls: "num", numeric: true, filter: true, html: (r) => num(r.quantity, 0),
        bind: (td, r) => bindOrderEdit(td, r, "quantity") },
      { id: "price", title: "Цена за шт.", key: "price", cls: "num", numeric: true, filter: true, html: (r) => money(r.price, r.currency),
        bind: (td, r) => bindOrderEdit(td, r, "price") },
      { id: "total", title: "Сумма", key: "total", cls: "num", numeric: true, filter: true, html: (r) => money(r.total, r.currency) },
      { id: "currency", title: "Валюта", key: "currency", filter: true, html: (r) => r.currency,
        bind: (td, r) => bindOrderEdit(td, r, "currency") },
      { id: "market", title: "Лучший ордер на площадке", key: "market_best", cls: "num", numeric: true, filter: true,
        html: (r) => `<span title="снят ${esc(dateTime(r.market_at))}">${money(r.market_best, r.currency)}</span>${r.outbid ? '<span class="badge">перебит</span>' : ""}` },
      { id: "account", title: "Аккаунт", key: "account", filter: "list", options: accountNames, values: (r) => [r.account],
        html: (r) => esc(r.account), bind: (td, r) => bindOrderEdit(td, r, "account_id") },
      { id: "comment", title: "Комментарий", key: "comment", cls: "comment", filter: true, html: (r) => esc(r.comment),
        bind: (td, r) => bindOrderEdit(td, r, "comment") },
      { id: "actions", title: "", cls: "actions", sortable: false,
        html: () => '<button class="small" data-a="all">Куплено всё</button><button class="small" data-a="part">Частично</button>'
          + `<button class="small danger icon-btn" data-a="remove" title="Убрать ордер" aria-label="Убрать ордер">${TRASH_ICON}</button>`,
        bind: (td, r) => td.querySelectorAll("button").forEach((b) => b.onclick = () => ({
          all: () => fillForm(r, false), part: () => fillForm(r, true), remove: () => removeOrder(r),
        })[b.dataset.a]()) },
    ];

    // Правка ордера двойным щелчком по ячейке — как в портфеле (перевыставление).
    function bindOrderEdit(td, r, field) {
      td.classList.add("editable");
      td.title = td.title || "Двойной щелчок — изменить";
      td.ondblclick = (e) => { e.preventDefault(); editOrderCell(td, r, field); };
    }

    const orderValue = (r, field) => field === "price" ? String(r.price).replace(".", ",") : r[field] ?? "";

    function editOrderCell(td, r, field) {
      const control = document.createElement(field === "currency" || field === "account_id" ? "select" : "input");
      if (field === "currency") control.innerHTML = currencyOptions().map((o) => `<option value="${esc(o.value)}">${esc(o.value)}</option>`).join("");
      if (field === "account_id") {
        control.innerHTML = meta.accounts.filter((a) => !a.hidden || a.id === r.account_id)
          .map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
      }
      if (field === "quantity") { control.type = "number"; control.min = 1; control.step = 1; }
      if (field === "price") control.inputMode = "decimal";
      control.value = orderValue(r, field);
      control.className = "cell-editor";
      td.ondblclick = null;
      td.replaceChildren(control);
      control.focus();
      if (control.select) control.select();
      let done = false;
      const finish = async (commit) => {
        if (done) return;
        done = true;
        const value = control.value.trim();
        const empty = value === "" && field !== "comment";  // комментарий можно стереть
        if (!commit || empty || value === String(orderValue(r, field))) { table.render(); return; }
        const body = { price: String(r.price), quantity: r.quantity, comment: r.comment || "" };
        body[field] = field === "quantity" || field === "account_id" ? +value : value;
        try {
          await api("POST", `/api/orders/${r.order_id}/change`, body);
          toast("Ордер перевыставлен");
          await reload();
        } catch (e) {
          toast(e.message + (e.violations && e.violations.length ? ": " + e.violations.join("; ") : ""), true);
          table.render();
        }
      };
      control.addEventListener("keydown", (e) => {
        if (e.key === "Enter") finish(true);
        if (e.key === "Escape") finish(false);
      });
      control.addEventListener("blur", () => finish(true));
      if (control.tagName === "SELECT") control.addEventListener("change", () => finish(true));
    }

    async function reload() {
      const data = await api("GET", "/api/orders");
      document.getElementById(ids.summary).innerHTML = statTile("Зарезервировано в ордерах",
        money(data.summary.reserved_usd, "USD"), { extra: '<div class="small muted">по текущему курсу Steam</div>' });
      table = table || makeTable(document.getElementById(ids.table), columns,
        { storageKey: "orders", empty: "Выставленных ордеров нет", counterId: ids.counter });
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
          ...(r.comment ? [{ name: "_note", type: "static", label: "", required: false,
            html: `<span class="muted small">Комментарий ордера «${esc(r.comment)}» допишется в комментарий строки в портфеле.</span>` }] : []),
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

    async function removeOrder(r) {
      const ok = await confirmDialog("Убрать ордер?", `${r.item}: ${r.quantity} шт. по ${money(r.price, r.currency)}. Ордер удаляется насовсем.`, "Убрать");
      if (!ok) return;
      try {
        await api("POST", `/api/orders/${r.order_id}/remove`);
        toast("Ордер убран");
        await reload();
      } catch (e) { toast(e.message, true); }
    }

    document.getElementById(ids.btnOrder).onclick = () => requireAccounts(orderForm);
    return { reload: () => reload().catch((e) => toast(e.message, true)) };
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
    const signed = signedUsd;
    let data, lineChart, pieChart;
    let period = +store.get("stats.period", "0");
    const DAY = 86400000;
    let panEnd = null;  // конец окна графика, сдвинутый сектором; null — по сегодняшний день
    let zoomSpan = null;  // ширина участка, выделенного двумя щелчками; null — ширина выбранного периода
    let zoomStart = null, zoomHover = null;  // первая граница выделения и где сейчас курсор
    let nav = null;  // полоса с сектором: {first, last, span, clampEnd, fmtDay}, null — листать некуда

    const inPlot = (chart, evt) => {
      const a = chart.chartArea;
      return evt.x >= a.left && evt.x <= a.right && evt.y >= a.top && evt.y <= a.bottom;
    };

    // Выделение участка: первый щелчок — вертикальная линия, второй — вторая граница; график сужается до них.
    function pickZoomEdge(chart, evt) {
      if (!inPlot(chart, evt)) return;
      const x = chart.scales.x.getValueForPixel(evt.x);
      if (zoomStart == null) {
        zoomStart = x;
        chart.draw();
        return;
      }
      const [a, b] = [Math.min(zoomStart, x), Math.max(zoomStart, x)];
      zoomStart = zoomHover = null;
      if (b - a < 3600000) { chart.draw(); return; }  // щелчок в ту же точку — отмена
      zoomSpan = b - a;
      panEnd = b;
      renderLine();
    }

    const zoomMarker = {
      id: "zoomMarker",
      afterDatasetsDraw(chart) {
        if (zoomStart == null) return;
        const { ctx, chartArea: area, scales: { x } } = chart;
        const px = (v) => Math.min(Math.max(x.getPixelForValue(v), area.left), area.right);
        ctx.save();
        if (zoomHover != null) {
          ctx.fillStyle = alpha(css("--accent"), 0.12);
          const [l, r] = [px(zoomStart), px(zoomHover)].sort((p, q) => p - q);
          ctx.fillRect(l, area.top, r - l, area.bottom - area.top);
        }
        ctx.strokeStyle = css("--accent");
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(px(zoomStart), area.top);
        ctx.lineTo(px(zoomStart), area.bottom);
        ctx.stroke();
        ctx.restore();
      },
    };

    // Сдвинуть окно графика так, чтобы оно кончалось в end; сектор на полосе — следом.
    function moveWindow(end) {
      if (!nav) return;
      end = nav.clampEnd(end);
      panEnd = end >= nav.last ? null : end;  // у правого края окно снова «прилипает» к сегодня
      const total = nav.last - nav.first;
      Object.assign(lineChart.options.scales.x, { min: end - nav.span, max: end });
      lineChart.update("none");
      const win = el("nav-window");
      win.style.left = `${(end - nav.span - nav.first) / total * 100}%`;
      win.style.width = `${nav.span / total * 100}%`;
      win.setAttribute("aria-valuetext", `${nav.fmtDay(end - nav.span)} — ${nav.fmtDay(end)}`);
      el("nav-from").textContent = nav.fmtDay(nav.first);
      el("nav-to").textContent = nav.fmtDay(nav.last);
      el("nav-label").textContent = `${nav.fmtDay(end - nav.span)} — ${nav.fmtDay(end)}`;
    }

    // Сектор тащат мышью или пальцем; щелчок по полосе мимо сектора ставит сектор серединой в эту точку.
    (function bindNav() {
      const track = el("nav-track"), win = el("nav-window");
      const msPerPx = () => (nav.last - nav.first) / track.clientWidth;
      let drag = null;
      win.addEventListener("pointerdown", (e) => {
        if (!nav) return;
        e.preventDefault();
        try { win.setPointerCapture(e.pointerId); } catch { /* указатель уже отпущен */ }
        drag = { x: e.clientX, end: lineChart.options.scales.x.max };
        win.classList.add("dragging");
      });
      win.addEventListener("pointermove", (e) => {
        if (drag) moveWindow(drag.end + (e.clientX - drag.x) * msPerPx());
      });
      const stop = () => { drag = null; win.classList.remove("dragging"); };
      win.addEventListener("pointerup", stop);
      win.addEventListener("pointercancel", stop);
      track.addEventListener("pointerdown", (e) => {
        if (!nav || e.target !== track) return;
        const at = nav.first + (e.clientX - track.getBoundingClientRect().left) * msPerPx();
        moveWindow(at + nav.span / 2);
      });
      win.addEventListener("keydown", (e) => {
        const step = { ArrowLeft: -DAY, ArrowRight: DAY, PageUp: -nav.span, PageDown: nav.span }[e.key];
        if (nav && step) { e.preventDefault(); moveWindow(lineChart.options.scales.x.max + step); }
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && zoomStart != null) { zoomStart = zoomHover = null; lineChart?.draw(); }
      });
    })();
    let chartCurrency = store.get("stats.currency", "TOTAL");
    let bestSort = store.get("stats.best", "pct");
    let drill = null;

    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.color = css("--muted");
    Chart.defaults.borderColor = css("--border");

    function renderSummary() {
      const s = data.summary;
      el("summary-now").innerHTML = holdingTiles(s, (n) => `без цены: ${n}`);
      el("summary-all").innerHTML = statTile("Всего вложено", money(s.total_invested_usd, "USD"), { tip: TIPS.totalInvested })
        + statTile("Всего продано", money(s.total_sold_usd, "USD"), { tip: TIPS.totalSold })
        + realizedTile(s);
    }

    function renderLine() {
      zoomStart = zoomHover = null;  // график перерисован — начатое выделение не продолжается
      const s = data.series;
      const cur = s.currency || "USD";
      document.querySelectorAll("#period button").forEach((b) => b.classList.toggle("active", !zoomSpan && +b.dataset.days === period));
      const axis = (v) => cur === "USD" ? "$" + num(v, 0) : `${num(v, 0)} ${SIGN[cur] || cur}`;
      // Оценка портфеля лежит слоем поверх реализованной прибыли: y = оценка + прибыль на этот момент,
      // настоящее значение — в v (его показывает подсказка). «Вложено» — отдельная линия без заливки.
      const profit = (s.profit || []).some(([, y]) => y) ? s.profit : [];
      // Прибыль между точками — по прямой, как и её линия: слой оценки ложится ровно на неё.
      const profitAt = (x) => {
        if (!profit.length || x < profit[0][0]) return 0;
        for (let i = 1; i < profit.length; i++) {
          const [x1, y1] = profit[i];
          if (x1 < x) continue;
          const [x0, y0] = profit[i - 1];
          return x1 === x0 ? y1 : y0 + (y1 - y0) * (x - x0) / (x1 - x0);
        }
        return profit[profit.length - 1][1];
      };
      const ds = (label, points, color, extra = {}, stacked = false) => ({
        label, data: points.map(([x, v]) => ({ x, y: stacked ? v + profitAt(x) : v, v })),
        borderColor: color, backgroundColor: alpha(color, 0.18),
        // Одна точка (например, всё куплено сегодня) — линии ещё нет, рисуем саму точку.
        fill: "origin", pointRadius: points.length === 1 ? 4 : 0, pointBackgroundColor: color,
        pointHoverRadius: points.length === 1 ? 5 : 3, borderWidth: 2, tension: 0, ...extra,
      });
      const onProfit = profit.length ? { target: 0 } : "origin";
      // Ушла в минус — «убыток»: в легенде по последнему значению, в подсказке — по значению в точке.
      const profitLabel = (v) => v < 0 ? "Реализованный убыток" : "Реализованная прибыль";
      const datasets = [
        ds(profitLabel(profit.length ? profit[profit.length - 1][1] : 0), profit, PROFIT,
          { backgroundColor: alpha(PROFIT, 0.25), isProfit: true }),
        ds("Текущая оценка портфеля", s.value, VALUE, { fill: onProfit }, true),
        ds("Оценка по истории продаж Steam", s.value_estimate, VALUE, { fill: onProfit, borderDash: [5, 4], backgroundColor: alpha(VALUE, 0.08) }, true),
        ds("Вложено", s.invested, INVESTED, { fill: false, borderWidth: 2.5 }),
      ].filter((d) => d.data.length);
      const fmtDay = (ms) => new Date(ms).toLocaleDateString("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "2-digit" });
      // Окно: [конец − ширина, конец]. Ширина — выбранный период или участок, выделенный двумя щелчками.
      // Сектор на полосе под графиком — это окно; его тащат влево-вправо по датам.
      const xs = datasets.flatMap((d) => d.data.map((p) => p.x));
      const first = xs.length ? Math.min(...xs) : Date.now();
      const last = Math.max(Date.now(), ...xs);
      const span = zoomSpan || period * DAY;
      const pannable = span > 0 && last - first > span;
      const clampEnd = (e) => Math.min(Math.max(e, first + span), last);
      const end = pannable && panEnd != null ? clampEnd(panEnd) : last;
      const min = span ? end - span : undefined;
      const max = span ? end : undefined;
      const config = {
        type: "line",
        data: { datasets },
        options: {
          maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
          interaction: { mode: "nearest", axis: "x", intersect: false },
          onClick: (evt, _, chart) => pickZoomEdge(chart, evt),
          onHover: (evt, _, chart) => {
            if (zoomStart == null) return;
            zoomHover = inPlot(chart, evt) ? chart.scales.x.getValueForPixel(evt.x) : null;
            chart.draw();
          },
          scales: {
            x: { type: "linear", min, max, ticks: { callback: fmtDay, maxTicksLimit: 8 }, grid: { display: false } },
            y: { beginAtZero: true, ticks: { callback: axis } },
          },
          plugins: {
            // Легенда — отрезками линий, как на самом графике: оценка по истории продаж — пунктиром.
            legend: { labels: {
              usePointStyle: true, pointStyleWidth: 26,
              generateLabels: (chart) => Chart.defaults.plugins.legend.labels.generateLabels(chart).map((item) => {
                const d = chart.data.datasets[item.datasetIndex];
                return { ...item, pointStyle: "line", fillStyle: "transparent", strokeStyle: d.borderColor,
                  lineWidth: 2.5, lineDash: d.borderDash || [] };
              }),
            } },
            tooltip: {
              itemSort: (a, b) => b.raw.v - a.raw.v,  // сверху — большее значение
              callbacks: {
                title: (items) => dateTime(new Date(items[0].parsed.x).toISOString()),
                label: (c) => `${c.dataset.isProfit ? profitLabel(c.raw.v) : c.dataset.label}: ${money(c.raw.v, cur)}`,
              },
            },
          },
        },
        plugins: [zoomMarker],
      };
      if (lineChart) lineChart.destroy();
      lineChart = new Chart(el("line"), config);
      el("zoom-reset").classList.toggle("hidden", !zoomSpan);
      nav = pannable ? { first, last, span, clampEnd, fmtDay } : null;
      el("line-nav").classList.toggle("hidden", !nav);
      if (nav) moveWindow(end);
      const scope = chartCurrency === "TOTAL"
        ? "Отражены все позиции. Текущая оценка портфеля взята по лучшему ордеру на покупку на ТП Steam (в USD). "
          + "«Вложено» и «Реализованная прибыль/убыток» берутся с графиков конкретных валют посредством пересчёта этих валют в USD по курсу на конкретные даты."
        : `Отражены только позиции с валютой ${cur}. Текущая оценка портфеля взята по лучшему ордеру на покупку на ТП Steam (в валюте ${cur}).`;
      el("line-note").textContent = !datasets.length
        ? (chartCurrency === "TOTAL" ? "Данных пока нет — внесите покупки на вкладке «Портфолио»." : `Позиций в ${cur} нет.`)
        : scope + "\nРеализованная прибыль/убыток + Текущая оценка портфеля дают общую оценку портфеля на текущий момент, "
          + "которая может быть больше или меньше суммы вложенных средств (линия «Вложено»).";
    }

    async function loadSeries() {
      try {
        data.series = await api("GET", `/api/stats/series?currency=${chartCurrency}`);
      } catch (e) { toast(e.message, true); }
      renderLine();
    }

    // Цвета категорий круговой диаграммы и подкатегорий наклеек; у агентов и оружия цвет подкатегории —
    // цвет редкости на площадке Steam (приходит с сервера), у ножей и перчаток — золото со штриховкой.
    const CATEGORY_COLORS = {
      "Кейсы": "#ff8a00", "Капсулы с наклейками": "#2bd94f", "Сувенирные наборы": "#111111", "Наклейки": "#f23fd2",
      "Нашивки": "#1fb8ff", "Агенты": "#ff2e3d", "Оружие": "#8f5bff", "Ножи и перчатки": "#ffd60a", "Прочее": "#6b7280",
    };
    const SUB_COLORS = { "Наклейки турниров": "#ff8a00", "Наклейки операций": "#1fb8ff", "Стикеры Armory": "#2bd94f", "Наклейки иные": "#8f5bff" };
    // Цвет — жёлтый категории «Ножи и перчатки»; различаются наклоном линий: ножи — слева-сверху направо-вниз,
    // перчатки — наоборот.
    const HATCH = { "Ножи": { dir: "\\", color: "#ffd60a" }, "Перчатки": { dir: "/", color: "#ffd60a" } };
    const colorOf = (type) => CATEGORY_COLORS[type] || PALETTE[PALETTE.length - 1];
    // Штриховка для диаграммы (узор canvas) и для квадратика в таблице (CSS).
    function hatchPattern({ dir, color }) {
      const c = document.createElement("canvas");
      c.width = c.height = 10;
      const g = c.getContext("2d");
      g.fillStyle = color;
      g.fillRect(0, 0, 10, 10);
      g.strokeStyle = "rgba(90, 64, 0, .75)";
      g.lineWidth = 1.6;
      g.beginPath();
      for (const k of [-10, 0, 10]) {
        if (dir === "\\") { g.moveTo(k, 0); g.lineTo(k + 10, 10); } else { g.moveTo(k + 10, 0); g.lineTo(k, 10); }
      }
      g.stroke();
      return g.createPattern(c, "repeat");
    }
    const hatchCss = ({ dir, color }) => `repeating-linear-gradient(${dir === "\\" ? 45 : -45}deg, ${color} 0 4px, rgba(90, 64, 0, .75) 4px 5.6px)`;
    // Цвет подкатегории: {chart — для диаграммы, css — для квадратика}.
    function subColor(s) {
      if (HATCH[s.subtype]) return { chart: hatchPattern(HATCH[s.subtype]), css: hatchCss(HATCH[s.subtype]) };
      const c = s.color || SUB_COLORS[s.subtype] || PALETTE[PALETTE.length - 1];
      return { chart: c, css: c };
    }

    // Уровни: категории → подкатегории (у наклеек, агентов, оружия, ножей и перчаток; предметы — в таблице)
    // или сразу предметы (у категорий без подкатегорий).
    function renderPie() {
      const types = data.types.types;
      const level = drill ? types.find((t) => t.type === drill) : null;
      if (drill && !level) drill = null;
      const subs = level && level.subtypes;
      const slices = subs || (level ? level.items : types);
      const base = level ? colorOf(level.type) : null;
      const shades = slices.map((s, i) => subs ? subColor(s)
        : level ? { chart: alpha(base, Math.max(0.25, 1 - i * (0.75 / Math.max(slices.length, 1)))) }
        : { chart: colorOf(s.type) });
      const colors = shades.map((c) => c.chart);
      const subCss = subs ? Object.fromEntries(subs.map((s, i) => [s.subtype, shades[i].css])) : {};
      el("pie-title").textContent = level ? `${level.type}: ${money(level.value_usd, "USD")}` : `По типам предметов: ${money(data.types.total_usd, "USD")}`;
      el("pie-back").classList.toggle("hidden", !level);
      const surface = css("--surface");
      const config = {
        type: "doughnut",
        data: {
          labels: slices.map((s) => subs ? s.subtype : level ? s.item : s.type),
          // Чёрный сектор (сувенирные наборы) на тёмном фоне — со светлой каймой.
          datasets: [{ data: slices.map((s) => s.value_usd), backgroundColor: colors,
            borderColor: colors.map((c) => c === "#111111" ? "#6b7280" : surface), borderWidth: 2 }],
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
      // В таблице внутри категории — её предметы; при подкатегориях у предмета квадратик и название подкатегории.
      const rows = level ? level.items : slices;
      // Предметы — группами по подкатегориям в порядке секторов, внутри — по стоимости.
      const subIndex = (s) => subs.findIndex((x) => x.subtype === s.subtype);
      if (subs) rows.sort((a, b) => subIndex(a) - subIndex(b) || b.value_usd - a.value_usd);
      el("pie-table").innerHTML = !slices.length ? '<div class="empty-state">Портфель пуст</div>'
        : `<table><thead><tr><th>${level ? "Предмет" : "Тип"}</th>${subs ? "<th>Подкатегория</th>" : ""}${level ? '<th class="num">Кол-во</th>' : ""}<th class="num">Стоимость</th><th class="num">Доля</th></tr></thead><tbody>
        ${rows.map((s, i) => level
          ? `<tr><td class="name">${subs ? "" : sw(colors[i])}<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.item)}</a>${s.no_price ? '<span class="note">без цены — по себестоимости</span>' : ""}</td>
              ${subs ? `<td>${sw(subCss[s.subtype])}${esc(s.subtype)}</td>` : ""}
              <td class="num">${num(s.quantity, 0)}</td><td class="num">${money(s.value_usd, "USD")}</td><td class="num">${num(s.share, 1)}%</td></tr>`
          : `<tr class="clickable" data-type="${esc(s.type)}"><td>${sw(colors[i])}${esc(s.type)}</td><td class="num">${money(s.value_usd, "USD")}</td><td class="num">${num(s.share, 1)}%</td></tr>`).join("")}
        </tbody></table>`;
      el("pie-table").querySelectorAll("tr[data-type]").forEach((tr) => tr.onclick = () => { drill = tr.dataset.type; renderPie(); });
    }

    function renderBest() {
      document.querySelectorAll("#best-sort button").forEach((b) => b.classList.toggle("active", b.dataset.sort === bestSort));
      const key = { pct: "profit_pct", annual: "annual_pct" }[bestSort] || "profit_usd";
      const all = [...data.sales].sort((a, b) => b[key] - a[key]);
      // Без флажка «Все продажи» — ТОП-10 по выбранному показателю.
      const rows = el("best-all").checked ? all : all.slice(0, 10);
      el("best").innerHTML = !rows.length ? '<div class="empty-state">Продаж пока нет</div>'
        : `<table><thead><tr><th>#</th><th>Предмет</th><th>Дата покупки</th><th>Дата продажи</th><th class="num">Кол-во</th><th class="num">Прибыль, $</th><th class="num">Прибыль, %</th><th class="num">Прибыль, % годовых</th><th class="num">Срок, мес.</th></tr></thead><tbody>
          ${rows.map((s, i) => `<tr><td class="muted">${i + 1}</td><td class="name">${esc(s.item)}<span class="type">${money(s.profit, s.currency)} в валюте строки</span></td>
            <td>${s.first_buy_date ? date(s.first_buy_date) : "—"}</td><td>${date(s.date)}</td><td class="num">${num(s.quantity, 0)}</td><td class="num">${signed(s.profit_usd)}</td><td class="num">${pct(s.profit_pct)}</td>
            <td class="num"><span title="За ${num(s.days, 0)} дн. владения">${pct(s.annual_pct)}</span></td><td class="num">${num(s.months, 1)}</td></tr>`).join("")}
          </tbody></table>`;
    }

    async function load() {
      data = await api("GET", "/api/stats");
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
      panEnd = zoomSpan = null;  // новый период — без выделения, снова по сегодняшний день
      renderLine();
    });
    el("zoom-reset").onclick = () => {
      panEnd = zoomSpan = null;
      renderLine();
    };
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

  return { portfolioPage, ordersPage, statsPage, settingsPage };
})();
