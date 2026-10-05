"use strict";

const $ = (id) => document.getElementById(id);
const rows = new Map();
let state = null;
let authenticated = false;
let stale = true;
let busy = false;
let refreshTask = null;
let timer;
let runSignature;
let nextId = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function running() { return state?.run?.status === "running"; }
function blocked() { return !authenticated || stale || busy || running(); }

function controls() {
  document.querySelectorAll("[data-mutation]").forEach((node) => {
    node.disabled = blocked() || node.dataset.bound === "true";
  });
  $("clear").disabled = blocked() || !state?.items.length;
  $("prepare").disabled = blocked() || !state?.items.length;
  $("prepare").textContent = running() ? "Preparing cart…" : "Prepare cart";
  $("prepare-note").textContent = stale ? "Connection stale. Reconnect before making changes."
    : running() ? "Shopping is running. List editing is paused."
      : "Keeps existing cart items. You complete checkout.";
  $("logout").disabled = busy;
  $("retry").disabled = busy || Boolean(refreshTask);
}

function connection(message, failed = false) {
  $("connection").hidden = !message;
  $("connection").classList.toggle("stale", failed);
  $("connection-text").textContent = message;
  $("retry").hidden = !failed;
}

function showLogin(message) {
  clearTimeout(timer);
  authenticated = false;
  stale = true;
  state = null;
  rows.clear();
  $("items").replaceChildren();
  $("run-review").replaceChildren();
  runSignature = undefined;
  $("workspace").hidden = true;
  $("logout").hidden = true;
  $("login").hidden = false;
  $("login-message").textContent = message;
  $("feedback").textContent = "";
  connection("");
  $("password").value = "";
  $("password").focus();
}

async function request(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch(`/api/${path}`, {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin", cache: "no-store", signal: controller.signal,
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    if (!response.ok) {
      const error = new Error("Request failed");
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } finally { clearTimeout(timeout); }
}

function schedule() {
  clearTimeout(timer);
  if (authenticated) timer = setTimeout(refresh, running() ? 3000 : 15000);
}

function refresh() {
  if (refreshTask) return refreshTask;
  refreshTask = (async () => {
    try {
      const data = await request("state");
      if (!Array.isArray(data.items) || !(data.run === null || typeof data.run === "object")) throw new Error("Invalid state");
      state = data;
      stale = false;
      authenticated = true;
      $("login").hidden = true;
      $("workspace").hidden = false;
      $("logout").hidden = false;
      connection("");
      renderItems();
      renderRun();
    } catch (error) {
      if (error.status === 401) showLogin(authenticated ? "Your session expired. Sign in again." : "");
      else {
        stale = true;
        connection(state ? "Connection lost. This list and run status may be out of date. Changes are paused."
          : "Could not load your list. Check your connection and retry.", true);
      }
    } finally {
      refreshTask = null;
      controls();
      schedule();
    }
  })();
  controls();
  return refreshTask;
}

async function mutate(action, body, onSuccess) {
  if (blocked()) return;
  busy = true;
  clearTimeout(timer);
  controls();
  $("feedback").textContent = "";
  try {
    if (refreshTask) await refreshTask;
    clearTimeout(timer);
    if (!authenticated || stale || running()) return;
    await request(action, body);
    if (onSuccess) onSuccess();
    await refresh();
  } catch (error) {
    if (error.status === 401) showLogin("Your session expired. Sign in again.");
    else {
      stale = true;
      $("feedback").textContent = error.status === 409
        ? "Shopping data is busy or this action is no longer available. Refresh before trying again."
        : error.status === 400 ? "The change could not be saved. Refresh, then check your values."
          : "The request could not be confirmed. Refresh and check the result before trying again.";
      connection("State may be out of date. Changes are paused until a fresh update.", true);
    }
  } finally { busy = false; controls(); schedule(); }
}

function makeButton(text, label, handler, className = "secondary") {
  const button = element("button", text, className);
  button.type = "button";
  button.dataset.mutation = "";
  button.setAttribute("aria-label", label);
  button.addEventListener("click", handler);
  return button;
}

function syncPreferences(row, focused = document.activeElement) {
  for (const [key, input] of Object.entries(row.inputs)) {
    if (!row.dirty.has(key) && input !== focused) {
      input.value = row.item[key === "product" ? "preferred_name" : key] ?? "";
    }
  }
}

function createRow(item) {
  const row = { item, dirty: new Set(), inputs: {} };
  row.node = element("li", undefined, "item");
  const main = element("div", undefined, "item-main");
  const title = element("span", item.name, "item-name");
  const stepper = element("div", undefined, "stepper");
  row.quantity = element("span", "", "quantity");
  row.minus = makeButton("−", `Decrease target quantity for ${item.name}`, () => mutate("items", { name: row.item.name, quantity: row.item.quantity - 1 }));
  row.plus = makeButton("+", `Increase target quantity for ${item.name}`, () => mutate("items", { name: row.item.name, quantity: row.item.quantity + 1 }));
  stepper.append(row.minus, row.quantity, row.plus);
  main.append(title, stepper);
  const tools = element("div", undefined, "item-tools");
  const details = element("details");
  const summary = element("summary", "Preferences");
  summary.setAttribute("aria-label", `Preferences for ${item.name}`);
  const form = element("form", undefined, "preference");
  for (const [key, labelText] of [["product", "Preferred product"], ["brand", "Brand (optional)"], ["sku", "SKU (optional)"], ["max_price", "Price cap per unit · SAR (optional)"]]) {
    const input = element("input");
    input.id = `preference-${++nextId}`;
    input.name = key;
    input.dataset.mutation = "";
    input.type = key === "max_price" ? "number" : "text";
    if (key === "max_price") { input.min = "0.01"; input.step = "0.01"; }
    else input.maxLength = 200;
    input.required = key === "product";
    const label = element("label", labelText);
    label.htmlFor = input.id;
    row.inputs[key] = input;
    input.addEventListener("input", () => { row.dirty.add(key); });
    input.addEventListener("blur", () => syncPreferences(row, null));
    form.append(label, input);
  }
  const save = element("button", "Save preferences", "secondary");
  save.type = "submit";
  save.dataset.mutation = "";
  form.append(save);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    syncPreferences(row, null);
    const value = (key) => row.inputs[key].value.trim();
    if (!value("product")) { row.inputs.product.focus(); return; }
    mutate("preference", { name: row.item.name, product: value("product"), brand: value("brand") || null,
      sku: value("sku") || null, max_price: value("max_price") ? Number(value("max_price")) : null }, () => { row.dirty.clear(); row.signature = null; });
  });
  row.alternatives = element("ul", undefined, "alternatives");
  details.append(summary, form, element("h3", "Approved alternatives"), row.alternatives);
  const remove = makeButton("Remove", `Remove ${item.name}`, () => {
    if (!blocked() && window.confirm(`Remove “${row.item.name}” from your saved list? This does not remove anything from your live Tamimi cart.`)) {
      mutate("remove", { name: row.item.name }, () => $("item-name").focus());
    }
  }, "quiet danger remove");
  tools.append(details, remove);
  row.node.append(main, tools);
  return row;
}

