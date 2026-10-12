// Browser acceptance against the actual Python service. No remote model or site is used.
// npm install --no-save playwright, or set FIXLOOP_PLAYWRIGHT_MODULE to an existing module.
import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {mkdir, readFile, writeFile} from "node:fs/promises";
import {createRequire} from "node:module";
import {resolve} from "node:path";
import {createInterface} from "node:readline";
const require = createRequire(import.meta.url);
const {chromium} = require(process.env.FIXLOOP_PLAYWRIGHT_MODULE || "playwright");
const root = resolve(`.runtime-tools/s5-browser-${Date.now()}`);
await mkdir(root, {recursive: true});
const child = spawn(process.env.FIXLOOP_TEST_PYTHON || "python", ["-X", "utf8", "tests/workbench_ui_fixture.py", root], {stdio: ["ignore", "pipe", "pipe"]});
let stderr = "";
child.stderr.on("data", chunk => { stderr += chunk; });
const lines = createInterface({input: child.stdout});
let browser;
const only = new Set((process.env.FIXLOOP_UI_CASES || "").split(",").filter(Boolean));
const watchdog = setTimeout(() => { console.error("Browser acceptance timed out", stderr); child.kill(); process.exit(1); }, 150_000);
async function waitText(page, selector, text) {
  await page.waitForFunction(({selector, text}) => document.querySelector(selector)?.textContent.includes(text), {selector, text});
}
async function complete(page, text) {
  await page.locator("#prompt").fill(text);
  const response = page.waitForResponse(item => item.request().method() === "POST" && new URL(item.url()).pathname.endsWith("/turns"));
  await page.locator("#send").click();
  const turn = await (await response).json();
  await page.locator(`[data-node-id="${turn.node.id}"]`).waitFor({state: "visible"});
  await page.waitForFunction(() => document.querySelector("#stop").hidden && !document.querySelector("#prompt").disabled);
}
async function newConversation(page, title, repo = "") {
  if (await page.locator("#new-chat").isHidden()) await page.locator("#toggle-sidebar").click();
  await page.locator("#new-chat").click();
  await page.locator("#field-title").fill(title);
  await page.locator("#field-project_path").fill(repo);
  await page.locator("#dialog-submit").click();
  await page.locator("#form-dialog").waitFor({state: "hidden"});
  await waitText(page, "#title", title);
}
async function tree(page, url) {
  const cid = await page.evaluate(() => localStorage.getItem("fixloop.conversation"));
  return (await page.request.get(`${url}/api/conversations/${cid}/tree`)).json();
}
async function edit(page, text, replacement) {
  const message = page.locator(".user-content").filter({hasText: new RegExp(`^${text}$`)}).locator("..");
  await message.getByRole("button", {name: "编辑并重新执行", exact: true}).click();
  await page.locator("#field-text").fill(replacement);
  await page.locator("#dialog-submit").click();
  await page.locator("#form-dialog").waitFor({state: "hidden"});
}
try {
  const startup = await new Promise((resolveStart, reject) => {
    lines.once("line", line => { try { resolveStart(JSON.parse(line)); } catch (error) { reject(error); } });
    child.once("exit", code => reject(new Error(`Fixture exited ${code}: ${stderr}`)));
  });
  browser = await chromium.launch({headless: true, ...(process.env.FIXLOOP_BROWSER_CHANNEL ? {channel: process.env.FIXLOOP_BROWSER_CHANNEL} : {})});
  async function run(name, test, mobile = false) {
    if (only.size && !only.has(name)) return;
    const context = await browser.newContext({viewport: mobile ? {width: 390, height: 844} : {width: 1440, height: 960}});
    const page = await context.newPage(), errors = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("console", message => { if (message.type() === "error" && message.text().includes("Content Security Policy")) errors.push(message.text()); });
    page.setDefaultTimeout(12_000);
    try {
      await page.goto(startup.url);
      await page.locator("#token").fill(startup.token);
      await page.locator("#login-form button[type=submit]").click();
      await page.locator("#app").waitFor({state: "visible"});
      await test(page, startup);
      assert.deepEqual(errors, [], "browser errors");
      console.log(`PASS ${name}`);
    } catch (error) {
      await page.screenshot({path: resolve(root, `${name}-failure.png`), fullPage: true});
      console.error(`FAIL ${name}`, await page.locator("#toast").textContent(), errors);
      throw error;
    } finally { await context.close(); }
  }
  await run("chat_history", async (page, {url}) => {
    await newConversation(page, "聊天与分支验收");
    await complete(page, "第一轮");
    await complete(page, "第二轮");
    const original = await tree(page, url), originalBranch = original.conversation.active_branch_id;
    await edit(page, "第一轮", "修改第一轮");
    await waitText(page, "#branch-count", "2 个分支");
    await page.waitForFunction(() => document.querySelector("#stop").hidden);
    assert.equal(await page.locator(".user-content").count(), 1);
    assert.equal(await page.locator(".user-content").innerText(), "修改第一轮");
    await page.locator(".message-actions").first().getByRole("button", {name: "重新生成", exact: true}).click();
    await waitText(page, "#branch-count", "3 个分支");
    await page.waitForFunction(() => document.querySelector("#stop").hidden);
    await page.reload();
    await page.locator("#app").waitFor({state: "visible"});
    assert.equal(await page.locator("#login").isHidden(), true);
    assert.equal(await page.evaluate(() => document.cookie), "");
    await page.locator("#branches").selectOption(originalBranch);
    await waitText(page, "#messages", "第二轮");
    await page.locator("#tree button").filter({hasText: /^1 · 第一轮$/}).click();
    await page.locator("#history-notice").waitFor({state: "visible"});
    assert.equal(await page.locator(".user-content").count(), 1);
    await complete(page, "分支尾的新消息");
    let current = await tree(page, url);
    const last = current.nodes.find(node => node.user_text === "分支尾的新消息");
    assert.equal(last.parent_id, original.branches[0].head_node_id);
    await page.locator("#tree button").filter({hasText: /^1 · 第一轮$/}).click();
    await page.locator("#history-notice").getByRole("button", {name: "从这里继续"}).click();
    await waitText(page, "#branch-count", "4 个分支");
    await complete(page, "新思路");
    assert.equal(await page.locator(".user-content").count(), 2);
    assert.equal(await page.locator(".user-content").last().innerText(), "新思路");
    await page.screenshot({path: resolve(root, "desktop-chat.png"), fullPage: true});
  });
  await run("project_restore", async (page, {repo, url}) => {
    await newConversation(page, "项目与文件验收", repo);
    assert.equal(await page.locator("#mode").inputValue(), "project_chat");
    await complete(page, "解释文件");
    await waitText(page, "#messages", "当前文件：base");
    await page.locator("#mode").selectOption("repair");
    await complete(page, "one");
    await complete(page, "two");
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "two");
    const original = await tree(page, url), originalBranch = original.conversation.active_branch_id;
    await writeFile(resolve(repo, "value.txt"), "manual", "utf8");
    await edit(page, "one", "edited");
    await page.locator("#restore-dialog").waitFor({state: "visible"});
    assert.equal(await page.locator("#restore-manual").isVisible(), true);
    assert.match(await page.locator("#restore-changes").innerText(), /value.txt/);
    await page.locator('#restore-dialog button[data-close="restore-dialog"]').filter({hasText: "取消"}).click();
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "manual");
    assert.equal((await tree(page, url)).branches.length, 1);
    await edit(page, "one", "edited");
    await page.locator("#restore-confirm").click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await waitText(page, "#branch-count", "2 个分支");
    await page.waitForFunction(() => document.querySelector("#stop").hidden);
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "edited");
    assert.equal(await page.locator(".user-content").count(), 2); // ancestor QA + edited repair
    await page.locator("#branches").selectOption(originalBranch);
    await page.locator("#restore-confirm").click();
    await page.locator("#restore-dialog").waitFor({state: "hidden"});
    await waitText(page, "#messages", "two");
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "manual");
    await waitText(page, "#manual-state", "一致");
    await edit(page, "one", "stale");
    await page.locator("#restore-dialog").waitFor({state: "visible"});
    await writeFile(resolve(repo, "value.txt"), "new manual", "utf8");
    await page.locator("#restore-confirm").click();
    await waitText(page, "#toast", "失效");
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "new manual");
    assert.equal((await tree(page, url)).branches.length, 2);
    await page.locator("#mode").selectOption("repair");
    await complete(page, "部分失败");
    assert.equal(await page.locator('.status[data-status="failed"]').last().isVisible(), true);
    assert.equal(await readFile(resolve(repo, "value.txt"), "utf8"), "部分失败");
    await waitText(page, "#run-detail", "value.txt");
    await page.screenshot({path: resolve(root, "desktop-project.png"), fullPage: true});
  });
  await run("interrupted_recovery", async (page) => {
    await page.locator("#conversations .conversation").filter({hasText: "恢复中断验收"}).click();
    await page.locator("#recovery").waitFor({state: "visible"});
    assert.equal(await page.locator("#send").isDisabled(), true);
    await page.locator("#recovery").getByRole("button", {name: "回到恢复前", exact: true}).click();
    await page.locator("#dialog-submit").click();
    await page.locator("#form-dialog").waitFor({state: "hidden"});
    await page.locator("#recovery").waitFor({state: "hidden"});
    assert.equal(await readFile(resolve(root, "interrupted", "value.txt"), "utf8"), "manual");
  });
  await run("reconnect_stop", async (page) => {
    await newConversation(page, "停止与重连验收");
    let cut = false; const cursors = [];
    page.on("request", request => { if (request.url().includes("/events?after=")) cursors.push(new URL(request.url()).searchParams.get("after")); });
    await page.route("**/events?after=0", async route => {
      if (cut) { await route.continue(); return; }
      cut = true;
      const rid = new URL(route.request().url()).pathname.split("/").at(-2);
      const data = {sequence: 3, run_id: rid, kind: "answer.delta", data: {text: "第一段"}};
      await route.fulfill({status: 200, contentType: "text/event-stream", body: `id: ${rid}:3\nevent: answer.delta\ndata: ${JSON.stringify(data)}\n\n`});
    });
    await complete(page, "重连测试");
    assert.ok(cursors.includes("3"), `missing reconnect cursor: ${cursors}`);
    assert.equal(await page.locator(".answer").last().innerText(), "第一段第二段第三段");
    await page.locator("#prompt").fill("等待停止"); await page.locator("#send").click();
    await waitText(page, "#messages", "正在等待停止");
    await page.locator("#stop").click();
    await page.waitForFunction(() => document.querySelector("#stop").hidden);
    assert.equal(await page.locator('.status[data-status="cancelled"]').isVisible(), true);
  });
  await run("terminal_refresh_gate", async page => {
    await newConversation(page, "终态同步验收");
    let reads = 0, release;
    const held = new Promise(resolveHold => { release = resolveHold; });
    await page.route("**/tree", async route => {
      reads++;
      if (reads === 2) await held;
      await route.continue();
    });
    await page.locator("#prompt").fill("重连测试");
    await page.locator("#send").click();
    await page.waitForFunction(() => !document.querySelector("#stop").hidden);
    await page.waitForFunction(() => document.querySelector("#stop").hidden);
    assert.equal(await page.locator("#prompt").isDisabled(), true);
    assert.equal(await page.locator("#branches").isDisabled(), true);
    release();
    await page.waitForFunction(() => !document.querySelector("#prompt").disabled);
    await complete(page, "同步后继续");
    await waitText(page, "#messages", "回答：同步后继续");
  });
  await run("session_and_copy", async page => {
    await newConversation(page, "复制与会话验收");
    await complete(page, "复制代码");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.getByRole("button", {name: "复制代码", exact: true}).click();
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "value = 1");
    await page.locator("#rename").click();
    await page.locator("#field-title").fill("已重命名");
    await page.locator("#dialog-submit").click();
    await page.locator("#form-dialog").waitFor({state: "hidden"});
    await waitText(page, "#title", "已重命名");
    let release;
    const gate = new Promise(resolveGate => { release = resolveGate; });
    await page.route("**/events?after=*", async route => {
      await gate;
      await route.fulfill({status: 401, contentType: "application/json", body: '{"error":"unauthorized"}'});
    });
    await page.locator("#prompt").fill("等待停止"); await page.locator("#send").click();
    await page.waitForFunction(() => !document.querySelector("#stop").hidden);
    await page.locator("#new-chat").click();
    await page.locator("#form-dialog").waitFor({state: "visible"});
    release();
    await page.locator("#login").waitFor({state: "visible"});
    assert.equal(await page.locator("#form-dialog").isHidden(), true);
  });
  await run("mobile_safe_render", async page => {
    await newConversation(page, "移动端验收");
    await complete(page, '# 标题\n**加粗** [文档](https://example.com) [危险](javascript:alert(1))\n<img src=x onerror="window.pwned=1">');
    assert.equal(await page.locator(".answer img").count(), 0);
    assert.equal(await page.locator('.answer a[href^="javascript:"]').count(), 0);
    assert.equal(await page.locator(".answer a").count(), 1);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: resolve(root, "mobile-chat.png"), fullPage: true});
    await page.locator("#toggle-details").click();
    assert.equal(await page.locator("#details").isVisible(), true);
    await page.locator("#toggle-details").click();
    await page.locator("#toggle-sidebar").click();
    await page.locator("#logout").click();
    await page.locator("#login").waitFor({state: "visible"});
  }, true);
  console.log(`Browser acceptance artifacts: ${root}`);
} finally {
  clearTimeout(watchdog);
  await browser?.close(); lines.close(); child.kill();
}
