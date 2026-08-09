import { chromium } from "/config/.tools/npm-global/lib/node_modules/@playwright/mcp/node_modules/playwright/index.mjs";

const baseUrl = process.argv[2] || "http://127.0.0.1:18099/";
const executablePath = process.env.CHROMIUM_PATH || "/config/.tools/bin/chromium";
const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage();

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.waitForSelector("#server-select option[value=body]", { state: "attached" });
  assert(await page.locator("select").count() === 2, "UI must have exactly two dropdowns");
  assert(await page.locator("textarea[spellcheck=false]").count() === 1, "raw textarea missing");
  assert((await page.locator("body").innerText()).includes("機密情報を貼り付けない"), "secret warning missing");

  const initialBranch = await page.locator("#state-summary-grid").innerText();
  assert(initialBranch.includes("generation/"), "generation state is not visible");

  await page.selectOption("#server-select", "body");
  await page.waitForSelector("#tool-select option[value=move_to]", { state: "attached" });
  await page.selectOption("#tool-select", "move_to");
  const template = await page.locator("#tool-input").inputValue();
  assert(!template.includes("\n"), "template must be compact one-line JSON");
  assert(JSON.stringify(JSON.parse(template)) === '{"reason":"","room":""}', "template contract mismatch");

  await page.locator("#tool-input").fill("{");
  assert(await page.locator("#send-tool-btn").isEnabled(), "invalid JSON must not disable Send");
  await page.click("#send-tool-btn");
  await page.waitForFunction(() => document.querySelector("#res-input-class")?.textContent === "invalid_json", null, { timeout: 5000 });
  assert(await page.locator("#res-response-id").textContent() === "false", "false response id must remain visible");
  assert(!(await page.locator("body").innerText()).includes("ui-sentinel"), "child secret leaked into DOM");

  page.once("dialog", dialog => dialog.accept());
  await page.click("#reset-state-btn");
  await page.waitForFunction(() => document.querySelector("#res-state-changes")?.textContent?.includes("new_branch"), null, { timeout: 5000 });
  const resetEvidence = await page.locator("#res-state-changes").innerText();
  assert(resetEvidence.includes("old_branch") && resetEvidence.includes("new_head"), "reset evidence incomplete");
  console.log("UI smoke passed");
} finally {
  await browser.close();
}
