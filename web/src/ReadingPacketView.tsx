import { Button, Tag } from "antd";
import type { Evidence, ReadingPacket } from "./types";
import EvidenceContent, {
  EvidenceLabels,
  publishedEvidence,
} from "./EvidenceContent";
const claimKinds = {
  contribution: "研究贡献",
  method: "核心方法",
  result: "实验结果",
  lesson: "可借鉴经验",
  limitation: "限制与边界",
  related_work: "相关工作",
};
const claimBasis = {
  observation: "论文观察",
  author_hypothesis: "作者假设",
  inference: "推断",
};
const conditions = [
  ["model", "模型"],
  ["dataset", "数据集"],
  ["split", "数据划分"],
  ["metric", "指标"],
  ["baseline", "比较基线"],
  ["budget", "预算与规模"],
] as const;
export function isReadingPacket(value: unknown): value is ReadingPacket {
  if (!value || typeof value !== "object") return false;
  const packet = value as Partial<ReadingPacket>;
  return (
    (packet.version === 1 || packet.version === 2) &&
    Array.isArray(packet.claims) &&
    packet.claims.length > 0
  );
}
export default function ReadingPacketView({
  packet,
  evidence,
  onEvidence,
}: {
  packet: ReadingPacket;
  evidence: Evidence[];
  onEvidence: (item: Evidence) => void;
}) {
  const sourceIndex = new Map(evidence.map((item) => [item.id, item]));
  return (
    <section className="reading-packet" aria-label="结构化结论与条件">
      <div className="packet-question">
        <h2>研究问题</h2>
        <p>{packet.research_question || "本次未提取研究问题。"}</p>
      </div>
      <div className="packet-claims">
        {packet.claims.map((claim) => (
          <section
            className="packet-claim"
            key={claim.id}
            aria-label={`结论 ${claim.id}`}
          >
            <div className="packet-claim-labels">
              <span className="claim-id">{claim.id}</span>
              <Tag>{claimKinds[claim.kind] ?? "结论"}</Tag>
              <Tag
                color={
                  claim.basis === "inference"
                    ? "orange"
                    : claim.basis === "author_hypothesis"
                      ? "gold"
                      : "cyan"
                }
              >
                {claimBasis[claim.basis] ?? "依据未说明"}
              </Tag>
            </div>
            <p className="claim-statement">{claim.statement}</p>
            {claim.numeric_comparison && (
              <p className="claim-comparison">
                数值对比：
                <strong>
                  {claim.numeric_comparison.value}{" "}
                  {claim.numeric_comparison.unit}
                </strong>{" "}
                {
                  { higher: "高于", lower: "低于", equal: "等于" }[
                    claim.numeric_comparison.relation
                  ]
                }
                基线{" "}
                <strong>
                  {claim.numeric_comparison.baseline_value}{" "}
                  {claim.numeric_comparison.unit}
                </strong>
              </p>
            )}
            <dl className="claim-conditions">
              {conditions.map(([field, label]) => (
                <div key={field}>
                  <dt>{label}</dt>
                  <dd className={claim.conditions?.[field] ? "" : "muted"}>
                    {claim.conditions?.[field] || "未说明"}
                  </dd>
                </div>
              ))}
            </dl>
            <details className="claim-evidence">
              <summary>支持证据 · {claim.evidence.length} 条</summary>
              {claim.evidence.map((item) => {
                const source = sourceIndex.get(item.id);
                const resolved = publishedEvidence(item, source);
                return (
                  <div className="claim-source" key={item.id}>
                    <div className="claim-source-title">
                      {source ? (
                        <Button
                          type="link"
                          size="small"
                          aria-label={`查看证据 ${item.id}`}
                          onClick={() => onEvidence(source)}
                        >
                          {item.id} · {source.source_title}
                        </Button>
                      ) : (
                        <span>证据 {item.id}</span>
                      )}
                      <EvidenceLabels item={resolved} />
                    </div>
                    <EvidenceContent item={resolved} />
                  </div>
                );
              })}
            </details>
          </section>
        ))}
      </div>
      {!!packet.limitations?.length && (
        <div className="packet-boundaries">
          <h2>整体限制</h2>
          <ul>
            {packet.limitations.map((limitation, index) => (
              <li key={index}>{limitation}</li>
            ))}
          </ul>
        </div>
      )}
      {!!packet.open_questions?.length && (
        <div className="packet-boundaries">
          <h2>待核查的问题</h2>
          <p className="muted">
            这些问题保留在证据记录中，核查结果以对应报告为准。
          </p>
          <ul>
            {packet.open_questions.map((question, index) => (
              <li key={index}>
                <strong>{question.question}</strong>
                <p>{question.reason}</p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
