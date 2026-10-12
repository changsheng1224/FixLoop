// Same-origin API and resumable SSE. Credentials never enter page storage.
export class ApiError extends Error {
  constructor(status, data) {
    const messages = {
      400: "请求内容无效，请检查输入。", 401: "本地会话已失效，请重新登录。",
      403: "请求来源被拒绝，请从本地工作台地址打开。", 404: "目标已不存在，请刷新会话。",
      409: "状态已变化或任务尚未结束，请刷新后重试。", 422: "此操作需要项目会话或恢复预览。",
      429: "连接数已满，请稍后重试。", 500: "本地服务操作失败，请检查终端及项目恢复状态。",
    };
    super(messages[status] || `本地服务返回 ${status}`);
    this.status = status;
    this.detail = data?.message || "";
  }
}

export async function api(path, {method = "GET", body, headers = {}, signal} = {}) {
  const response = await fetch(`/api${path}`, {
    method, credentials: "same-origin", cache: "no-store", signal,
    headers: {...(body === undefined ? {} : {"Content-Type": "application/json"}), ...headers},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, data);
  return data;
}

// Exported separately so split UTF-8 and reconnect frames can be tested without a browser.
export class SSEParser {
  constructor() { this.buffer = ""; }
  push(text) {
    this.buffer += text;
    if (this.buffer.length > 8_000_000) throw new Error("事件内容超过显示限制。");
    const frames = [];
    let match;
    while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
      const raw = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      let event = "message", id = "";
      const data = [];
      for (const line of raw.split(/\r?\n/)) {
        if (line.startsWith(":")) continue;
        const index = line.indexOf(":");
        const name = index < 0 ? line : line.slice(0, index);
        const value = index < 0 ? "" : line.slice(index + 1).replace(/^ /, "");
        if (name === "event") event = value;
        else if (name === "id") id = value;
        else if (name === "data") data.push(value);
      }
      if (data.length) frames.push({event, id, data: JSON.parse(data.join("\n"))});
    }
    return frames;
  }
}

export async function streamRun(path, cursor, signal, onEvent) {
  const response = await fetch(`/api${path}/events?after=${cursor}`, {
    credentials: "same-origin", cache: "no-store", signal,
  });
  if (!response.ok) throw new ApiError(response.status, await response.json());
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SSEParser();
  try {
    for (;;) {
      const {done, value} = await reader.read();
      const frames = parser.push(done ? decoder.decode() : decoder.decode(value, {stream: true}));
      for (const frame of frames) onEvent(frame);
      if (done) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

export function pathTo(nodes, id) {
  const byId = new Map(nodes.map(node => [node.id, node]));
  const result = [], seen = new Set();
  while (id) {
    if (seen.has(id) || !byId.has(id)) throw new Error("会话树不完整，请刷新后重试。");
    seen.add(id);
    const node = byId.get(id);
    result.push(node);
    id = node.parent_id;
  }
  return result.reverse();
}
