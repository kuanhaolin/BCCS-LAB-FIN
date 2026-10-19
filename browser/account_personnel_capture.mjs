// Use official page handlers to prepare a personnel reimbursement.
// The browser is forbidden from POSTing to the save endpoint; Python validates
// the captured form and performs the single final POST separately.
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";

const origin = "https://account.ncu.edu.tw";
const menu = origin + "/APSWIS_Q/Dept_Add_Q/DA_SerBug_Menu_Q.asp?Firstload=Y";
const savePath = "/APSWIS_Q/Dept_Add_Q/STD_SAVE_SE1_Q.asp";
const chromePath = process.platform === "darwin"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  : "/usr/bin/google-chrome";
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
let stage = "input";

async function portFor(profile) {
  for (let i = 0; i < 100; i++) {
    try {
      const port = Number((await readFile(join(profile, "DevToolsActivePort"), "utf8")).split("\n")[0]);
      if (Number.isInteger(port) && port > 0) return port;
    } catch { /* Chrome is starting. */ }
    await pause(100);
  }
  throw new Error("Chrome did not start");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.handlers = new Map();
    ws.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error("DevTools command failed"));
        else pending.resolve(message.result);
      } else if (message.method) {
        for (const handler of this.handlers.get(message.method) || []) {
          Promise.resolve(handler(message.params)).catch(() => {});
        }
      }
    });
  }
  on(method, handler) {
    this.handlers.set(method, [...(this.handlers.get(method) || []), handler]);
  }
  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
}

const captureExpression = `(() => {
  const seen = new Set(), forms = [], handlers = [];
  const scan = view => {
    if (seen.has(view) || seen.size > 64) return;
    seen.add(view);
    try {
      if (view.location.hostname !== "account.ncu.edu.tw") return;
      forms.push(...view.document.forms);
      if (typeof view.SUM_ALERT === "function") handlers.push(view);
      Array.from(view.frames).forEach(scan);
    } catch (_) {}
  };
  scan(window.top);
  const one = (items, predicate) => {
    const matches = items.filter(predicate);
    if (matches.length !== 1) throw new Error("Official form is not unique");
    return matches[0];
  };
  const main = one(forms, f => ["APPYSET", "BUGETNO_1", "BUGCODE_1", "D_AMOUNT_1"].every(
    n => f.elements.namedItem(n)));
  const payee = one(forms, f => ["VENNAME_1", "VENDORID_1", "AMOUNT_1"].every(
    n => f.elements.namedItem(n)));
  const save = one(forms, f => {
    const url = new URL(f.action, f.ownerDocument.location.href);
    return url.origin === "${origin}" && url.pathname === "${savePath}" &&
      f.method.toLowerCase() === "post";
  });
  if (handlers.length !== 1 || handlers[0].document !== main.ownerDocument)
    throw new Error("Official save handler is not unique");
  const value = (form, name) => form.elements.namedItem(name)?.value ?? "";
  if (value(main, "APPYSET") !== "15") throw new Error("Wrong category");
  let submits = 0;
  Object.defineProperty(save, "submit", { configurable: true, value: () => { submits++; } });
  const messages = [];
  for (const view of seen) {
    try {
      if (view.location.hostname !== "account.ncu.edu.tw") continue;
      view.alert = message => { messages.push(String(message)); };
      view.confirm = () => false;
    } catch (_) {}
  }
  handlers[0].SUM_ALERT();
  if (submits !== 1 || messages.length) throw new Error("Official validation did not complete");
  return {
    fields: Array.from(save.elements).filter(el => el.name).map(el => [el.name, el.value]),
    main: { category: value(main, "APPYSET"), plan: value(main, "BUGETNO_1"),
      code: value(main, "BUGCODE_1"), amount: value(main, "D_AMOUNT_1"),
      move: value(main, "MOVETYPE_1") },
    payee: { name: value(payee, "VENNAME_1"),
      amount: value(payee, "AMOUNT_1"), hasVendorId: !!value(payee, "VENDORID_1") },
  };
})()`;

