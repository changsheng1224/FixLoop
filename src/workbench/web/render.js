// Deliberately limited Markdown, rendered with DOM text nodes. Raw HTML stays text.
export function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function button(text, action, className = "quiet") {
  const node = el("button", className, text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function inline(parent, text, depth = 0) {
  if (depth > 3) { parent.append(document.createTextNode(text)); return; }
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    parent.append(document.createTextNode(text.slice(start, match.index)));
    const token = match[0];
    if (token.startsWith("`")) parent.append(el("code", "", token.slice(1, -1)));
    else if (token.startsWith("**")) {
      const strong = el("strong"); inline(strong, token.slice(2, -2), depth + 1); parent.append(strong);
    } else {
      const link = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      let url;
      try { url = new URL(link[2]); } catch { /* Invalid links are plain text. */ }
      if (url && ["http:", "https:"].includes(url.protocol)) {
        const anchor = el("a", "", link[1]);
        anchor.href = url.href; anchor.target = "_blank"; anchor.rel = "noopener noreferrer";
        parent.append(anchor);
      } else parent.append(document.createTextNode(token));
    }
    start = match.index + token.length;
  }
  parent.append(document.createTextNode(text.slice(start)));
}

export function markdown(text) {
  const root = el("div", "answer");
  const lines = String(text).split(/\r?\n/);
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const language = line.slice(3).trim().slice(0, 40), code = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      if (i < lines.length) i++;
      const block = el("div", "code-block"), bar = el("div", "code-toolbar");
      const content = code.join("\n");
      const copy = button("复制代码", async () => {
        try { await navigator.clipboard.writeText(content); copy.textContent = "已复制"; }
        catch { copy.textContent = "复制失败"; }
      });
      bar.append(el("span", "", language || "代码"), copy);
      const pre = el("pre"); pre.append(el("code", "", content)); block.append(bar, pre); root.append(block);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const node = el(`h${Math.min(heading[1].length + 1, 4)}`); inline(node, heading[2]); root.append(node); i++; continue;
    }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line), list = el(ordered ? "ol" : "ul");
      const pattern = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*]\s+(.+)$/;
      while (i < lines.length) {
        const item = pattern.exec(lines[i]); if (!item) break;
        const li = el("li"); inline(li, item[1]); list.append(li); i++;
      }
      root.append(list); continue;
    }
    if (line.startsWith("> ")) {
      const quote = el("blockquote"); inline(quote, line.slice(2)); root.append(quote); i++; continue;
    }
    const paragraph = [];
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|>\s|\s*([-*]|\d+\.)\s)/.test(lines[i])) paragraph.push(lines[i++]);
    if (!paragraph.length) paragraph.push(lines[i++]);
    const p = el("p"); inline(p, paragraph.join("\n")); root.append(p);
  }
  return root;
}

export const statusNames = {
  queued: "排队中", running: "执行中", succeeded: "已完成", failed: "失败", cancelled: "已停止", interrupted: "已中断",
};
export const modeNames = {chat: "普通聊天", project_chat: "项目问答", repair: "修复任务"};
export function statusBadge(status) {
  const node = el("span", "status", statusNames[status] || status);
  node.dataset.status = status;
  return node;
}
