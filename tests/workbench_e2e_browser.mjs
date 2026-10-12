// Browser acceptance: supervised production workers, guarded repairs and reviewed application.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {resolve} from "node:path";
import {createInterface} from "node:readline";
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.FIXLOOP_PLAYWRIGHT_MODULE || "playwright");
const root = resolve(process.env.FIXLOOP_E2E_ARTIFACTS || `.runtime-tools/s6-browser-${Date.now()}`);
await mkdir(root, {recursive: true});
const only = new Set((process.env.FIXLOOP_E2E_CASES || "").split(",").filter(Boolean));
const names = ["chat_protocol", "project_lifecycle", "hardening_ui"];
for (const name of only) assert.ok(names.includes(name), `Unknown browser case: ${name}`);
const fixtureArgs = ["-X", "utf8", "tests/workbench_e2e_fixture.py", root];
if (only.has("hardening_ui")) fixtureArgs.push("--ui-hardening");
const child = spawn(process.env.FIXLOOP_TEST_PYTHON || "python", fixtureArgs, {stdio: ["ignore", "pipe", "pipe"], env: {...process.env, HF_HUB_OFFLINE: "1", TRANSFORMERS_OFFLINE: "1"}});
let serviceLog = "", browser;
child.stdout.on("data", chunk => { serviceLog += chunk; });
child.stderr.on("data", chunk => { serviceLog += chunk; });
const lines = createInterface({input: child.stdout});
const results = [];
const watchdog = setTimeout(async () => {
  results.push({name: "watchdog", status: "failed", error: "S6 browser timed out"});
  child.kill();
  await Promise.allSettled([
    writeFile(resolve(root, "service.log"), serviceLog),
    writeFile(resolve(root, "browser-results.json"), JSON.stringify({results}, null, 2)),
  ]);
  console.error("S6 browser timed out");
  await browser?.close();
  process.exit(1);
}, 180_000);
const tool = (name, args) => `<tool>${JSON.stringify({name, args})}</tool>`;
const repair = (old, value) => [
  {text: '{"conclusion":"Update the requested initial value and verify it."}'},
  {text: tool("read_file", {path: "v.py"})},
  {text: tool("patch_file", {path: "v.py", old_text: `value = ${old}`, new_text: `value = ${value}`})},
  {text: "<final>Updated the initial value.</final>"},
];
async function contains(page, selector, text) {
  await page.waitForFunction(({selector, text}) => document.querySelector(selector)?.textContent.includes(text), {selector, text});
}
async function settled(page) {
  await page.waitForFunction(() => document.querySelector("#stop").hidden && !document.querySelector("#prompt").disabled);
}
async function newChat(page, title, project = "") {
  await page.locator("#new-chat").click();
  await page.locator("#field-title").fill(title);
  await page.locator("#field-project_path").fill(project);
  await page.locator("#dialog-submit").click();
  await page.locator("#form-dialog").waitFor({state: "hidden"});
  await contains(page, "#title", title);
}
async function send(page, text) {
  await page.locator("#prompt").fill(text);
  const pending = page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname.endsWith("/turns"));
  await page.locator("#send").click();
  const response = await pending;
  assert.equal(response.status(), 202);
  const turn = await response.json();
  await page.locator(`[data-node-id="${turn.node.id}"]`).waitFor();
  await settled(page);
  return turn;
}
async function tree(page, startup) {
  const cid = await page.evaluate(() => localStorage.getItem("fixloop.conversation"));
  return (await page.request.get(`${startup.url}/api/conversations/${cid}/tree`)).json();
}
async function edit(page, request, replacement) {
  await page.locator(".user-content").filter({hasText: new RegExp(`^${request}$`)}).locator("..").getByRole("button", {name: "编辑并重新执行", exact: true}).click();
  await page.locator("#field-text").fill(replacement);
  await page.locator("#dialog-submit").click();
  await page.locator("#form-dialog").waitFor({state: "hidden"});
}
try {
  const startup = await new Promise((resolveStart, reject) => {
    lines.once("line", line => { try { resolveStart(JSON.parse(line)); } catch (e) { reject(e); } });
    child.once("exit", code => reject(new Error(`Service exited ${code}: ${serviceLog}`)));
  });
  async function script(steps) {
    const r = await fetch(`${startup.provider}/__test__/script`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({steps})});
    assert.equal(r.status, 200);
  }
  async function records() { return (await fetch(`${startup.provider}/__test__/records`)).json(); }
  async function applyCandidate(page) {
    await page.getByRole("button", {name: "查看差异并应用", exact: true}).click();
    await page.locator("#restore-dialog").waitFor({state: "visible"});
    assert.match(await page.locator(".candidate-diff").innerText(), /value/);
    await page.getByRole("button", {name: "应用到项目", exact: true}).click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await settled(page);
  }
  browser = await chromium.launch({headless: true, ...(process.env.FIXLOOP_BROWSER_CHANNEL ? {channel: process.env.FIXLOOP_BROWSER_CHANNEL} : {})});
  async function run(name, test) {
    if (only.size && !only.has(name)) return;
    const context = await browser.newContext({viewport: {width: 1440, height: 960}});
    const page = await context.newPage(), errors = [], start = Date.now();
    page.setDefaultTimeout(45_000);
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", message => { if (message.type() === "error" && message.text().includes("Content Security Policy")) errors.push(message.text()); });
    try {
      await page.goto(startup.url);
      await page.locator("#token").fill(startup.token);
      await page.locator("#login-form button[type=submit]").click();
      await page.locator("#app").waitFor({state: "visible"});
      await test(page, startup);
      assert.deepEqual(errors, []);
      const telemetry = await records();
      assert.equal(telemetry.remaining, 0);
      assert.ok(telemetry.requests.every(r => !r.unexpected));
      await page.screenshot({path: resolve(root, `${name}.png`), fullPage: true});
      results.push({name, status: "passed", elapsed_ms: Date.now() - start});
      console.log(`PASS ${name}`);
    } catch (error) {
      await page.screenshot({path: resolve(root, `${name}-failure.png`), fullPage: true});
      results.push({name, status: "failed", elapsed_ms: Date.now() - start, error: String(error), toast: await page.locator("#toast").textContent(), page_errors: errors});
      throw error;
    } finally { await context.close(); }
  }
  await run("chat_protocol", async page => {
    await newChat(page, "真实协议普通聊天");
    await script([{chunks: [{text: "春日"}, {text: "散步记"}]}]);
    await send(page, "写一个短标题");
    await contains(page, "#messages", "春日散步记");
    await script([{text: "未来轮回答"}]);
    await send(page, "FUTURE_ONLY");
    await script([{text: "重新写的标题"}]);
    await edit(page, "写一个短标题", "换一个标题");
    await contains(page, "#branch-count", "2 个分支");
    await settled(page);
    assert.equal(await page.locator(".user-content").count(), 1);
    const before = (await records()).requests;
    assert.ok(before.every(r => r.path === "/api/chat"));
    assert.ok(!JSON.stringify(before.at(-1).body).includes("FUTURE_ONLY"));
    assert.equal(before.at(-1).body.tools, undefined);
    await script([{chunks: [{text: "第一段"}, {text: "第二段", delay_s: 1.5}, {text: "第三段", delay_s: 1.5}]}]);
    await page.locator("#prompt").fill("刷新后继续观察");
    await page.locator("#send").click();
    await contains(page, "#messages", "第一段");
    await page.reload();
    await page.locator("#app").waitFor({state: "visible"});
    await settled(page);
    assert.equal(await page.locator(".answer").last().innerText(), "第一段第二段第三段");
    assert.equal(await page.evaluate(() => document.cookie), "");
    await script([{chunks: [{text: "等待停止"}, {text: "不应追加", delay_s: 2}]}]);
    await page.locator("#prompt").fill("停止模型请求");
    await page.locator("#send").click();
    await contains(page, "#messages", "等待停止");
    await page.locator("#stop").click();
    await settled(page);
    assert.equal(await page.locator('.status[data-status="cancelled"]').count(), 1);
    assert.ok(!(await page.locator("#messages").innerText()).includes("不应追加"));
  });
  await run("hardening_ui", async (page, data) => {
    assert.ok(data.ui_cases, "Explicit UI fixture required; no actual repair is claimed");
    async function choose(name) {
      await page.locator("#conversations button").filter({hasText: `UI ${name}`}).click();
      await contains(page, "#title", `UI ${name}`);
    }
    await choose("candidate");
    await settled(page);
    const project = data.ui_cases.candidate.project;
    assert.equal(await readFile(resolve(project, "a.txt"), "utf8"), "old\n");
    await page.getByRole("button", {name: "查看差异并应用", exact: true}).click();
    await page.locator("#restore-dialog").waitFor({state: "visible"});
    assert.match(await page.locator(".candidate-diff").innerText(), /\+new/);
    await page.screenshot({path: resolve(root, "hardening-candidate-preview.png"), fullPage: true});
    await page.locator('#restore-dialog button[data-close="restore-dialog"]').filter({hasText: "取消"}).click();
    assert.equal(await readFile(resolve(project, "a.txt"), "utf8"), "old\n");
    await page.getByRole("button", {name: "查看差异并应用", exact: true}).click();
    await page.getByRole("button", {name: "应用到项目", exact: true}).click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await settled(page);
    assert.equal(await readFile(resolve(project, "a.txt"), "utf8"), "new\n");
    assert.equal(await readFile(resolve(project, "b.txt"), "utf8"), "new\n");
    await page.locator("#tree button").filter({hasText: "Review fixture candidate"}).click();
    await contains(page, "#details", "已应用到项目");

    await choose("recovery");
    await contains(page, "#recovery", "部分新文件和部分旧文件");
    await contains(page, "#recovery", "上次已选择回到恢复前");
    assert.equal(await page.getByRole("button", {name: "完成恢复", exact: true}).isDisabled(), true);
    await page.screenshot({path: resolve(root, "hardening-partial-recovery.png"), fullPage: true});
    assert.equal(await page.locator("#prompt").isDisabled(), true);
    await page.getByRole("button", {name: "回到恢复前", exact: true}).click();
    await page.getByRole("button", {name: "确认处理", exact: true}).click();
    await page.locator("#form-dialog").waitFor({state: "hidden"});
    await settled(page);
    for (const name of ["a.txt", "b.txt"]) assert.equal(await readFile(resolve(data.ui_cases.recovery.project, name), "utf8"), "old\n");
    await page.getByRole("button", {name: "放弃候选", exact: true}).click();
    await page.getByRole("button", {name: "确认放弃", exact: true}).click();
    await page.locator("#form-dialog").waitFor({state: "hidden"});
    await contains(page, "#details", "已放弃候选");

    await choose("observer");
    await contains(page, "#recovery", "尚未确认上次执行");
    assert.equal(await page.locator("#prompt").isDisabled(), true);
    const response = page.waitForResponse(r => r.request().method() === "POST" && r.url().endsWith("/reconcile"));
    await page.getByRole("button", {name: "重新检查并停止遗留执行", exact: true}).click();
    const result = await response;
    assert.equal(result.status(), 409);
    assert.ok(new URL(result.url()).pathname.includes(data.ui_cases.blocked.id));
    await contains(page, "#recovery", "尚未确认上次执行");
    assert.equal(await page.locator("#prompt").isDisabled(), true);
  });
  await run("project_lifecycle", async (page, data) => {
    const repo = resolve(root, "project");
    await mkdir(repo);
    await writeFile(resolve(repo, "v.py"), "value = 1\n");
    await writeFile(resolve(repo, "test_value.py"), "from v import value\ndef test_value():\n    assert value in (2, 3, 4)\n");
    await newChat(page, "真实修复与文件分支", repo);
    await script([{text: tool("read_file", {path: "v.py"})}, {text: "<final>Current initial value is 1.</final>"}]);
    await send(page, "Read the initial value.");
    assert.match((await records()).requests.at(-1).body.prompt, /1: value = 1/);
    await page.locator("#mode").selectOption("repair");
    const firstText = "Change initial value from 1 to 2";
    await script(repair(1, 2));
    await send(page, firstText);
    let state = await tree(page, data);
    assert.equal(state.runs.at(-1).status, "succeeded");
    assert.equal(state.runs.at(-1).artifacts.verified, true);
    assert.ok(state.runs.at(-1).artifacts.verification.passed >= 1);
    assert.equal((await readFile(resolve(repo, "v.py"), "utf8")).trim(), "value = 1");
    await applyCandidate(page);
    await script(repair(2, 3));
    await send(page, "FUTURE_ONLY change value from 2 to 3");
    state = await tree(page, data);
    assert.equal(state.runs.at(-1).status, "succeeded");
    await applyCandidate(page);
    const original = state.conversation.active_branch_id;
    await writeFile(resolve(repo, "v.py"), "value = 99\n");
    await writeFile(resolve(repo, "manual.txt"), "editor note");
    await edit(page, firstText, "Change initial value from 1 to 4");
    await page.locator("#restore-dialog").waitFor({state: "visible"});
    assert.equal(await page.locator("#restore-manual").isVisible(), true);
    await page.locator('#restore-dialog button[data-close="restore-dialog"]').filter({hasText: "取消"}).click();
    assert.match(await readFile(resolve(repo, "v.py"), "utf8"), /99/);
    assert.equal((await tree(page, data)).branches.length, 1);
    await edit(page, firstText, "Change initial value from 1 to 4");
    const count = (await records()).requests.length;
    await script(repair(1, 4));
    await page.locator("#restore-confirm").click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await contains(page, "#branch-count", "2 个分支");
    await settled(page);
    state = await tree(page, data);
    assert.equal(state.runs.at(-1).status, "succeeded");
    assert.equal((await readFile(resolve(repo, "v.py"), "utf8")).trim(), "value = 1");
    await applyCandidate(page);
    assert.equal((await readFile(resolve(repo, "v.py"), "utf8")).trim(), "value = 4");
    assert.ok((await records()).requests.slice(count).every(r => !r.body.prompt.includes("FUTURE_ONLY")));
    await page.locator("#branches").selectOption(original);
    await page.locator("#restore-confirm").click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await contains(page, "#messages", "FUTURE_ONLY");
    assert.equal((await readFile(resolve(repo, "v.py"), "utf8")).trim(), "value = 99");
    assert.equal(await readFile(resolve(repo, "manual.txt"), "utf8"), "editor note");
    await contains(page, "#manual-state", "一致");
  });
} finally {
  clearTimeout(watchdog);
  await browser?.close();
  lines.close();
  child.kill();
  await writeFile(resolve(root, "service.log"), serviceLog);
  await writeFile(resolve(root, "browser-results.json"), JSON.stringify({results, completed_at: new Date().toISOString()}, null, 2));
  console.log(`S6 browser artifacts: ${root}`);
}
