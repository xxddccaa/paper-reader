import { Tag } from "antd";
import type { Evidence, EvidenceDetails } from "./types";

type EvidenceItem = EvidenceDetails & { quote?: string; verified?: boolean };
const types = {
  text_quote: "原文引文",
  table_cell: "表格单元格",
  image_observation: "图像观察",
};
const methods = {
  quote_match: "引文匹配",
  section_lookup: "原文段落定位",
  table_cell_match: "单元格与表头匹配",
  visual_observation: "图像观察记录核验",
};
const expectedMethod = {
  text_quote: "quote_match",
  table_cell: "table_cell_match",
  image_observation: "visual_observation",
};
export function evidenceType(item: EvidenceDetails) {
  // The type is optional for legacy quotes. A visual method must never be
  // presented as a quote, even when the type is missing in a partial record.
  return (
    item.evidence_type ??
    (item.verification_method === "visual_observation"
      ? "image_observation"
      : item.verification_method === "table_cell_match"
        ? "table_cell"
        : "text_quote")
  );
}
export function isVerifiedEvidence(item: EvidenceItem) {
  const type = evidenceType(item);
  if (isSectionSelection(item))
    return (
      item.verified === true && item.verification_method === "section_lookup"
    );
  return (
    Object.hasOwn(expectedMethod, type) &&
    item.verified === true &&
    (item.verification_method === expectedMethod[type] ||
      (type === "text_quote" && !item.verification_method))
  );
}
function isSectionSelection(item: EvidenceDetails) {
  return (
    evidenceType(item) === "text_quote" &&
    (item.verification_method === "section_lookup" ||
      item.selection === "section")
  );
}
export function evidencePreview(item: EvidenceItem) {
  return evidenceType(item) === "image_observation"
    ? item.observation || "未提供图像观察描述；请查看图表核查记录。"
    : item.quote || "未提供原文内容";
}
export function EvidenceLabels({ item }: { item: EvidenceItem }) {
  const type = evidenceType(item);
  const verified = isVerifiedEvidence(item);
  const sectionSelection = isSectionSelection(item);
  return (
    <>
      <Tag>
        {sectionSelection ? "原文段落" : (types[type] ?? "证据类型未识别")}
      </Tag>
      <Tag color={verified ? "success" : "warning"}>
        {verified
          ? type === "image_observation"
            ? "观察记录已核验"
            : type === "table_cell"
              ? "单元格已匹配"
              : sectionSelection
                ? "段落已定位"
                : "原文匹配"
          : "未确认核验"}
      </Tag>
      <span className="evidence-method">
        核验方式：
        {item.verification_method
          ? (methods[item.verification_method] ?? item.verification_method)
          : item.evidence_type || item.selection
            ? "未提供"
            : "未提供（旧记录）"}
      </span>
    </>
  );
}
export default function EvidenceContent({ item }: { item: EvidenceItem }) {
  const type = evidenceType(item);
  return (
    <div className="evidence-content">
      {type === "image_observation" ? (
        <div className="source-observation">
          <p>{evidencePreview(item)}</p>
          <p className="muted">图像观察来自图表核查记录，不是原文引文匹配。</p>
        </div>
      ) : (
        <blockquote className="source-quote">
          {evidencePreview(item)}
        </blockquote>
      )}
      {type === "table_cell" && (
        <dl className="evidence-details">
          <div>
            <dt>表格</dt>
            <dd>{item.table_id || "未提供"}</dd>
          </div>
          <div>
            <dt>行 / 列（原始索引）</dt>
            <dd>
              {item.row ?? "未提供"} / {item.column ?? "未提供"}
            </dd>
          </div>
          <div>
            <dt>表头链</dt>
            <dd>{item.headers?.join(" → ") || "未提供"}</dd>
          </div>
        </dl>
      )}
      {type === "image_observation" && (
        <dl className="evidence-details">
          <div>
            <dt>图表</dt>
            <dd>{item.figure_id || "未提供"}</dd>
          </div>
          <div>
            <dt>观察记录</dt>
            <dd>{item.observation_id || "未提供"}</dd>
          </div>
          <div>
            <dt>图像 SHA-256</dt>
            <dd>{item.image_sha256 || "未提供"}</dd>
          </div>
          <div>
            <dt>适用条件</dt>
            <dd>{item.conditions || "未提供；请结合图表核查记录判断。"}</dd>
          </div>
        </dl>
      )}
      {!!item.uncertainties?.length && (
        <div className="coverage-gaps" role="note" aria-label="证据不确定项">
          <strong>仍不确定</strong>
          <ul>
            {item.uncertainties.map((text, index) => (
              <li key={index}>{text}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// Only the published index supplies verification state; packet flags are not
// authoritative. Retain packet identity metadata if the index omits it.
export function publishedEvidence(
  item: EvidenceItem,
  published?: Evidence,
): EvidenceItem {
  return {
    ...item,
    ...published,
    verified: published?.verified === true,
    verification_method: published?.verification_method,
  };
}