function renderItems() {
  const names = new Set(state.items.map((item) => item.name));
  for (const [name, row] of rows) {
    if (!names.has(name)) { row.node.remove(); rows.delete(name); }
  }
  for (const item of state.items) {
    let row = rows.get(item.name);
    if (!row) { row = createRow(item); rows.set(item.name, row); $("items").append(row.node); }
    row.item = item;
    const signature = JSON.stringify(item);
    if (row.signature === signature) continue;
    row.signature = signature;
    row.quantity.textContent = item.quantity;
    row.quantity.setAttribute("aria-label", `${item.quantity} target units`);
    row.minus.dataset.bound = String(item.quantity <= 1);
    row.plus.dataset.bound = String(item.quantity >= 999);
    syncPreferences(row);
    row.alternatives.replaceChildren(...(item.alternatives.length ? item.alternatives.map((alt) =>
      element("li", `${alt.name || "Unnamed product"}${alt.sku ? ` · SKU ${alt.sku}` : ""}`)) : [element("li", "No approved alternatives saved.")]));
  }
  const count = state.items.length;
  $("count").textContent = `${count} ${count === 1 ? "item" : "items"} · Target quantities in purchasable units`;
  $("empty").hidden = count !== 0;
}

function productText(product) {
  return [product.name || "Unnamed product", product.brand, product.package_size, product.sku ? `SKU ${product.sku}` : null].filter(Boolean).join(" · ");
}
function priceText(product) {
  return typeof product.unit_price === "number" ? `SAR ${product.unit_price.toFixed(2)} per cart unit` : "Unit price not reported";
}

