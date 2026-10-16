import { Tag } from "antd";
import type { StageRecord, WorkflowSummaries } from "./types";

const recordOf = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const records = (value: unknown) =>
  Array.isArray(value) ? value.map(recordOf) : [];
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
const text = (value: unknown, fallback = "未提供") =>
  typeof value === "string" && value ? value : fallback;
const count = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : "—";
function hasCompleteDetails(value: unknown, total: unknown, identity: string) {
  if (
    !Array.isArray(value) ||
    typeof total !== "number" ||
    !Number.isSafeInteger(total) ||
    total < 0 ||
    value.length !== total
  )
    return false;
  const ids = value.map((item) => recordOf(item)[identity]);
  return (
    ids.every((id) => typeof id === "string" && id.trim().length > 0) &&
    new Set(ids).size === total
  );
}
const completedStatuses = new Set([
  "succeeded",
  "completed",
  "complete",
  "processed",
  "checked",
  "verified",
  "reused",
]);
const statusLabels: Record<string, [string, string]> = {
  pending: ["待处理", "warning"],
  queued: ["待处理", "warning"],
  running: ["进行中", "processing"],
  succeeded: ["已完成", "success"],
  completed: ["已完成", "success"],
  complete: ["已完成", "success"],
  processed: ["已处理", "success"],
  checked: ["已核查", "success"],
  verified: ["已核查", "success"],
  reused: ["已复用", "cyan"],
  indexed: ["已索引", "default"],
  failed: ["未通过", "error"],
  unreadable: ["无法辨读", "error"],
  unresolved: ["待核查", "warning"],
  partial: ["部分完成", "warning"],
  skipped: ["已跳过", "default"],
  cancelled: ["已取消", "default"],
};
const kindLabels: Record<string, string> = {
  body: "正文",
  section: "正文",
  paragraph: "段落",
  appendix: "附录",
  reference: "参考文献",
  references: "参考文献",
  table: "表格",
  figure: "图表",
  formula: "公式",
  abstract: "摘要",
  caption: "图表说明",
};
function CoverageStatus({ status }: { status: unknown }) {
  const [label, color] = statusLabels[text(status, "")] ?? [
    "未记录核查状态",
    "default",
  ];
  return <Tag color={color}>{label}</Tag>;
}
function gapText(value: unknown): string {
  if (typeof value === "string") return value;
  const item = recordOf(value);
  const identity = [item.section_id, item.figure_id, item.claim_id, item.id]
    .filter(Boolean)
    .join(" · ");
  const message = [item.message, item.reason, item.detail, item.question].find(
    (entry) => typeof entry === "string",
  );
  return message
    ? [identity, message].filter(Boolean).join("：")
    : (JSON.stringify(value) ?? "未提供说明");
}
function Gaps({ items }: { items: unknown }) {
  if (!Array.isArray(items) || !items.length) return null;
  return (
    <div className="coverage-gaps" role="note" aria-label="未解决项">
      <strong>未解决项 · {items.length}</strong>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{gapText(item)}</li>
        ))}
      </ul>
    </div>
  );
}
const hasGaps = (value: unknown) => Array.isArray(value) && value.length > 0;
const sectionDone = (item: Record<string, unknown>) =>
  completedStatuses.has(text(item.status)) ||
  (["reference", "references"].includes(text(item.kind)) &&
    item.status === "indexed");

// Inventory and plan summaries remain useful if execution stops before a
// coverage checkpoint. Later host snapshots replace these preliminary lists.
export function stageSummaries(stageRecords: StageRecord[]): WorkflowSummaries {
  const result: WorkflowSummaries = {};
  for (const stage of stageRecords) {
    const summary = stage.summary;
    for (const key of [
      "source_selection",
      "section_coverage",
      "figure_checks",
    ] as const) {
      if (stage[key]) Object.assign(result, { [key]: stage[key] });
      if (summary?.[key]) Object.assign(result, { [key]: summary[key] });
    }
    if (!summary) continue;
    if (stage.name === "source_inventory") {
      if (Array.isArray(summary.section_inventory)) {
        result.section_coverage = {
          sections: summary.section_inventory.map((item) => ({
            ...item,
            status: "not_recorded",
            segment_ids: [],
          })),
        };
      }
      if (Array.isArray(summary.figures)) {
        result.figure_checks = {
          items: summary.figures.map((item) => ({
            ...item,
            figure_id: item.id,
            status: "not_recorded",
            uncertainties: [],
            claim_ids: [],
          })),
        };
      }
    } else if (stage.name === "segment_plan") {
      result.section_coverage = { ...result.section_coverage, ...summary };
    } else if (stage.name === "section_coverage") {
      result.section_coverage = summary;
    } else if (stage.name === "figure_checks") {
      result.figure_checks = summary;
    }
  }
  return result;
}