async function main() {
  const input = JSON.parse(await new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", chunk => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  }));
  if (!input || typeof input.script !== "string" || input.script.length > 30000 ||
      !input.script.includes("ATOLIS") || !input.script.includes("CHK5") ||
      typeof input.cookies !== "object" || !input.cookies ||
      Object.keys(input.cookies).length < 1 ||
      Object.entries(input.cookies).some(([name, value]) =>
        !/^ASPSESSIONID[A-Z0-9]{8}$/.test(name) ||
        typeof value !== "string" || !value || value.length > 8192)) {
    throw new Error("Invalid personnel preparation input");
  }
  const profile = input.profile;
  if (typeof profile !== "string" ||
      dirname(resolve(profile)) !== resolve(tmpdir()) ||
      !basename(profile).startsWith("ncu-personnel-")) {
    throw new Error("Invalid isolated Chrome profile");
  }
  let chrome, ws;
  try {
    stage = "chrome-start";
    chrome = spawn(chromePath, [
      "--headless=new", "--disable-gpu", "--no-first-run",
      "--no-default-browser-check", "--remote-debugging-port=0",
      `--user-data-dir=${profile}`, "about:blank",
    ], { stdio: "ignore" });
    const port = await portFor(profile);
    stage = "tab-open";
    const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,
                                 { method: "PUT" });
    if (!response.ok) throw new Error("Cannot create browser tab");
    const tab = await response.json();
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    const cdp = new Cdp(ws);
    let blockedSaves = 0, dialogs = 0;
    cdp.on("Fetch.requestPaused", async params => {
      blockedSaves++;
      await cdp.send("Fetch.failRequest", {
        requestId: params.requestId, errorReason: "BlockedByClient",
      });
    });
    cdp.on("Page.javascriptDialogOpening", async () => {
      dialogs++;
      await cdp.send("Page.handleJavaScriptDialog", { accept: false });
    });
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");
    await cdp.send("Fetch.enable", {
      patterns: [{ urlPattern: origin + savePath, requestStage: "Request" }],
    });
    await cdp.send("Network.setCookies", {
      cookies: Object.entries(input.cookies).map(([name, value]) => ({
        name, value, url: origin + "/APSWIS_Q/", secure: true,
      })),
    });
    stage = "navigate";
    await cdp.send("Page.navigate", { url: menu });
    for (let i = 0; i < 100; i++) {
      const state = await cdp.send("Runtime.evaluate", {
        expression: "({url:location.href,ready:document.readyState})", returnByValue: true,
      });
      const value = state.result?.value;
      if (value?.ready === "complete" && new URL(value.url).origin === origin &&
          new URL(value.url).pathname === new URL(menu).pathname) break;
      if (i === 99) throw new Error("Official menu did not load");
      await pause(200);
    }
    stage = "fill";
    const filled = await cdp.send("Runtime.evaluate", {
      expression: input.script, awaitPromise: true, returnByValue: true,
    });
    if (filled.exceptionDetails || filled.result?.value !== "filled" ||
        dialogs || blockedSaves) throw new Error("Official fields were not filled");
    stage = "capture";
    const captured = await cdp.send("Runtime.evaluate", {
      expression: captureExpression, returnByValue: true,
    });
    if (captured.exceptionDetails || !captured.result?.value || dialogs || blockedSaves)
      throw new Error("Official save form was not captured");
    const browserCookies = await cdp.send("Network.getCookies", { urls: [origin + "/APSWIS_Q/"] });
    const cookies = {};
    for (const cookie of browserCookies.cookies || []) {
      if (/^ASPSESSIONID[A-Z0-9]{8}$/.test(cookie.name) &&
          cookie.domain.replace(/^\./, "") === "account.ncu.edu.tw") {
        if (cookies[cookie.name] && cookies[cookie.name] !== cookie.value)
          throw new Error("Ambiguous session cookie");
        cookies[cookie.name] = cookie.value;
      }
    }
    if (!Object.keys(cookies).length) throw new Error("No accounting session cookies");
    process.stdout.write(JSON.stringify({ ...captured.result.value, cookies }));
  } finally {
    if (ws) ws.close();
    if (chrome) {
      const stopped = new Promise(resolve => chrome.once("exit", resolve));
      chrome.kill();
      await Promise.race([stopped, pause(2000)]);
    }
  }
}

main().catch(() => {
  // Never print session cookies, official form values, or private page content.
  process.stderr.write(`Unable to prepare official personnel form (${stage}).\n`);
  process.exitCode = 1;
});
