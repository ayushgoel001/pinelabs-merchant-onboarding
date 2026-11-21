const byId = (id) => document.getElementById(id);
const store = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },   // storage can be unavailable
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* ignore */ } },
  remove(key) { try { sessionStorage.removeItem(key); } catch { /* ignore */ } },
};

function toast(text) {
  const note = document.createElement("div");
  note.className = "toast";
  note.setAttribute("role", "status");
  note.textContent = text;
  document.body.append(note);
  setTimeout(() => note.remove(), 5000);
}
const flash = store.get("flash");
if (flash) { store.remove("flash"); toast(flash); }

function refreshLink(message, data) {
  if (!data.refresh) return;
  const link = document.createElement("a");
  link.href = data.refresh;
  link.textContent = "Open current case";
  message.append(" ", link);
}

async function redeem() {
  const ticket = window.location.hash.slice(1);
  history.replaceState(null, "", location.pathname);
  const result = await fetch("/access", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ticket})
  });
  if (result.ok && result.redirected) location.replace(result.url);
  else byId("redeem").textContent = "This link is unavailable or has expired.";
}
if (byId("redeem")) redeem().catch(() => { byId("redeem").textContent = "Unable to open this link."; });

// ---- Merchant intake and amendment. Merchants never see the structured documents: they are built from the form, and
// a file chosen for a document is uploaded after the application exists, through the same endpoints as before.
const KINDS = ["identity", "tax", "bank"];
const KIND_NAMES = {identity: "identity document", tax: "tax (GST) document", bank: "bank document"};
const MAX_UPLOAD = 2 * 1024 * 1024;
const UPLOAD_TYPES = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", pdf: "application/pdf"};
const sizeText = (bytes, digits = 1) => bytes < 1024 * 1024 ? (bytes / 1024).toFixed(0) + " KiB" : (bytes / 1024 / 1024).toFixed(digits) + " MiB";
const extensionOf = (file) => file.name.split(".").pop().toLowerCase();

const form = byId("intake");
const area = (kind) => byId("doc-" + kind);
const typedBox = (kind) => form.querySelector(`.use-typed[data-kind="${kind}"]`);
const merchantValues = () => Object.fromEntries(new FormData(form));
const typedDocument = (kind, m) => kind === "identity" ? {name: m.name, pan: m.pan}
  : kind === "tax" ? {name: m.name, pan: m.pan, gst: m.gst} : {beneficiary: m.name, account: m.account, routing: m.routing};
let initialValues = "", initialChecks = "", dirty = false;
const checksState = () => KINDS.map(k => typedBox(k)?.checked ? "1" : "0").join("");
const canonical = (doc) => JSON.stringify(Object.keys(doc || {}).sort().map(key => [key, doc[key]]));

function showJson(kind, value, custom) {
  area(kind).value = value ? JSON.stringify(value, null, 2) : "";
  area(kind).dataset.custom = custom ? "1" : "";
}
function syncTypedDocuments() {
  // The structured details follow the form, unless someone chose them by hand (a scenario, a sample or an edit).
  const m = merchantValues();
  for (const kind of KINDS) {
    if (typedBox(kind)?.checked && area(kind).dataset.custom !== "1") showJson(kind, typedDocument(kind, m), false);
  }
}

if (form) {
  initialValues = JSON.stringify(merchantValues());
  initialChecks = checksState();
  form.addEventListener("input", event => {
    event.target.removeAttribute?.("aria-invalid");
    if (event.target.matches("textarea[data-document]")) { event.target.dataset.custom = "1"; dirty = true; }
    else if (event.target.name) syncTypedDocuments();
  });
  // Ticking "use the details above" makes that document the typed details, replacing whatever it held before
  // (a contradicting demo document, say). Unticking leaves the document as it is.
  form.querySelectorAll(".use-typed").forEach(box => box.addEventListener("change", () => {
    if (box.checked) showJson(box.dataset.kind, typedDocument(box.dataset.kind, merchantValues()), false);
  }));
  KINDS.forEach(kind => {
    const input = byId("file-" + kind);
    if (!input) return;
    input.addEventListener("change", () => {
      const note = byId("note-" + kind), file = input.files[0];
      const problem = file ? checkFile(file) : "";
      note.classList.toggle("bad", Boolean(problem));
      note.textContent = file ? (problem || file.name + " · " + sizeText(file.size)) : "";
      input.closest(".doc-card")?.classList.toggle("has-file", Boolean(file) && !problem);
      // A chosen file is the document, so the typed alternative steps aside.
      const box = typedBox(kind);
      if (box) { box.disabled = Boolean(file) && !problem; if (box.disabled) box.checked = false; }
    });
    const zone = input.closest(".dropzone");
    ["dragenter", "dragover"].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(name => zone.addEventListener(name, event => { event.preventDefault(); zone.classList.remove("drag"); }));
    zone.addEventListener("drop", event => {
      if (event.dataTransfer?.files?.length) { input.files = event.dataTransfer.files; input.dispatchEvent(new Event("change")); }
    });
  });
}