function renderRun() {
  const run = state.run;
  const signature = JSON.stringify(run);
  if (signature === runSignature) return;
  runSignature = signature;
  const target = $("run-review");
  target.replaceChildren();
  if (!run) {
    target.append(element("p", "Your prepared cart will appear here."), element("p", "Check products, prices and your delivery slot before you complete checkout in Tamimi.", "muted"));
    return;
  }
  const labels = { running: "Preparing your cart", complete: "Cart prepared", incomplete: "Preparation incomplete", error: "Preparation failed" };
  target.append(element("p", labels[run.status] || "Status unavailable", `run-status ${run.status === "incomplete" ? "warning" : run.status === "error" ? "error" : ""}`));
  target.append(element("p", run.status === "running" ? "Searching and checking products. Item-level live progress is not available."
    : run.status === "complete" ? "Review the results below, then complete checkout yourself in Tamimi."
      : "Review your live Tamimi cart manually before starting another preparation."));
  target.append(element("p", "This app does not place orders.", "hint"));
  if (run.status !== "running") {
    target.append(element("h3", "Delivery slot"), element("p", run.slot || "No delivery slot reported.", "muted"));
    target.append(element("h3", "Recorded cart"));
    const cart = element("ul", undefined, "run-list");
    for (const product of run.cart || []) {
      const li = element("li");
      li.append(element("p", productText(product)), element("p", `${product.quantity ?? "Unreported"} units · ${priceText(product)}`, "hint"));
      cart.append(li);
    }
    target.append(cart);
    if (!run.cart?.length) target.append(element("p", "No verified cart details were returned. This does not mean your live cart is empty.", "muted"));
    target.append(element("h3", "Selections & unresolved items"));
    const outcomes = element("ul", undefined, "run-list");
    const modes = { exact: "Exact selection", approved_alternative: "Approved alternative", automatic_substitution: "Automatic substitution" };
    for (const outcome of run.outcomes || []) {
      const li = element("li");
      li.append(element("strong", outcome.item_name || "Unnamed item"));
      li.append(element("p", [modes[outcome.selection_mode], outcome.unresolved ? "Unresolved — check manually" : null].filter(Boolean).join(" · ") || "Selection not reported", "hint"));
      if (outcome.product) li.append(element("p", productText(outcome.product)), element("p", priceText(outcome.product), "hint"));
      outcomes.append(li);
    }
    target.append(outcomes);
    if (!run.outcomes?.length) target.append(element("p", "No item outcomes reported.", "muted"));
    for (const [key, heading] of [["unresolved", "Unresolved items"], ["missing_or_over_cap", "Missing or over price cap"]]) {
      if (run[key]?.length) {
        const list = element("ul", undefined, "run-list");
        for (const name of run[key]) list.append(element("li", name));
        target.append(element("h3", heading), list);
      }
    }
    if (run.unresolved?.length || run.missing_or_over_cap?.length || run.outcomes?.some((outcome) => outcome.unresolved)) target.append(element("p", "Detailed unresolved reasons are not available here.", "hint"));
    if (run.confirmed) target.append(element("p", "You recorded that checkout was completed for this attempt.", "run-status"));
    else if (run.attempt && ["complete", "incomplete"].includes(run.status)) {
      target.append(makeButton("I completed checkout", "I completed checkout for this attempt", () => {
        if (!blocked() && window.confirm(`I completed checkout\n\nConfirm only if you completed checkout yourself in Tamimi for attempt ${run.attempt}. This records your purchase confirmation and may save validated selections as approved alternatives. It does not place an order.`)) {
          mutate("confirm", { attempt: run.attempt, confirmed: true });
        }
      }, "secondary confirm-purchase"));
    }
  }
  target.append(element("p", `Run reference: ${run.id}${run.attempt ? ` · Attempt ${run.attempt}` : ""}`, "run-reference"));
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy) return;
  busy = true;
  const button = event.submitter || $("login-form").querySelector("button");
  button.disabled = true;
  $("login-message").textContent = "Signing in…";
  try {
    await request("login", { password: $("password").value });
    $("password").value = "";
    authenticated = true;
    $("login").hidden = true;
    connection("Loading your shopping list…");
    await refresh();
    if (!stale) $("item-name").focus();
  } catch (error) {
    $("login-message").textContent = error.status === 401 ? "Password not recognized. Try again."
      : error.status === 429 ? "Too many attempts. Wait a minute, then try again." : "Could not sign in. Check your connection and try again.";
  } finally { busy = false; button.disabled = false; controls(); }
});

$("add-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = $("item-name").value.trim();
  const quantity = Number($("item-quantity").value);
  if (!name || !Number.isInteger(quantity) || quantity < 1 || quantity > 999) return;
  mutate("items", { name, quantity }, () => {
    $("item-name").value = "";
    $("item-quantity").value = "1";
  }).then(() => { if (!blocked()) $("item-name").focus(); });
});
$("clear").addEventListener("click", () => {
  if (!blocked() && window.confirm("Clear your saved list, preferences and approved alternatives? Purchase history and observed prices are kept. Your live Tamimi cart stays unchanged.")) mutate("clear", {});
});
$("prepare-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state?.items.length) return;
  mutate("shop", { location: $("location").value.trim() || null });
});
$("retry").addEventListener("click", () => { if (!busy) refresh(); });
$("logout").addEventListener("click", async () => {
  if (busy) return;
  busy = true;
  clearTimeout(timer);
  controls();
  try {
    if (refreshTask) await refreshTask;
    clearTimeout(timer);
    await request("logout", {});
    showLogin("Signed out.");
  } catch (error) {
    if (error.status === 401) showLogin("Signed out.");
    else { stale = true; connection("Could not confirm sign out. Retry the connection, then sign out again.", true); }
  } finally { busy = false; controls(); schedule(); }
});

refresh();
