// UI smoke test for Phase 4: an admin sets up a container, role, user, grant and document
// entirely from the UI, then the new user asks a question and gets a cited answer.
//
//   npm install --prefix .scratch/e2e playwright-core
//   NODE_PATH=.scratch/e2e/node_modules CHROMIUM=/usr/bin/chromium BASE=http://127.0.0.1:8088 \
//     node scripts/ui_smoke.mjs
//
// Needs a seeded demo (admin@acme.test / $ERAG_DEMO_PASSWORD) and a running worker + Ollama.
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(path.resolve(process.env.NODE_PATH ?? ".", "x.js"));
const { chromium } = require("playwright-core");

const BASE = process.env.BASE ?? "http://127.0.0.1:8000";
const ADMIN_PW = process.env.ERAG_DEMO_PASSWORD ?? "acme-demo-password";
const DOC = process.env.UI_DOC ?? path.resolve(".scratch/ui-parking.md");
const OUT = process.env.UI_OUT ?? ".scratch";
const sfx = Date.now().toString().slice(-6);
const role = `UI Tester ${sfx}`;
const container = `UI Facilities ${sfx}`;
const email = `ui.tester.${sfx}@acme.test`;
const pw = "ui-tester-password-1";

// RFC 6238 TOTP (SHA-1, 6 digits, 30 s), to log in with MFA like an authenticator app would.
import crypto from "node:crypto";
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

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM ?? "/usr/bin/chromium" });
const step = (s) => console.log(`- ${s}`);
const pages = [];

async function login(page, user, password) {
  await page.goto(BASE);
  await page.getByLabel("Email").fill(user);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "Chat" }).waitFor();
}

