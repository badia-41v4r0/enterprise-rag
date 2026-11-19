// Browser end-to-end test of the setup wizard (PLAN-v0.4 section 9). Run through scripts/setup_e2e.sh, which
// creates a throw-away installation with `erag init` and passes SETUP_TOKEN and BASE.
//
// The administrator completes every step in the browser (with MFA), builds a small structure by hand (two
// containers, two roles, two people from a CSV), uploads two documents, checks "Does it work?" for both people,
// downloads the setup report, finishes; then both people log in, change their temporary passwords, ask the same
// question and get different answers.
import crypto from "node:crypto";
import fs from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.resolve(process.env.NODE_PATH ?? ".", "x.js"));
const { chromium } = require("playwright-core");

const BASE = process.env.BASE ?? "http://127.0.0.1:8099";
const TOKEN = process.env.SETUP_TOKEN;
const OUT = process.env.OUT ?? ".scratch";
const SHOTS = process.env.SHOTS ?? "docs/media";
const ADMIN = { email: "it.admin@nimbus.test", name: "Nina IT", password: "Nimbus-Setup-Admin-2026!" };
const CANARY = "PAYROLL-CANARY-7731";
const Q = "What is the salary band for a data engineer at Nimbus?";
if (!TOKEN) throw new Error("SETUP_TOKEN is required (run scripts/setup_e2e.sh)");

function base32(s) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const ch of s.replace(/=+$/, "")) bits += alphabet.indexOf(ch).toString(2).padStart(5, "0");
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}
function totp(secret, seconds = Date.now() / 1000) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(seconds / 30)));
  const h = crypto.createHmac("sha1", base32(secret)).update(counter).digest();
  const o = h[h.length - 1] & 0xf;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}
// A TOTP code is accepted once per 30-second step: wait for a fresh step when the last one was used.
let lastStep = 0;
async function freshCode(secret) {
  let step = Math.floor(Date.now() / 30000);
  while (step <= lastStep) {
    await new Promise((r) => setTimeout(r, 1000));
    step = Math.floor(Date.now() / 30000);
  }
  lastStep = step;
  return totp(secret);
}

const step = (s) => console.log(`- ${s}`);
const docsDir = path.join(OUT, "setup-e2e-docs");
fs.mkdirSync(docsDir, { recursive: true });
fs.writeFileSync(
  path.join(docsDir, "nimbus-handbook.md"),
  "# Nimbus Analytics employee handbook\n\n## Salaries\n\nSalary bands are confidential. Employees who want to know the salary band for a role, " +
    "for example a data engineer, should ask the payroll team. The handbook does not list any salary figures.\n\n## Holidays\n\nEveryone has 27 days of holiday per year.\n",
);
fs.writeFileSync(
  path.join(docsDir, "nimbus-salary-bands.md"),
  `# Nimbus Analytics salary bands 2026 (payroll only)\n\n## Data engineer\n\nThe salary band for a data engineer at Nimbus is band B4: 78,000 to 92,000 EUR per year. Reference ${CANARY}.\n\n## Analyst\n\nAnalysts are in band B2: 52,000 to 61,000 EUR per year.\n`,
);

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? "/usr/bin/chromium" });
let mfaSecret = "";
const shot = async (page, name) => {
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, name), fullPage: false });
};

// Sensitive actions ask "Confirm it is you": the administrator has MFA, so a fresh TOTP code.
async function confirmIfAsked(page) {
  const dlg = page.getByRole("dialog", { name: "Confirm it is you" });
  try {
    await dlg.waitFor({ timeout: 2500 });
  } catch {
    return;
  }
  await dlg.getByLabel("Code").fill(await freshCode(mfaSecret));
  await dlg.getByRole("button", { name: "Confirm" }).click();
  await dlg.waitFor({ state: "detached" });
}

async function stepIs(page, key) {
  await page.locator(`main[data-current-step="${key}"]`).waitFor({ timeout: 30_000 });
}