function checkFile(file) {
  if (!UPLOAD_TYPES[extensionOf(file)]) return "Only PNG, JPEG or PDF files can be read.";
  if (file.size > MAX_UPLOAD) return "This file is too large (" + sizeText(file.size, 2) + "). The limit is 2 MiB. Try a smaller scan or a lower-resolution photo.";
  return "";
}

document.querySelectorAll(".sample").forEach(button => {
  button.dataset.defaultSample = button.dataset.sample;
  button.addEventListener("click", () => {
    const kind = byId(button.dataset.target).dataset.document;
    showJson(kind, JSON.parse(button.dataset.sample), true);
    if (typedBox(kind)) typedBox(kind).checked = true;
    dirty = true;
    byId(button.dataset.target).focus();
  });
});

function progressStep(text) {
  const list = byId("progress");
  list.hidden = false;
  const item = document.createElement("li");
  item.textContent = text;
  item.dataset.state = "working";
  list.append(item);
  return {done: (note) => { item.dataset.state = "done"; if (note) item.textContent = note; },
          fail: (note) => { item.dataset.state = "failed"; item.textContent = note; }};
}

async function postJson(url, method, body) {
  const response = await fetch(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  return {response, data: await response.json().catch(() => ({}))};
}

if (form) form.addEventListener("submit", async event => {
  event.preventDefault();
  const message = byId("message"), uploadMessage = byId("upload-message");
  const submit = form.querySelector('[type="submit"]');
  message.textContent = "";
  uploadMessage.textContent = "";
  byId("progress").replaceChildren();
  form.querySelectorAll("[aria-invalid]").forEach(el => el.removeAttribute("aria-invalid"));
  const caseId = form.dataset.case;
  const m = merchantValues();
  const formChanged = JSON.stringify(m) !== initialValues;
  const files = KINDS.map(kind => ({kind, file: byId("file-" + kind)?.files[0]})).filter(item => item.file);
  for (const {kind, file} of files) {
    const problem = checkFile(file);
    if (problem) { uploadMessage.textContent = `The ${KIND_NAMES[kind]}: ${problem}`; byId("file-" + kind).focus(); return; }
  }
  // Which documents go in the structured part of the application. A file is uploaded afterwards, so it is not typed.
  const documents = {};
  for (const kind of KINDS) {
    const current = area(kind).value.trim();
    const hasFile = files.some(item => item.kind === kind);
    const typed = typedBox(kind)?.checked;
    const hand = area(kind).dataset.custom === "1" || dirty;
    let value = null;
    if (hasFile) value = caseId && current ? current : null;  // keep what the case holds until the new file replaces it
    else if (typed) value = (hand || !formChanged) && current ? current : JSON.stringify(typedDocument(kind, m));
    else if (current && (caseId || area(kind).dataset.custom === "1")) value = current;  // kept as it is: held, or chosen by hand
    if (value === null) continue;
    try { documents[kind] = JSON.parse(value); }
    catch {
      message.textContent = "The advanced details for the " + KIND_NAMES[kind] + " are not valid JSON.";
      form.querySelector("details.advanced").open = true;
      area(kind).setAttribute("aria-invalid", "true");
      area(kind).focus();
      return;
    }
  }
  // The save always goes first and names the files that follow it. The server then holds the case (no checks, no
  // approval, no use of the copy it holds) until each named file is read, or a later save stops naming it.
  if (!Object.keys(documents).length && !files.length) documents.identity = {};
  submit.disabled = true;
  const idle = submit.textContent;
  submit.textContent = "Saving…";
  let revision = Number(form.dataset.revision || 1), id = caseId, failures = [];
  try {
    {
      const step = progressStep(caseId ? "Saving your changes" : "Saving your application");
      const payload = {merchant: m, documents, uploads: files.map(item => item.kind)};
      if (caseId) payload.data_revision = revision; else payload.submission_key = form.dataset.key;
      const {response, data} = await postJson(caseId ? "/api/cases/" + caseId : "/api/cases", caseId ? "PATCH" : "POST", payload);
      if (!response.ok) {
        step.fail("We could not save your application.");
        message.textContent = data.detail + (data.fields?.length ? " Check " + data.fields.join(", ") + "." : "");
        let first = null;
        for (const name of data.fields || []) {
          const input = form.querySelector(`[name="${CSS.escape(name)}"]`);
          if (input) { input.setAttribute("aria-invalid", "true"); first = first || input; }
        }
        if (first) first.focus(); else message.scrollIntoView({block: "center"});
        refreshLink(message, data);
        return;
      }
      step.done(caseId ? "Changes saved" : "Application saved");
      if (!caseId) {
        id = data.case_id;
        // The merchant gets access to their own case through the one-time ticket in the response, as before.
        await fetch("/access", {method: "POST", headers: {"Content-Type": "application/json"},
                                 body: JSON.stringify({ticket: data.location.split("#")[1]})});
        revision = 1;
      } else revision += 1;
    }
    for (const {kind, file} of files) {
      const step = progressStep(`Reading your ${KIND_NAMES[kind]}…`);
      try {
        const response = await fetch(`/api/cases/${id}/documents/${kind}?revision=${revision}`, {
          method: "POST", headers: {"Content-Type": UPLOAD_TYPES[extensionOf(file)]}, body: file});
        const data = await response.json().catch(() => ({}));
        if (response.ok) { revision += 1; step.done(`Your ${KIND_NAMES[kind]} was read`); }
        else { failures.push(`the ${KIND_NAMES[kind]}: ${(data.detail || "it could not be read").replace(/\.$/, "")}`); step.fail(`We could not read your ${KIND_NAMES[kind]}`); }
      } catch { failures.push(`the ${KIND_NAMES[kind]}: the upload failed`); step.fail(`The upload of your ${KIND_NAMES[kind]} failed`); }
    }
    store.set("flash", failures.length
      ? "Saved, but we could not use " + failures.join("; ") + ". You can upload it again from this page."
      : caseId ? "Your changes were saved. We are checking them now."
      : files.length ? "Application submitted. We are reading your documents and checking everything." : "Application submitted.");
    location.assign("/cases/" + id);
  } catch { message.textContent = "Could not save. Check your connection and try again."; }
  finally { submit.disabled = false; submit.textContent = idle; }
});

const picker = byId("scenario");
if (picker) picker.addEventListener("change", () => {
  const chosen = JSON.parse(picker.dataset.scenarios).find(s => s.slug === picker.value);
  byId("story").textContent = chosen ? chosen.story : "";
  for (const button of document.querySelectorAll(".sample")) {
    const kind = byId(button.dataset.target).dataset.document;
    button.dataset.sample = chosen ? JSON.stringify(chosen.samples[kind]) : button.dataset.defaultSample;
  }
  if (!chosen) {
    form.reset();
    KINDS.forEach(k => { area(k).dataset.custom = ""; byId("file-" + k)?.dispatchEvent(new Event("change")); });
    syncTypedDocuments();
    return;
  }
  for (const [name, value] of Object.entries(chosen.merchant)) {
    const input = form.querySelector(`[name="${name}"]`);
    if (input) input.value = value;
  }
  for (const kind of KINDS) {
    const doc = chosen.documents[kind];
    // Ticked only when the scenario's document really is the details above; a contradicting one is sent as it is.
    const same = Boolean(doc) && canonical(doc) === canonical(typedDocument(kind, chosen.merchant));
    showJson(kind, doc, Boolean(doc) && !same);
    if (typedBox(kind)) typedBox(kind).checked = same;
    const note = byId("note-" + kind);
    if (note && !byId("file-" + kind).files.length) {
      note.textContent = doc && !same ? "This scenario types this document separately (see Advanced)." : "";
    }
  }
  dirty = true;
});

// Keep a case page current while provider requests are outstanding.
const caseRoot = byId("case-root");
const casePath = /^\/cases\/[0-9a-f]{32}$/;
if (caseRoot && document.body.classList.contains("portal") && casePath.test(location.pathname)) {
  store.set("lastMerchantCase", location.pathname);
}
const lastApplication = byId("last-application");
const lastPath = lastApplication && store.get("lastMerchantCase");
if (lastPath && casePath.test(lastPath)) {
  lastApplication.href = lastPath;
  lastApplication.hidden = false;
}
if (caseRoot && caseRoot.dataset.pending === "1") {
  const timer = setTimeout(() => location.reload(), 2000);
  document.querySelectorAll(".decision, details").forEach(el => {
    el.addEventListener("focusin", () => clearTimeout(timer));
    el.addEventListener("pointerdown", () => clearTimeout(timer));
  });
}

// Operator decisions. Rejection needs a second, explicit click because it is final.
document.querySelectorAll(".decision").forEach(decision => {
  const button = decision.querySelector("button");
  const message = decision.querySelector('[role="alert"]');
  const idle = button.textContent;
  let armedTimer = null;
  const disarm = () => {
    clearTimeout(armedTimer);
    button.classList.remove("armed");
    button.textContent = idle;
    decision.dataset.armed = "";
  };
  decision.elements.reason.addEventListener("change", () => { disarm(); message.textContent = ""; });
  decision.addEventListener("submit", async event => {
    event.preventDefault();
    if (decision.dataset.kind === "reject" && decision.dataset.armed !== "1") {
      decision.dataset.armed = "1";
      button.classList.add("armed");
      button.textContent = "Confirm: " + idle.toLowerCase();
      message.textContent = "Click again to confirm. Rejection is final.";
      armedTimer = setTimeout(() => { disarm(); message.textContent = ""; }, 8000);
      return;
    }
    clearTimeout(armedTimer);
    const body = {...JSON.parse(decision.dataset.command), reason: decision.elements.reason.value};
    button.disabled = true;
    try {
      const response = await fetch(decision.dataset.url, {
        method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
      });
      const data = await response.json();
      if (response.ok) {
        store.set("flash", "Decision recorded: " + (decision.dataset.label || "decision") + ".");
        location.assign(data.location);
        return;
      }
      disarm();
      message.textContent = data.detail;
      refreshLink(message, data);
    } catch { disarm(); message.textContent = "Connection unavailable. Reload the case to check the decision."; }
    finally { button.disabled = false; }
  });
});

// Operator-only simulator controls (hold release and callback replay), enabled with --demo-controls.
document.querySelectorAll(".control").forEach(button => {
  button.addEventListener("click", async () => {
    button.disabled = true;
    const response = await fetch(button.dataset.url, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: button.dataset.body
    });
    if (response.ok) setTimeout(() => location.reload(), 900);
    else { button.disabled = false; button.textContent = "Unavailable"; }
  });
});

