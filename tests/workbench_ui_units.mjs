// Run with Node.js 22+. No npm dependencies or remote services required.
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
const source = await readFile(new URL("../src/workbench/web/api.js", import.meta.url), "utf8");
const {SSEParser, pathTo} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const frames = [
  'id: run-a:1\nevent: answer.delta\ndata: {"text":"你好"}\n\n',
  ': heartbeat\r\n\r\nevent: run.snapshot\r\ndata: {"status":"succeeded"}\r\n\r\n',
].join("");
for (let width = 1; width <= frames.length; width++) {
  const parser = new SSEParser(), events = [];
  const bytes = new TextEncoder().encode(frames), decoder = new TextDecoder();
  for (let i = 0; i < bytes.length; i += width) events.push(...parser.push(decoder.decode(bytes.slice(i, i + width), {stream: true})));
  events.push(...parser.push(decoder.decode()));
  assert.equal(events.length, 2);
  assert.equal(events[0].data.text, "你好");
  assert.equal(events[1].data.status, "succeeded");
}
const nodes = [
  {id: "1", parent_id: null}, {id: "2", parent_id: "1"},
  {id: "3", parent_id: "2"}, {id: "2b", parent_id: "1"},
];
assert.deepEqual(pathTo(nodes, "2b").map(n => n.id), ["1", "2b"]);
assert.deepEqual(pathTo(nodes, null), []);
assert.throws(() => pathTo(nodes, "missing"));
assert.throws(() => pathTo([{id: "loop", parent_id: "loop"}], "loop"));
console.log("SSE UTF-8/chunk boundaries, heartbeat, CRLF and branch ancestry passed.");
