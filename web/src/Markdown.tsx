import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Button } from "antd";
import type { Evidence } from "./types";
export default function Markdown({
  content,
  evidence = [],
  onEvidence,
}: {
  content: string;
  evidence?: Evidence[];
  onEvidence?: (item: Evidence) => void;
}) {
  const known = new Map(evidence.map((e) => [e.id, e]));
  const linked = content.replace(
    /\[([^\]\n]+)\](?!\()/g,
    (match, id: string) => (known.has(id) ? `[${id}](evidence:${id})` : match),
  );
  return (
    <article className="markdown">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
        urlTransform={(url) =>
          url.startsWith("evidence:") ? url : defaultUrlTransform(url)
        }
        components={{
          a: ({ href, children }) => {
            if (href?.startsWith("evidence:")) {
              const item = known.get(href.slice(9));
              return item ? (
                <Button
                  className="evidence-link"
                  type="link"
                  size="small"
                  onClick={() => onEvidence?.(item)}
                  aria-label={`查看证据 ${item.id}`}
                >
                  {children}
                </Button>
              ) : (
                <span
                  className="unverified-citation"
                  title="该引用尚未关联证据"
                >
                  {children}
                </span>
              );
            }
            return (
              <a href={href} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
          table: ({ children }) => (
            <div className="markdown-table">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {linked}
      </ReactMarkdown>
    </article>
  );
}
