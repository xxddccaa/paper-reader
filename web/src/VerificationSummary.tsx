import { Tag } from "antd";
import type { WorkflowMode } from "./types";
import { AgentCoverage } from "./AgentTaskProgress";
const recordOf = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
function ReviewState({ report }: { report: unknown }) {
  const passed = recordOf(report).passed;
  return (
    <Tag
      color={
        passed === true ? "success" : passed === false ? "error" : "default"
      }
    >
      {passed === true ? "已通过" : passed === false ? "未通过" : "未提供记录"}
    </Tag>
  );
}
export default function VerificationSummary({
  verification,
  mode,
  hasTypedEvidence = false,
}: {
  verification: Record<string, unknown>;
  mode: WorkflowMode;
  hasTypedEvidence?: boolean;
}) {
  const workflow = recordOf(verification.workflow);
  const legacyReport = recordOf(verification.agent_report);
  const agent = verification.execution_engine === "pi-agent-v1";
  const reviews = recordOf(verification.reviews);
  const agentEvidence = recordOf(verification.evidence_check).passed;
  const evidenceStatus =
    verification.server_evidence_check ?? verification.server_quote_check ??
    (agent && typeof agentEvidence === "boolean" ? agentEvidence ? "passed" : "failed" : undefined);
  const usesEvidenceCheck =
    agent || hasTypedEvidence || verification.server_evidence_check != null;
  const nativeStatus = workflow.native_status;
  const nativeNotRequested =
    mode === "guided" || nativeStatus === "not_requested";
  const nativeComplete = !nativeNotRequested && nativeStatus === "completed";
  const crossPaper = verification.cross_paper_status;
  return (
    <section className="panel verification-summary" aria-label="核查与流程状态">
      <h3>核查与流程</h3>
      <dl>
        <div>
          <dt>{usesEvidenceCheck ? "证据出处核验" : "引文匹配"}</dt>
          <dd>
            <Tag
              color={
                evidenceStatus === "passed"
                  ? "success"
                  : ["partial", "failed"].includes(String(evidenceStatus))
                    ? "warning"
                    : "default"
              }
            >
              {evidenceStatus === "passed"
                ? "已通过"
                : ["partial", "failed"].includes(String(evidenceStatus))
                  ? "存在待核查项"
                  : "未提供记录"}
            </Tag>
          </dd>
        </div>
        <div>
          <dt>事实与条件复核</dt>
          <dd>
            <ReviewState report={verification.fact_review ?? (agent ? reviews.fact : undefined)} />
          </dd>
        </div>
        <div>
          <dt>解读内容复核</dt>
          <dd>
            <ReviewState
              report={
                verification.product_review ?? (agent ? reviews.product : legacyReport.product_review)
              }
            />
          </dd>
        </div>
        {agent && (
          <>
            <div><dt>图表复核</dt><dd><ReviewState report={reviews.visual} /></dd></div>
            <div><dt>资料覆盖复核</dt><dd><ReviewState report={reviews.coverage} /></dd></div>
          </>
        )}
        <div>
          <dt>原生 Skill 流程</dt>
          <dd>
            <Tag
              color={
                nativeComplete
                  ? "success"
                  : nativeStatus === "failed"
                    ? "error"
                    : "default"
              }
            >
              {nativeNotRequested
                ? "未请求"
                : nativeComplete
                  ? "已完成"
                  : nativeStatus === "failed"
                    ? "未完成"
                    : "未提供完成状态"}
            </Tag>
          </dd>
        </div>
        {crossPaper !== undefined && (
          <div>
            <dt>跨论文核查</dt>
            <dd>
              <Tag
                color={
                  crossPaper === "completed"
                    ? "success"
                    : ["partial", "not_completed", "failed"].includes(
                          String(crossPaper),
                        )
                      ? "warning"
                      : "default"
                }
              >
                {crossPaper === "completed"
                  ? "已完成"
                  : crossPaper === "not_requested"
                    ? "未请求"
                    : ["partial", "not_completed", "failed"].includes(
                          String(crossPaper),
                        )
                      ? "尚未完成"
                      : "未提供记录"}
              </Tag>
            </dd>
          </div>
        )}
      </dl>
      {agent && <AgentCoverage coverage={verification.coverage} compact />}
      <p className="muted">
        {nativeNotRequested
          ? "本次采用 Skill 阅读指引，未执行完整原生笔记流程。"
          : "原生流程完成状态单独记录，不由引文匹配或解读任务状态推定。"}
      </p>
      <p className="muted">
        引文匹配确认文字出处；表格单元格与图像观察采用各自的核验方式。事实复核检查结论、条件与推断。
      </p>
    </section>
  );
}
