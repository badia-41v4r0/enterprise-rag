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
  await admin.locator('input[type="file"]').setInputFiles(DOC);
  await admin.getByRole("button", { name: "Upload" }).click();
  await admin.getByText("Queued for ingestion.").waitFor();
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
  const selects = admin.locator("select");
  await selects.nth(0).selectOption({ label: role });
  await selects.nth(1).selectOption("container");
  await selects.nth(2).selectOption({ label: container });
  await selects.nth(3).selectOption("read");
  await admin.getByRole("button", { name: "Add grant" }).click();
  await admin.locator("tr", { hasText: container }).waitFor();
  step("grant created: read on the container");
  await admin.screenshot({ path: `${OUT}/ui-admin-grants.png`, fullPage: true });

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
  console.log("UI SMOKE PASSED");
} catch (err) {
  console.error("UI SMOKE FAILED:", err);
  for (const [i, p] of pages.entries()) await p.screenshot({ path: `${OUT}/ui-failure-${i}.png`, fullPage: true });
  process.exitCode = 1;
} finally {
  await browser.close();
}
