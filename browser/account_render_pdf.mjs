// Render an allowlisted official print page with an isolated Chrome profile.
// Cookie values arrive on stdin and are never written to application files.
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { validAccountPrintTarget } from "./account_print_target.mjs";

const accountOrigin = "https://account.ncu.edu.tw";
const humanOrigin = "https://cis.ncu.edu.tw";
const chromePath = process.platform === "darwin"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  : "/usr/bin/google-chrome";
let stage = "input";

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

async function devtoolsPort(profile) {
  const marker = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100; i++) {
    try {
      return Number((await readFile(marker, "utf8")).split("\n")[0]);
    } catch { await pause(100); }
  }
  throw new Error("Chrome did not start");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.events = new Map();
    ws.addEventListener("message", event => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }
      if (message.id) {
        const target = this.pending.get(message.id);
        if (!target) return;
        this.pending.delete(message.id);
        if (message.error) target.reject(new Error("DevTools command failed"));
        else target.resolve(message.result);
      } else if (message.method) {
        const waiters = this.events.get(message.method) || [];
        this.events.delete(message.method);
        waiters.forEach(resolve => resolve(message.params));
      }
    });
  }
  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  waitEvent(method) {
    return new Promise(resolve => {
      this.events.set(method, [...(this.events.get(method) || []), resolve]);
    });
  }
}

async function main() {
  const input = JSON.parse(await new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.on("data", chunk => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", reject);
  }));
  const target = new URL(input.url);
  const human = input.mode === "humansys";
  const research = input.mode === "research";
  const validAccount = !human && validAccountPrintTarget(target, input.number);
  const validHuman = human && target.origin === humanOrigin &&
    target.pathname.startsWith("/HumanSys/") &&
    /recordreport|print|pdf/i.test(target.pathname) &&
    (target.pathname.split("/").includes(input.number) ||
     [...target.searchParams.values()].includes(input.number));
  const validResearch = research && target.origin === humanOrigin &&
    target.pathname === `/ResearchSys/budget/budgetPrint/${input.budgetId}` &&
    target.searchParams.size === 1 && target.searchParams.get("action") === "pdf" &&
    /^\d{1,20}$/.test(input.budgetId) &&
    /^[A-Za-z0-9-]{1,30}$/.test(input.number);
  if ((!validAccount && !validHuman && !validResearch) ||
      (!research && !/^\d{1,20}$/.test(input.number)) ||
      typeof input.cookies !== "object" || !input.cookies) {
    throw new Error("Invalid print target");
  }
  const profile = await mkdtemp(join(tmpdir(), "ncu-print-"));
  let chrome, ws;
  try {
    stage = "chrome-start";
    chrome = spawn(chromePath, [
      "--headless=new", "--disable-gpu", "--no-first-run",
      "--no-default-browser-check", "--remote-debugging-port=0",
      `--user-data-dir=${profile}`, "about:blank",
    ], { stdio: "ignore" });
    const port = await devtoolsPort(profile);
    stage = "tab-open";
    const response = await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,
                                 { method: "PUT" });
    if (!response.ok) throw new Error("Cannot create print tab");
    const tab = await response.json();
    ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", reject, { once: true });
    });
    const cdp = new Cdp(ws);
    stage = "cookies";
    await cdp.send("Page.enable");
    await cdp.send("Network.enable");
    const cookies = (human || research)
      ? input.cookies.map(cookie => ({
          name: cookie.name, value: cookie.value,
          url: humanOrigin + cookie.path, secure: true,
        }))
      : Object.entries(input.cookies).map(([name, value]) => ({
          name, value, url: accountOrigin + "/APSWIS_Q/", secure: true,
        }));
    if (!cookies.length || cookies.some(cookie =>
      !cookie.name || !cookie.value ||
      ((human || research) && (!cookie.url.startsWith(
        humanOrigin + (human ? "/HumanSys" : "/ResearchSys")) ||
        !/^\/[A-Za-z0-9/_-]+$/.test(new URL(cookie.url).pathname))))) {
      throw new Error("Invalid session cookies");
    }
    await cdp.send("Network.setCookies", { cookies });
    stage = "navigate";
    await cdp.send("Page.navigate", { url: input.url });
    stage = "verify-page";
    let page;
    for (let attempt = 0; attempt < 100; attempt++) {
      const state = await cdp.send("Runtime.evaluate", {
        expression: "({url:location.href, ready:document.readyState, title:document.title||'', text:document.body?.innerText||'', password:!!document.querySelector('input[type=password]')})",
        returnByValue: true,
      });
      page = state.result?.value;
      const contentMatches = human
        ? `${page?.title || ""} ${page?.text || ""}`.includes("公差申請單")
        : research
        ? `${page?.title || ""} ${page?.text || ""}`.includes(input.number) &&
          /經分表|異動歷程/.test(`${page?.title || ""} ${page?.text || ""}`)
        : page?.text.includes(input.number);
      if (page?.ready === "complete" && contentMatches) break;
      await pause(200);
    }
    const finalUrl = new URL(page?.url || "about:blank");
    const contentMatches = human
      ? `${page?.title || ""} ${page?.text || ""}`.includes("公差申請單")
      : research
      ? `${page?.title || ""} ${page?.text || ""}`.includes(input.number) &&
        /經分表|異動歷程/.test(`${page?.title || ""} ${page?.text || ""}`)
      : page?.text.includes(input.number);
    if (finalUrl.origin !== target.origin || finalUrl.pathname !== target.pathname ||
        page.password || !contentMatches) {
      stage = finalUrl.origin !== target.origin ? "verify-origin"
        : finalUrl.pathname !== target.pathname ? "verify-path"
        : page.password ? "verify-login"
        : page?.title ? "verify-content-other-title" : "verify-content-no-title";
      throw new Error("Print page could not be verified");
    }
    stage = "print-pdf";
    const printOptions = human
      ? {
          printBackground: true,
          preferCSSPageSize: false,
          paperWidth: 8.2677165,
          paperHeight: 11.6929134,
          // The official form occupies a full A4 page. A slight reduction
          // leaves room above it without pushing its footer to a third page.
          scale: 0.98,
          marginTop: 0.15,
          marginBottom: 0,
          marginLeft: 0,
          marginRight: 0,
        }
      : { printBackground: true, preferCSSPageSize: true };
    const result = await cdp.send("Page.printToPDF", printOptions);
    const pdf = Buffer.from(result.data || "", "base64");
    if (!pdf.subarray(0, 5).equals(Buffer.from("%PDF-"))) {
      throw new Error("Chrome did not produce PDF");
    }
    process.stdout.write(pdf);
  } finally {
    if (ws) ws.close();
    if (chrome) {
      const stopped = new Promise(resolve => chrome.once("exit", resolve));
      chrome.kill();
      await Promise.race([stopped, pause(2000)]);
    }
    await rm(profile, { recursive: true, force: true, maxRetries: 3 });
  }
}

main().catch(() => {
  // Never print the target URL, session cookies, or private page content.
  process.stderr.write(`Unable to render official print page (${stage}).\n`);
  process.exitCode = 1;
});