try {
  const admin = await (await browser.newContext()).newPage();
  pages.push(admin);
  admin.on("pageerror", (e) => console.error("page error:", e.message));
  await login(admin, "admin@acme.test", ADMIN_PW);
  step("admin logged in");
  await admin.getByRole("button", { name: "Admin" }).click();

  await admin.getByRole("button", { name: "Roles", exact: true }).click();
  await admin.getByPlaceholder("role name").fill(role);
  await admin.getByRole("button", { name: "Create role" }).click();
  await admin.getByText(role).waitFor();
  step(`role created: ${role}`);

  await admin.getByRole("button", { name: "Containers", exact: true }).click();
  await admin.getByPlaceholder("name", { exact: true }).fill(container);
  await admin.getByPlaceholder("description").fill("Created from the UI smoke test");
  await admin.getByRole("button", { name: "Create", exact: true }).click();
  const row = admin.locator("tr", { hasText: container });
  await row.waitFor();
  await row.getByRole("button", { name: "Documents" }).click();
  await admin.getByTestId("upload-input").setInputFiles(DOC);
  await admin.getByLabel("Upload queue").getByText("queued").waitFor();
  await admin.locator("tr", { hasText: "ui parking" }).getByText("ready").waitFor({ timeout: 60_000 });
  step(`container created, document uploaded and ingested: ${container}`);

  await admin.getByRole("button", { name: "Users", exact: true }).click();
  await admin.getByPlaceholder("email").fill(email);
  await admin.getByPlaceholder("display name").fill("UI Tester");
  await admin.getByPlaceholder("password (12+ chars)").fill(pw);
  await admin.locator("label", { hasText: role }).first().locator("input").check();
  await admin.getByRole("button", { name: "Create", exact: true }).click();
  await admin.locator("td", { hasText: email }).waitFor();
  step(`user created: ${email}`);

  await admin.getByRole("button", { name: "Grants", exact: true }).click();
  const cell = admin.getByLabel(`${role} on ${container}`);
  await cell.selectOption("read");
  await admin.waitForFunction(
    (label) => document.querySelector(`select[aria-label="${label}"]`)?.value === "read",
    `${role} on ${container}`,
  );
  await admin.getByLabel("preview user").selectOption({ label: email });
  await admin.getByTestId("effective").getByText(container).waitFor();
  step("grant created in the matrix: read on the container; effective-permissions preview shows it");
  await admin.screenshot({ path: `${OUT}/ui-admin-grants.png`, fullPage: true });

  // R2: credential store (write-only) and the provider key source.
  await admin.getByRole("button", { name: "Secrets", exact: true }).click();
  await admin.getByPlaceholder("name (e.g. erp_token)").fill(`ui_smoke_${sfx}`);
  await admin.getByPlaceholder("value").fill("not-a-real-secret-value");
  await admin.getByRole("button", { name: "Save secret" }).click();
  await admin.locator("td", { hasText: `ui_smoke_${sfx}` }).waitFor();
  if ((await admin.content()).includes("not-a-real-secret-value")) throw new Error("secret value visible in the page");
  admin.once("dialog", (d) => d.accept());
  await admin.getByLabel(`Delete secret ui_smoke_${sfx}`).click();
  await admin.locator("td", { hasText: `ui_smoke_${sfx}` }).waitFor({ state: "detached" });
  await admin.getByRole("button", { name: "Providers", exact: true }).click();
  const orStatus = await admin.getByTestId("openrouter-status").innerText();
  step(`secret stored (value never shown) and deleted; OpenRouter: ${orStatus.split("\n")[0]}`);

  // R3: connectors. The seeded folder connector is listed; preview is a dry run; a path outside the
  // allowed roots is refused.
  await admin.getByRole("button", { name: "Connectors", exact: true }).click();
  const wiki = admin.locator('tr[data-connector="Engineering wiki (folder)"]');
  await wiki.waitFor();
  await wiki.getByRole("button", { name: "Preview" }).click();
  const pv = await admin.getByTestId("preview").innerText();
  await wiki.getByRole("button", { name: "Sync now" }).click();
  await wiki.getByRole("button", { name: "Runs" }).click();
  await admin.getByTestId("runs").getByText("done").first().waitFor({ timeout: 60_000 });
  await admin.getByPlaceholder("connector name").fill(`escape-${sfx}`);
  await admin.getByLabel("target container").selectOption({ label: `${container} (internal)` });
  await admin.getByPlaceholder("e.g. acme/engineering-wiki").fill("/etc");
  await admin.getByRole("button", { name: "Create connector" }).click();
  await admin.getByText("is not a folder inside an allowed root").waitFor();
  step(`connectors: preview "${pv.split("\n")[0]}", sync run done, /etc refused`);

  // R4: the seeded remote MCP server (pending) is approved, two of its tools are enabled and granted to
  // the Engineering role only. Clearance raised to internal so engineering conversations may use it.
  await admin.getByRole("button", { name: "MCP registry", exact: true }).click();
  const office = admin.locator('[data-server="acme-office"]');
  await office.waitFor();
  const officeTools = await (await admin.evaluate(() => fetch("/api/admin/registry/servers").then((r) => r.json())))
    .find((x) => x.name === "acme-office").tools;
  if (officeTools.some((t) => t.enabled) && !process.env.UI_REUSE) console.log("  (office tools already enabled from an earlier run)");
  await office.getByLabel("status of acme-office").selectOption("approved");
  await office.getByLabel("clearance of acme-office").selectOption("internal");
  // Controlled checkboxes: click, then wait for the server state to come back.
  const setBox = async (label, want) => {
    const box = office.getByLabel(label);
    if ((await box.isChecked()) !== want) await box.click();
    await admin.waitForFunction(
      ([sel, w]) => document.querySelector(sel)?.checked === w,
      [`[data-server="acme-office"] input[aria-label="${label}"]`, want],
    );
  };
  for (const t of ["list_holidays", "convert_currency"]) {
    await setBox(`enable ${t}`, true);
    await setBox(`Engineering may execute ${t}`, true);
  }
  await setBox("enable office_info", false);
  const toolsFor = async (email) => {
    const page = await (await browser.newContext()).newPage();
    pages.push(page);
    await login(page, email, ADMIN_PW);
    return (await page.evaluate(() => fetch("/api/tools").then((r) => r.json()))).map((t) => t.name);
  };
  const engTools = await toolsFor("engineer@acme.test");
  const hireTools = await toolsFor("newhire@acme.test");
  if (!engTools.includes("acme-office__list_holidays") || !engTools.includes("acme-office__convert_currency") || engTools.includes("acme-office__office_info"))
    throw new Error(`engineer tools: ${engTools}`);
  if (hireTools.some((t) => t.startsWith("acme-office__"))) throw new Error(`new hire sees office tools: ${hireTools}`);
  await admin.screenshot({ path: `${OUT}/ui-mcp-registry.png`, fullPage: true });
  step(`MCP registry: acme-office approved, 2 tools for Engineering; engineer sees ${engTools.filter((t) => t.startsWith("acme")).join(", ")}; new hire sees none`);

  // R5: an MCP packaged as an approved image runs in the isolated runner (no network, read-only, no caps).
  if (process.env.SKIP_RUNNER !== "1") {
    await admin.getByRole("button", { name: "Secrets", exact: true }).click();
    await admin.getByPlaceholder("name (e.g. erp_token)").fill("office_booking_token");
    await admin.getByPlaceholder("value").fill(`booking-${sfx}-not-real`);
    await admin.getByRole("button", { name: "Save secret" }).click();
    await admin.locator("td", { hasText: "office_booking_token" }).waitFor();
    await admin.getByRole("button", { name: "MCP registry", exact: true }).click();
    const iso = admin.locator('[data-server="office-isolated"]');
    if ((await iso.count()) === 0) {
      await admin.getByLabel("transport").selectOption("runner");
      await admin.getByPlaceholder("name (e.g. acme-office)").fill("office-isolated");
      await admin.getByLabel("image").selectOption("office");
      await admin.getByPlaceholder("secrets as ENV_VAR=secret_name").fill("ERAG_OFFICE_API_TOKEN=office_booking_token");
      await admin.getByLabel("clearance", { exact: true }).selectOption("internal");
      await admin.getByRole("button", { name: "Register and discover tools" }).click();
    }
    await iso.waitFor({ timeout: 60_000 });
    await iso.getByLabel("status of office-isolated").selectOption("approved");
    const setIso = async (label, want) => {
      const box = iso.getByLabel(label);
      if ((await box.isChecked()) !== want) await box.click();
      await admin.waitForFunction(
        ([sel, w]) => document.querySelector(sel)?.checked === w,
        [`[data-server="office-isolated"] input[aria-label="${label}"]`, want],
      );
    };
    await setIso("enable office_info", true);
    await setIso("Engineering may execute office_info", true);
    await iso.getByRole("button", { name: "Start" }).click();
    await iso.getByTestId("runner-state").getByText("running").waitFor({ timeout: 60_000 });
    const panel = await iso.getByTestId("runner-panel").innerText();
    if (!panel.includes("network none") || !panel.includes("read-only root true")) throw new Error(`runner panel: ${panel}`);
    if (panel.includes(`booking-${sfx}-not-real`)) throw new Error("secret visible in logs");
    await admin.screenshot({ path: `${OUT}/ui-runner.png`, fullPage: true });
    step(`runner: office-isolated registered from the approved image, approved, started (${panel.split("\n")[0].slice(0, 140)})`);
  }

  const user = await (await browser.newContext()).newPage();
  pages.push(user);
  await login(user, email, pw);
  await user.getByPlaceholder("Ask a question (Enter to send)").fill("Where do employees park?");
  await user.getByRole("button", { name: "Send" }).click();
  const cite = user.getByRole("button", { name: /\[1\] ui parking/ });
  await cite.waitFor({ timeout: 180_000 });
  await user.getByText("thinking...").waitFor({ state: "detached", timeout: 180_000 });
  await cite.click();
  await user.getByText("GREEN-OTTER-77").last().waitFor();
  const answer = await user.locator("p.whitespace-pre-wrap").last().innerText();
  step(`new user got a cited answer: ${JSON.stringify(answer.slice(0, 200))}`);
  await user.screenshot({ path: `${OUT}/ui-chat.png`, fullPage: true });

  // R2: the user enrols in MFA and logs in again with a TOTP code.
  await user.getByRole("button", { name: "Account" }).click();
  await user.getByRole("button", { name: "Set up MFA" }).click();
  const secret = (await user.getByTestId("mfa-secret").innerText()).trim();
  await user.getByLabel("Code").fill(totp(secret));
  await user.getByRole("button", { name: "Confirm and enable" }).click();
  await user.getByTestId("recovery-codes").waitFor();
  await user.getByRole("button", { name: "Log out" }).click();
  await user.getByLabel("Email").fill(email);
  await user.getByLabel("Password").fill(pw);
  await user.getByRole("button", { name: "Sign in" }).click();
  await user.getByLabel("Authenticator code (or a recovery code)").fill(totp(secret, Date.now() / 1000 + 30));
  await user.getByRole("button", { name: "Verify" }).click();
  await user.getByRole("button", { name: "Chat" }).waitFor();
  step("user enabled MFA (QR + recovery codes) and logged in again with password + TOTP");

  // R1 exit: a department head (manage on HR Confidential only) uploads and replaces a document in
  // their own container, and sees nothing else to manage.
  const hr = await (await browser.newContext()).newPage();
  pages.push(hr);
  await login(hr, "hr@acme.test", ADMIN_PW);
  await hr.getByRole("button", { name: "Content" }).click();
  const tabs = await hr.locator("main button").allInnerTexts();
  if (!tabs.includes("HR Confidential") || tabs.some((t) => ["Finance", "Engineering", "General Handbook"].includes(t)))
    throw new Error(`manager sees unexpected containers: ${tabs}`);
  const hrDoc = path.resolve(OUT, `hr-onboarding-${sfx}.md`);
  const fs = await import("node:fs");
  fs.writeFileSync(hrDoc, `# HR onboarding checklist ${sfx}\n\nNew employees meet their HR partner on day one.\n`);
  await hr.getByTestId("upload-input").setInputFiles(hrDoc);
  const hrRow = hr.locator(`tr[data-doc="hr-onboarding-${sfx}.md"]`);
  await hrRow.getByText("ready").waitFor({ timeout: 60_000 });
  fs.writeFileSync(hrDoc, `# HR onboarding checklist ${sfx}\n\nNew employees meet their HR partner on day one and day five.\n`);
  await hrRow.locator(`input[data-replace="hr-onboarding-${sfx}.md"]`).setInputFiles(hrDoc);
  await hrRow.locator("td").nth(4).getByText("2").waitFor({ timeout: 30_000 });
  await hrRow.getByText("ready").waitFor({ timeout: 60_000 });
  step("HR manager uploaded and replaced a document in HR Confidential (2 versions, ready)");
  hr.once("dialog", (d) => d.accept());
  await hrRow.getByRole("button", { name: "Delete document" }).click();
  await hrRow.waitFor({ state: "detached" });
  step("HR manager deleted it again");
  const adminTab = await hr.getByRole("button", { name: "Admin" }).count();
  if (adminTab !== 0) throw new Error("manager sees the Admin tab");
  await hr.screenshot({ path: `${OUT}/ui-content-manager.png`, fullPage: true });
  console.log("UI SMOKE PASSED");
} catch (err) {
  console.error("UI SMOKE FAILED:", err);
  for (const [i, p] of pages.entries()) await p.screenshot({ path: `${OUT}/ui-failure-${i}.png`, fullPage: true });
  process.exitCode = 1;
} finally {
  await browser.close();
}
