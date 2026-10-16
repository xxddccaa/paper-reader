import type { Paper } from "./types";

export function titlePending(paper: Paper) {
  return !paper.metadata_json.title_status ||
    ["pending", "processing"].includes(String(paper.metadata_json.title_status));
}

export default function PaperTitle({ paper }: { paper: Paper }) {
  const translated = paper.metadata_json.title_zh;
  return (
    <div>
      <div className="paper-title">{paper.title}</div>
      {typeof translated === "string" && translated && translated !== paper.title && (
        <div className="paper-title-translation" lang="zh-CN">{translated}</div>
      )}
    </div>
  );
}