document.querySelectorAll("[data-copy]").forEach(button => {
  button.addEventListener("click", async () => {
    const text = byId(button.dataset.copy).textContent.trim();
    try { await navigator.clipboard.writeText(text); button.textContent = "Copied"; }
    catch {
      const range = document.createRange();
      range.selectNodeContents(byId(button.dataset.copy));
      getSelection().removeAllRanges();
      getSelection().addRange(range);
      button.textContent = "Press Ctrl+C";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
});

const heartbeat = byId("heartbeat");
if (heartbeat) setInterval(async () => {
  try {
    const status = await (await fetch("/health", {cache: "no-store"})).json();
    heartbeat.textContent = (status.worker_online ? "Worker online" : "Worker offline")
      + (status.worker_error ? " · recent error: " + status.worker_error : "");
    heartbeat.classList.toggle("s-ACTIVE", Boolean(status.worker_online));
    heartbeat.classList.toggle("s-IN_REVIEW", !status.worker_online);
  } catch {
    heartbeat.textContent = "Connection unavailable";
    heartbeat.classList.remove("s-ACTIVE");
    heartbeat.classList.add("s-IN_REVIEW");
  }
}, 1000);

// Operator desk: client-side filters and search (remembered for the session) and a gentle refresh while the tab is visible.
const casesTable = byId("cases");
if (casesTable) {
  const buttons = [...document.querySelectorAll(".filters button")];
  const rows = [...casesTable.tBodies[0].rows];
  const search = byId("desk-search");
  let filter = "all";
  const apply = () => {
    const term = search ? search.value.trim().toLowerCase() : "";
    let shown = 0;
    for (const row of rows) {
      const match = (filter === "all" || row.dataset.state === filter || row.dataset.queue === filter)
        && (!term || (row.dataset.search || "").includes(term));
      row.hidden = !match;
      if (match) shown += 1;
    }
    for (const button of buttons) button.setAttribute("aria-pressed", String(button.dataset.filter === filter));
    byId("no-match").hidden = shown !== 0;
    const count = byId("match-count");
    if (count) count.textContent = shown === rows.length ? "" : shown + " of " + rows.length + " shown";
    store.set("deskFilter", filter);
    store.set("deskSearch", term);
  };
  // Sort the rows already on the page: by submission time, or the operator's queue first (then newest).
  const sort = byId("desk-sort");
  const order = () => {
    const mode = sort ? sort.value : "newest";
    const created = row => Number(row.dataset.created || 0);
    const sorted = [...rows].sort((a, b) => {
      if (mode === "attention") {
        const rank = row => (row.dataset.queue === "operator" ? 0 : row.dataset.queue === "merchant" ? 1 : 2);
        if (rank(a) !== rank(b)) return rank(a) - rank(b);
      }
      return mode === "oldest" ? created(a) - created(b) : created(b) - created(a);
    });
    casesTable.tBodies[0].append(...sorted);
    store.set("deskSort", mode);
  };
  if (sort) {
    const savedSort = store.get("deskSort");
    if ([...sort.options].some(o => o.value === savedSort)) sort.value = savedSort;
    sort.addEventListener("change", order);
    order();
  }
  buttons.forEach(button => button.addEventListener("click", () => { filter = button.dataset.filter; apply(); }));
  if (search) search.addEventListener("input", apply);
  const saved = store.get("deskFilter") || "all";
  filter = buttons.some(b => b.dataset.filter === saved) ? saved : "all";
  if (search) search.value = store.get("deskSearch") || "";
  apply();
  setInterval(() => {
    if (!document.hidden && document.activeElement !== search && document.activeElement !== sort) location.reload();
  }, 10000);
}

const menuToggle = document.querySelector(".menu-toggle");
if (menuToggle) {
  const closeMenu = () => {
    document.body.classList.remove("nav-open");
    menuToggle.setAttribute("aria-expanded", "false");
  };
  menuToggle.addEventListener("click", () => {
    const open = document.body.classList.toggle("nav-open");
    menuToggle.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && document.body.classList.contains("nav-open")) {
      closeMenu();
      menuToggle.focus();
    }
  });
  document.addEventListener("click", event => {
    if (!event.target.closest(".menu-toggle, .sidebar")) closeMenu();
  });
  window.matchMedia("(min-width: 926px)").addEventListener("change", closeMenu);
}
const sectionLinks = [...document.querySelectorAll(".intake-steps a")];
function markSection(id) {
  for (const link of sectionLinks) {
    if (link.hash === "#" + id) link.setAttribute("aria-current", "step");
    else link.removeAttribute("aria-current");
  }
}
if (sectionLinks.length) {
  sectionLinks.forEach(link => link.addEventListener("click", () => markSection(link.hash.slice(1))));
  form.addEventListener("focusin", event => {
    if (event.target.closest("#uploads, .advanced")) markSection("uploads");
    else if (event.target.closest("#submission")) markSection("submission");
    else markSection("business");
  });
}
if (location.hash === "#desk-search") byId("desk-search")?.focus();
document.querySelectorAll('a[href="#attempts"], a[href="#definitions"]').forEach(link => {
  link.addEventListener("click", () => {
    const details = byId(link.hash.slice(1));
    if (details) details.open = true;
  });
});