export default function WorkflowCoverage({
  summary,
  context = "本版本",
}: {
  summary: WorkflowSummaries | Record<string, unknown>;
  context?: string;
}) {
  const source = recordOf(summary.source_selection);
  const coverage = recordOf(summary.section_coverage);
  const figures = recordOf(summary.figure_checks);
  const sections = records(coverage.sections);
  const segments = records(coverage.segments);
  const items = records(figures.items);
  const sectionsComplete =
    coverage.complete === true &&
    !hasGaps(coverage.unresolved) &&
    count(coverage.processed) !== "—" &&
    coverage.processed === coverage.total &&
    hasCompleteDetails(coverage.sections, coverage.total, "id") &&
    sections.every(sectionDone) &&
    segments.every((item) => completedStatuses.has(text(item.status)));
  const figuresComplete =
    figures.complete === true &&
    !hasGaps(figures.unresolved) &&
    count(figures.verified) !== "—" &&
    figures.verified === figures.total &&
    hasCompleteDetails(figures.items, figures.total, "figure_id") &&
    items.every(
      (item) =>
        completedStatuses.has(text(item.status)) &&
        !hasGaps(item.uncertainties),
    );
  if (![source, coverage, figures].some((item) => Object.keys(item).length))
    return null;
  return (
    <section
      className="panel workflow-coverage"
      aria-label={`${context}来源与覆盖`}
    >
      <h3>{context} · 来源与覆盖</h3>
      {!!Object.keys(source).length && (
        <section aria-label="原文来源" className="coverage-block">
          <h4>原文来源</h4>
          <dl className="evidence-details">
            <div>
              <dt>选用格式</dt>
              <dd>{text(source.format).toUpperCase()}</dd>
            </div>
            <div>
              <dt>选择原因</dt>
              <dd>{text(source.reason)}</dd>
            </div>
            <div>
              <dt>请求论文版本</dt>
              <dd>{text(source.requested_id)}</dd>
            </div>
            <div>
              <dt>实际论文版本</dt>
              <dd>{text(source.resolved_id)}</dd>
            </div>
          </dl>
        </section>
      )}
      {!!Object.keys(coverage).length && (
        <section aria-label="章节覆盖" className="coverage-block">
          <div className="coverage-heading">
            <h4>章节覆盖</h4>
            <Tag color={sectionsComplete ? "success" : "warning"}>
              {sectionsComplete ? "全部完成" : "尚未完成"}
            </Tag>
          </div>
          <p>
            已处理 {count(coverage.processed)} / {count(coverage.total)} 个章节
          </p>
          {!sectionsComplete && (
            <p className="coverage-warning">
              仍有章节或分段未完成，或缺少完整核查记录。
            </p>
          )}
          <Gaps items={coverage.unresolved} />
          <details open={!sectionsComplete}>
            <summary>
              章节与分段明细 · {sections.length} 个章节 · {segments.length}{" "}
              个分段
            </summary>
            {sections.length ? (
              <ul className="coverage-items">
                {sections.map((item, index) => (
                  <li key={`${text(item.id)}-${index}`}>
                    <div className="coverage-heading">
                      <strong>{text(item.title, text(item.id))}</strong>
                      <CoverageStatus status={item.status} />
                    </div>
                    <p className="muted">
                      {text(item.id)} ·{" "}
                      {kindLabels[text(item.kind)] ??
                        text(item.kind, "类型未提供")}
                    </p>
                    <p>
                      所属分段：
                      {strings(item.segment_ids).join("、") || "未提供"}
                    </p>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">未提供章节明细。</p>
            )}
            {!!segments.length && (
              <ul className="coverage-items" aria-label="分段明细">
                {segments.map((item, index) => (
                  <li key={`${text(item.id)}-${index}`}>
                    <div className="coverage-heading">
                      <strong>分段 {text(item.id)}</strong>
                      <CoverageStatus status={item.status} />
                    </div>
                    <p>
                      章节：{strings(item.section_ids).join("、") || "未提供"}
                    </p>
                  </li>
                ))}
              </ul>
            )}
          </details>
        </section>
      )}
      {!!Object.keys(figures).length && (
        <section aria-label="图表核查" className="coverage-block">
          <div className="coverage-heading">
            <h4>图表核查</h4>
            <Tag color={figuresComplete ? "success" : "warning"}>
              {figuresComplete
                ? figures.total === 0
                  ? "无需核查"
                  : "全部完成"
                : "尚未完成"}
            </Tag>
          </div>
          <p>
            已核查 {count(figures.verified)} / {count(figures.total)} 个图表
          </p>
          {!figuresComplete && (
            <p className="coverage-warning">
              仍有图表未核查或信息无法确认，不能据此补写数值。
            </p>
          )}
          <Gaps items={figures.unresolved} />
          <details open={!figuresComplete}>
            <summary>图表明细 · {items.length} 项</summary>
            {items.length ? (
              <ul className="coverage-items">
                {items.map((item, index) => (
                  <li key={`${text(item.figure_id)}-${index}`}>
                    <div className="coverage-heading">
                      <strong>{text(item.figure_id)}</strong>
                      <CoverageStatus status={item.status} />
                    </div>
                    <p>{text(item.caption, "未提供图表说明")}</p>
                    <p className="muted">所属章节：{text(item.section_id)}</p>
                    <dl className="evidence-details">
                      <div>
                        <dt>图像观察</dt>
                        <dd>{text(item.observation, "未提供观察记录")}</dd>
                      </div>
                      <div>
                        <dt>适用条件</dt>
                        <dd>{text(item.conditions)}</dd>
                      </div>
                      <div>
                        <dt>关联结论</dt>
                        <dd>
                          {strings(item.claim_ids).join("、") || "未提供"}
                        </dd>
                      </div>
                      {!!item.image_sha256 && (
                        <div>
                          <dt>图像 SHA-256</dt>
                          <dd>{text(item.image_sha256)}</dd>
                        </div>
                      )}
                    </dl>
                    <Gaps items={item.uncertainties} />
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">未提供图表明细。</p>
            )}
          </details>
        </section>
      )}
    </section>
  );
}