// MODE=demo: "Demo with mock data", the banner, finishing with skipped steps, then Remove demo data from the
// Setup assistant (with the confirmation that lists what goes, and step-up).
async function demoFlow(page) {
  await page.getByLabel("Demo with mock data").check();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "organization");
  await page.getByRole("status").getByText("Demo installation, not for real data").waitFor();
  step("3. demo installed; the banner is shown");
  const login = async (email) =>
    (await fetch(`${BASE}/api/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password: "acme-demo-password" }) })).status;
  if ((await login("admin@acme.test")) !== 401) throw new Error("the demo must not create admin@acme.test");
  if ((await login("hr@acme.test")) !== 200) throw new Error("demo personas should exist");
  for (const key of ["organization", "models", "access", "people", "knowledge", "check", "tools"]) {
    await stepIs(page, key);
    await page.getByRole("button", { name: "Skip this step" }).click();
  }
  await stepIs(page, "finish");
  await page.getByText("This is a demo installation with published passwords").waitFor();
  await page.getByRole("button", { name: "Finish setup" }).click();
  await page.getByRole("button", { name: "Admin" }).waitFor({ timeout: 30_000 });
  await page.getByTestId("demo-banner").waitFor();
  await page.getByTestId("setup-checklist").locator('[data-item="remove_demo"][data-done="false"]').waitFor();
  step("4-10 skipped, 11 finished with the private-network warning; the checklist asks to remove the demo data");
  await page.getByRole("button", { name: "Setup assistant", exact: true }).click();
  await page.getByRole("button", { name: "3. Installation type" }).click();
  await page.getByRole("button", { name: "Remove demo data" }).click();
  const inv = await page.getByTestId("demo-inventory").innerText();
  if (!inv.includes("hr@acme.test") || !inv.includes("HR Confidential") || inv.includes(ADMIN.email)) throw new Error(`inventory: ${inv}`);
  await shot(page, "setup-remove-demo.png");
  // The step-up dialog may appear (the login is older than the step-up window or MFA is on).
  await page.getByRole("button", { name: "Delete demo data" }).click();
  await confirmIfAsked(page);
  await page.getByText("No demo data is installed.").waitFor({ timeout: 60_000 });
  await page.getByTestId("demo-banner").waitFor({ state: "detached", timeout: 15_000 });
  if ((await login("hr@acme.test")) !== 401) throw new Error("demo personas should be gone");
  step("Remove demo data: confirmation listed users, roles, containers; after step-up the demo is gone and the banner too");
}

try {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await ctx.newPage();
  page.on("pageerror", (e) => console.error("page error:", e.message));

  // Setup mode: only the wizard; the API answers 503 elsewhere.
  const blocked = await (await fetch(`${BASE}/api/admin/users`)).json();
  if (blocked.detail !== "setup required") throw new Error("expected 503 setup required before setup");
  await page.goto(BASE);
  await page.getByRole("heading", { name: "Enter the setup token" }).waitFor();
  await shot(page, "setup-token.png");
  await page.getByLabel("Setup token").fill("WRONG-TOKEN");
  await page.getByRole("button", { name: "Continue" }).click();
  await page.getByRole("alert").getByText("Invalid or expired setup token").waitFor();
  await page.getByLabel("Setup token").fill(TOKEN);
  await page.getByRole("button", { name: "Continue" }).click();
  await stepIs(page, "license");
  step("setup token: a wrong one refused, the real one accepted");

  // 0. License: Continue is disabled until the box is ticked.
  const text = await page.getByTestId("license-text").innerText();
  if (!text.includes("PolyForm Noncommercial License 1.0.0") || !text.includes("60-day evaluation permit")) throw new Error("license text");
  const cont = page.getByRole("button", { name: "Continue", exact: true });
  await page.getByLabel("Your name (recorded with the acceptance)").fill(ADMIN.name);
  if (!(await cont.isDisabled())) throw new Error("Continue must stay disabled until the checkbox is ticked");
  await shot(page, "setup-license.png");
  await page.getByLabel("I have read this notice and accept the license terms for this installation").check();
  await cont.click();
  await stepIs(page, "environment");
  step("0. license accepted (checkbox required)");

  // 1. Environment check (the worker reports within a few seconds of starting: check again until it has).
  await page.getByTestId("env-checks").locator('[data-check="database"]').waitFor({ timeout: 30_000 });
  for (let i = 0; i < 20 && (await page.locator('[data-check="worker"]').getAttribute("data-status")) !== "ok"; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    await page.getByRole("button", { name: "Check again" }).click();
    await page.waitForTimeout(1500);
  }
  const statuses = await page.locator("[data-check]").evaluateAll((els) => Object.fromEntries(els.map((e) => [e.dataset.check, e.dataset.status])));
  for (const k of ["database", "master_key", "production", "secrets", "storage", "worker", "ollama", "embedding_model", "chat_model"]) {
    if (statuses[k] !== "ok") throw new Error(`environment check ${k} is ${statuses[k]}`);
  }
  await shot(page, "setup-environment.png");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "admin");
  step(`1. environment: ${JSON.stringify(statuses)}`);

  // 2. Administrator + MFA.
  await page.getByLabel("Email").fill(ADMIN.email);
  await page.getByLabel("Name").fill(ADMIN.name);
  await page.getByLabel("Password (12 or more characters)").fill("short");
  await page.getByLabel("Password (12 or more characters)").fill(ADMIN.password);
  await page.getByLabel("Repeat the password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Create administrator" }).click();
  await page.getByText("Administrator created and signed in.").waitFor();
  await page.getByRole("button", { name: "Set up MFA" }).click();
  mfaSecret = (await page.getByTestId("mfa-secret").innerText()).trim();
  await page.locator("#mfa-code").fill(await freshCode(mfaSecret));
  await page.getByRole("button", { name: "Confirm and enable" }).click();
  await page.getByTestId("recovery-codes").waitFor();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "install_type");
  step("2. administrator created and signed in; MFA enrolled, recovery codes shown once");

  if (process.env.MODE === "demo") {
    await demoFlow(page);
    console.log("SETUP E2E (DEMO) PASSED");
    await browser.close();
    process.exit(0);
  }

  // 3. Installation type: empty (the default).
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "organization");
  step("3. empty installation");

  // 4. Organization.
  await page.getByLabel("Company name").fill("Nimbus Analytics");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await confirmIfAsked(page);
  await stepIs(page, "models");
  step("4. organization: Nimbus Analytics, cloud ceiling public");

  // 5. Models: tests, chat model, and a download confirmation that is cancelled (nothing downloaded).
  await page.getByTestId("installed-models").waitFor({ timeout: 30_000 });
  const qwen = "qwen3:4b-instruct-2507-q4_K_M";
  const useBtn = page.getByLabel(`use ${qwen}`);
  if (await useBtn.count()) {
    await useBtn.click();
    await confirmIfAsked(page);
  }
  await page.locator(`[data-model="${qwen}"]`).getByText("chat model").waitFor({ timeout: 15_000 });
  const tests = page.getByRole("button", { name: "Test" });
  await tests.nth(0).click();
  await page.getByTestId("test-embedding").getByText("1024 dimensions").waitFor({ timeout: 60_000 });
  await tests.nth(1).click();
  await page.getByTestId("test-chat").getByText("OK in").waitFor({ timeout: 120_000 });
  await page.getByLabel("model to download").fill("all-minilm");
  await page.getByRole("button", { name: "Download", exact: true }).click();
  const dlg = page.getByRole("dialog", { name: "Confirm the download" });
  await dlg.waitFor({ timeout: 30_000 });
  const confirmText = await dlg.innerText();
  if (!confirmText.includes("all-minilm") || !confirmText.includes("MB") || !confirmText.includes("Ollama registry")) throw new Error(`download confirmation: ${confirmText}`);
  await shot(page, "setup-models.png");
  await dlg.getByRole("button", { name: "Cancel" }).click();
  const tags = await (await fetch("http://127.0.0.1:11434/api/tags")).json();
  if (tags.models.some((m) => m.name.startsWith("all-minilm"))) throw new Error("a model was downloaded without confirmation");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "access");
  step(`5. models: chat ${qwen}, embedding and chat Test OK; download confirmation shows name, size and source; cancelled, nothing downloaded`);

  // 6. Access structure, by hand.
  await page.getByTestId("access-missing").waitFor();
  const makeContainer = async (name, cls) => {
    await page.getByPlaceholder("name", { exact: true }).fill(name);
    await page.getByPlaceholder("description").fill(`${name} documents`);
    await page.locator("select").filter({ hasText: "confidential" }).first().selectOption(cls);
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await page.getByRole("cell", { name, exact: true }).first().waitFor();
  };
  await makeContainer("Handbook", "internal");
  await makeContainer("Payroll", "confidential");
  for (const r of ["Staff", "Payroll team"]) {
    await page.getByPlaceholder("role name").fill(r);
    await page.getByRole("button", { name: "Create role" }).click();
    await page.getByRole("cell", { name: r, exact: true }).first().waitFor();
  }
  const setLevel = async (label, level) => {
    await page.getByLabel(label).selectOption(level);
    await page.waitForFunction(([l, v]) => document.querySelector(`select[aria-label="${l}"]`)?.value === v, [label, level]);
  };
  await setLevel("Staff on Handbook", "read");
  await setLevel("Payroll team on Handbook", "read");
  await setLevel("Payroll team on Payroll", "read");
  await page.getByTestId("access-missing").waitFor({ state: "detached", timeout: 10_000 });
  await shot(page, "setup-access.png");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "people");
  step("6. access structure built by hand: Handbook (internal), Payroll (confidential), Staff and Payroll team");

  // 7. People from a CSV, with a bad row reported.
  await page.getByLabel(/Or paste a list/).fill("email,name,role\nana@nimbus.test,Ana Staff,Staff\nben@nimbus.test,Ben Payroll,Payroll team\nzoe@nimbus.test,Zoe,Marketing\n");
  await page.getByRole("button", { name: "Import" }).click();
  await page.getByTestId("csv-errors").getByText("Line 4 (zoe@nimbus.test): unknown role: Marketing").waitFor();
  const temp = {};
  for (const who of ["ana", "ben"]) {
    const line = await page.getByTestId("temp-passwords").locator(`[data-email="${who}@nimbus.test"]`).innerText();
    temp[who] = line.split(": ")[1].trim();
  }
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "knowledge");
  step("7. people: Ana (Staff) and Ben (Payroll team) imported with temporary passwords; Zoe refused (unknown role)");

  // 8. Knowledge: one document per container, live progress until both are ready.
  const upload = async (container, file) => {
    await page.getByLabel("Container", { exact: true }).selectOption({ label: `${container} (${container === "Payroll" ? "confidential" : "internal"})` });
    await page.getByTestId("upload-input").setInputFiles(path.join(docsDir, file));
    await page.getByLabel("Upload queue").getByText("queued").waitFor({ timeout: 30_000 });
  };
  await upload("Handbook", "nimbus-handbook.md");
  await upload("Payroll", "nimbus-salary-bands.md");
  await page.getByTestId("ingestion-progress").getByText("ready 2").waitFor({ timeout: 180_000 });
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "check");
  step("8. knowledge: two documents uploaded and indexed (live progress)");

  // 9. Does it work? Same question as Ana and as Ben.
  for (const who of ["ana", "ben"]) {
    await page.getByLabel("Ask as").selectOption({ label: `${who}@nimbus.test` });
    await page.getByLabel("Question").fill(Q);
    await page.getByRole("button", { name: "Ask" }).click();
    await page.locator(`[data-testid="check-result"][data-user="${who}@nimbus.test"]`).waitFor({ timeout: 240_000 });
  }
  const anaCheck = await page.locator('[data-testid="check-result"][data-user="ana@nimbus.test"]').innerText();
  const benCheck = await page.locator('[data-testid="check-result"][data-user="ben@nimbus.test"]').innerText();
  if (anaCheck.includes("Salary bands 2026") || anaCheck.includes(CANARY) || anaCheck.includes("B4")) throw new Error(`Ana saw payroll data: ${anaCheck}`);
  if (!benCheck.includes("Payroll")) throw new Error(`Ben's check shows no payroll source: ${benCheck}`);
  await shot(page, "setup-check.png");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "tools");
  step("9. does it work: Ana sees only the handbook, Ben's answer uses the payroll document");

  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await stepIs(page, "finish");
  step("10. tools and router left off");

  // 11. Review, report (no secrets) and finish.
  await page.getByTestId("security-checklist").waitFor();
  const report = await page.evaluate(() => fetch("/api/setup/report").then((r) => r.text()));
  for (const secret of [TOKEN, ADMIN.password, temp.ana, temp.ben, mfaSecret]) {
    if (report.includes(secret)) throw new Error("the setup report contains a secret");
  }
  if (!report.includes("Nimbus Analytics") || !report.includes("Payroll (confidential)")) throw new Error("report content");
  fs.writeFileSync(path.join(OUT, "setup-report-e2e.md"), report);
  const mfaItem = await page.locator('[data-item="mfa"]').getAttribute("data-status");
  if (mfaItem !== "ok") throw new Error("MFA should be ok in the security checklist");
  await shot(page, "setup-review.png");
  await page.getByRole("button", { name: "Finish setup" }).click();
  await page.getByRole("button", { name: "Admin" }).waitFor({ timeout: 30_000 });
  await page.getByTestId("setup-checklist").waitFor();
  await page.getByTestId("company-name").getByText("Nimbus Analytics").waitFor();
  await shot(page, "setup-checklist.png");
  const after = await fetch(`${BASE}/api/setup/status`);
  if (after.status !== 404) throw new Error(`setup endpoints must 404 after completion, got ${after.status}`);
  const reuse = await fetch(`${BASE}/api/setup/token`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: TOKEN }) });
  if (reuse.status !== 404) throw new Error("the setup token must not work after completion");
  step("11. report has no secrets; setup finished; admin signed in with the checklist card; setup endpoints 404; token dead");

  // Both people log in, must change the temporary password, then ask the same question.
  const asUser = async (who) => {
    const p = await (await browser.newContext()).newPage();
    await p.goto(BASE);
    await p.getByLabel("Email").fill(`${who}@nimbus.test`);
    await p.getByLabel("Password").fill(temp[who]);
    await p.getByRole("button", { name: "Sign in" }).click();
    await p.getByRole("heading", { name: "Choose your own password" }).waitFor();
    if (who === "ana") await shot(p, "setup-change-password.png");
    await p.getByLabel("Current (temporary) password").fill(temp[who]);
    await p.getByLabel("New password", { exact: true }).fill(`${who}-Own-Passw0rd-2026`);
    await p.getByLabel("Repeat the new password").fill(`${who}-Own-Passw0rd-2026`);
    await p.getByRole("button", { name: "Save and continue" }).click();
    await p.getByRole("button", { name: "Chat" }).waitFor();
    const res = await p.evaluate(async (q) => {
      const me = await fetch("/api/auth/me").then((r) => r.json());
      const r = await fetch("/api/chat", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token }, body: JSON.stringify({ message: q }) });
      const events = (await r.text()).split("\n").filter((l) => l.startsWith("data:")).map((l) => JSON.parse(l.slice(5)));
      const done = events.find((e) => e.type === "done") ?? {};
      const cites = events.find((e) => e.type === "citations")?.items ?? [];
      return { answer: done.answer ?? "", model: done.model, classification: done.classification, sources: cites.map((c) => `${c.title} (${c.container})`) };
    }, Q);
    return res;
  };
  const ana = await asUser("ana");
  const ben = await asUser("ben");
  console.log(`  ana: model=${ana.model} class=${ana.classification} sources=${JSON.stringify(ana.sources)} answer=${JSON.stringify(ana.answer.slice(0, 160))}`);
  console.log(`  ben: model=${ben.model} class=${ben.classification} sources=${JSON.stringify(ben.sources)} answer=${JSON.stringify(ben.answer.slice(0, 160))}`);
  if (ana.sources.some((s) => s.includes("Payroll")) || ana.answer.includes("92,000") || ana.answer.includes(CANARY)) throw new Error("Ana got payroll data");
  if (!ben.sources.some((s) => s.includes("Payroll"))) throw new Error("Ben's answer is not grounded in the payroll document");
  if (ana.answer === ben.answer) throw new Error("expected different answers for different roles");
  if (ben.classification !== "confidential") throw new Error("Ben's answer should be confidential (local model)");
  step("both people changed their temporary passwords; same question, different answers (Ana: handbook only; Ben: payroll, confidential, local model)");
  console.log("SETUP E2E PASSED");
} catch (err) {
  console.error("SETUP E2E FAILED:", err.message);
  const pages = browser.contexts().flatMap((c) => c.pages());
  for (const [i, p] of pages.entries()) await p.screenshot({ path: path.join(OUT, `setup-e2e-failure-${i}.png`), fullPage: true }).catch(() => {});
  process.exitCode = 1;
} finally {
  await browser.close();
}
