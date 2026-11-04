import type { ReactNode } from "react";
import { safeSourceUrl } from "./assistant-safe-url";

/**
 * Minimal, safe markdown renderer for assistant replies.
 * Everything becomes React text nodes; raw HTML is never parsed or injected.
 * Supported: fenced code blocks, headings, bullet/numbered lists, paragraphs,
 * inline code, **bold**, and [links](https://...) restricted to safe https URLs.
 */

type Block =
  | { kind: "code"; lang: string; text: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "para"; text: string };

export function parseMarkdownBlocks(src: string): Block[] {
  const lines = String(src ?? "").replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  const flush = () => { if (para.length) { blocks.push({ kind: "para", text: para.join("\n") }); para = []; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = /^\s*(```+|~~~+)\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      flush();
      const marker = fence[1];
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) { body.push(lines[i]); i++; }
      blocks.push({ kind: "code", lang: fence[2] || "", text: body.join("\n") });
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { flush(); blocks.push({ kind: "heading", level: h[1].length, text: h[2] }); continue; }
    const li = /^\s*([-*]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      flush();
      const ordered = /\d/.test(li[1]);
      const last = blocks[blocks.length - 1];
      if (last && last.kind === "list" && last.ordered === ordered) last.items.push(li[2]);
      else blocks.push({ kind: "list", ordered, items: [li[2]] });
      continue;
    }
    if (!line.trim()) { flush(); continue; }
    para.push(line);
  }
  flush();
  return blocks;
}

export function renderInline(text: string, keyPrefix = "i"): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[([^\]\n]+)\]\(([^)\s]+)\))/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const k = `${keyPrefix}-${n++}`;
    if (m[1]) out.push(<code key={k} className="rounded bg-slate-100 px-1 py-px font-mono text-[0.85em] text-slate-800">{m[1].slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={k} className="font-semibold text-slate-900">{m[2].slice(2, -2)}</strong>);
    else {
      const href = safeSourceUrl(m[5]);
      out.push(href
        ? <a key={k} href={href} target="_blank" rel="noopener noreferrer nofollow" className="text-teal-800 underline underline-offset-2 hover:text-teal-950">{m[4]}</a>
        : <span key={k}>{m[4]} (link unavailable)</span>);
    }
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function CodeBlock({ code, lang, testId }: { code: string; lang?: string; testId?: string }) {
  return (
    <figure className="overflow-hidden rounded-lg border border-slate-800/80 bg-slate-900" data-testid={testId}>
      {lang ? <figcaption className="border-b border-slate-700/80 px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-slate-400">{lang}</figcaption> : null}
      <pre className="overflow-x-auto p-3 font-mono text-[12px] leading-relaxed text-slate-100"><code>{code}</code></pre>
    </figure>
  );
}

export function AssistantMarkdown({ content, testId }: { content: string; testId?: string }) {
  const blocks = parseMarkdownBlocks(content);
  return (
    <div className="space-y-2.5 text-[13.5px] leading-relaxed text-slate-800 break-words" data-testid={testId}>
      {blocks.map((b, i) => {
        if (b.kind === "code") return <CodeBlock key={i} code={b.text} lang={b.lang} testId="assistant-code-block" />;
        if (b.kind === "heading") {
          const cls = b.level <= 2 ? "text-[15px] font-semibold text-slate-900" : "text-[13.5px] font-semibold text-slate-900";
          return <p key={i} role="heading" aria-level={Math.min(6, b.level + 2)} className={cls}>{renderInline(b.text, `h${i}`)}</p>;
        }
        if (b.kind === "list") {
          const Tag = b.ordered ? "ol" : "ul";
          return (
            <Tag key={i} className={`space-y-1 pl-5 ${b.ordered ? "list-decimal" : "list-disc"} marker:text-teal-700`}>
              {b.items.map((it, j) => <li key={j}>{renderInline(it, `l${i}-${j}`)}</li>)}
            </Tag>
          );
        }
        return <p key={i} className="whitespace-pre-wrap">{renderInline(b.text, `p${i}`)}</p>;
      })}
    </div>
  );
}
