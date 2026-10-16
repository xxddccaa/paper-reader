import { Tag } from "antd";
import type { Job, StageRecord, WorkflowMode } from "./types";
import WorkflowCoverage, { stageSummaries } from "./WorkflowCoverage";
import AgentTaskProgress from "./AgentTaskProgress";

const stageLabels: Record<string, string> = {
  source: "获取与解析原文",
  source_inventory: "整理原文目录与图表",
  segment_plan: "规划分段阅读",
  section_coverage: "汇总章节覆盖",
  figure_checks: "汇总图表核查",
  chunk_extract: "分段提取结论与证据",
  figure_review: "图表观察与核查",
  merge_review: "合并结论与交叉复核",
  revision_plan: "判断修订范围",
  extract: "提取结论与证据",
  packet_validate: "校验引文与数值",
  packet_repair: "修正证据记录",
  repair: "修正证据记录",
  evidence_repair: "修正引文与数值",
  merge_repair: "修正合并后的证据",
  fact_review: "事实与条件复核",
  fact_repair: "修正事实与条件",
  fact_review_final: "再次复核事实",
  compose: "按模板写作",
  product_review: "解读内容复核",
  product_repair: "修订解读内容",
  product_review_final: "再次复核解读",
  publish: "保存解读版本",
  preparing: "准备论文原文",
  prepare_source: "获取与解析原文",
  parsing: "解析原文与图表",
  reading: "阅读贡献与方法",
  running: "阅读与整理证据",
  analyzing: "整理证据与方法",
  researching: "核查相关论文",
  searching: "检索相关研究",
  writing: "生成解读",
  verifying: "核查结论与引用",
  validating: "核查证据与结果",
  revising: "按要求修订",
  starting: "准备开始",
  publishing: "保存解读结果",
  complete: "处理完成",
  completed: "处理完成",
  succeeded: "处理完成",
  failed: "处理失败",
  cancelled: "任务已取消",
  queued: "等待后台开始",
};
export const workflowModeLabel = (mode?: WorkflowMode) =>
  mode === "guided" ? "模板解读" : "完整原生 Skill";
export const stageLabel = (stage: string): string => {
  if (stageLabels[stage]) return stageLabels[stage];
  const repairRound = /^(.*repair)_(\d+)$/.exec(stage);
  if (repairRound)
    return `${stageLabel(repairRound[1])} · 第 ${repairRound[2]} 轮`;
  const segment = /^segment_(\d+)_(.+)$/.exec(stage);
  if (segment)
    return `分段 ${segment[1]} · ${segment[2] === "review" ? "事实与条件复核" : stageLabel(segment[2])}`;
  const figure = /^figure_(\d+)_(.+)$/.exec(stage);
  if (figure) return `图表 ${figure[1]} · ${figure[2] === "review" ? "观察与核查" : stageLabel(figure[2])}`;
  return "阅读与核查论文";
};
const isEvidenceRepair = (stage: string) => stage.includes("repair") && !stage.startsWith("product_");
const statusLabels: Record<StageRecord["status"], [string, string]> = {
  pending: ["待处理", "default"],
  running: ["进行中", "processing"],
  succeeded: ["已完成", "success"],
  failed: ["未通过", "error"],
  reused: ["已复用", "cyan"],
  skipped: ["已跳过", "default"],
};
const durationLabel = (value: number) =>
  value < 1000
    ? "不足 1 秒"
    : value < 60000
      ? `${Math.floor(value / 1000)} 秒`
      : `${Math.floor(value / 60000)} 分 ${Math.floor(value / 1000) % 60} 秒`;
export const currentJobStage = (job: Job) => {
  if (job.status === "succeeded") return stageLabel("completed");
  if (job.status === "cancelled") return stageLabel("cancelled");
  if (job.execution_engine === "pi-agent-v1")
    return job.status === "waiting"
      ? "工作已保存，待继续"
      : job.agent_task?.current_action || "Pi Agent 正在处理任务";
  const seen = new Set<string>();
  const currentRecords = [...(job.stage_records ?? [])]
    .reverse()
    .filter((record) => {
      if (seen.has(record.name)) return false;
      seen.add(record.name);
      return true;
    });
  if (job.status === "running") {
    const running = currentRecords.find(
      (record) => record.status === "running",
    );
    return stageLabel(running?.name ?? job.stage);
  }
  if (job.status === "failed") {
    const failed = currentRecords.find((record) => record.status === "failed");
    return stageLabel(failed?.name ?? job.stage);
  }
  return stageLabel(job.stage);
};
export default function WorkflowProgress({
  job,
  mode,
}: {
  job: Job;
  mode: WorkflowMode;
}) {
  if (job.execution_engine === "pi-agent-v1")
    return <AgentTaskProgress task={job.agent_task} status={job.status} />;
  const records = job.stage_records ?? [];
  const active = ["queued", "running"].includes(job.status);
  const repairing = active && records.some((record) => record.status === "running" && isEvidenceRepair(record.name));
  const completed = records.filter((s) => s.status === "succeeded").length;
  const reused = records.filter((s) => s.status === "reused").length;
  if (!records.length)
    return active ? (
      <p className="muted workflow-stage-note">
        {mode === "guided"
          ? "等待后台返回阶段记录。"
          : "正在执行原生笔记与检查流程，尚无阶段明细。"}
        关闭页面不影响任务。
      </p>
    ) : null;
  return (
    <section className="workflow-stages" aria-label="任务阶段记录">
      {repairing && (
        <p className="workflow-stage-note" role="status">
          正在自动修复证据：重新核对原文、调整引文或定位，并再次校验。任务会继续运行。
        </p>
      )}
      <details
        open={active || ["failed", "partial", "cancelled"].includes(job.status)}
      >
        <summary className="workflow-stage-summary">
          <span>{records.length} 项阶段记录</span>
          <span>
            {completed} 项完成{reused > 0 ? ` · ${reused} 项复用` : ""}
          </span>
        </summary>
        <ol className="workflow-stage-grid">
          {records.map((record, index) => {
            const [status, color] = statusLabels[record.status] ?? [
              "状态未知",
              "default",
            ];
            const elapsed =
              record.status === "running" && record.started_at
                ? Math.max(0, Date.now() - Date.parse(record.started_at))
                : undefined;
            return (
              <li
                key={`${record.name}-${record.attempt ?? 0}-${index}`}
                className={`workflow-stage workflow-stage-${record.status}`}
              >
                <div className="workflow-stage-title">
                  <strong>{stageLabel(record.name)}</strong>
                  <Tag color={color}>{record.status === "failed" && isEvidenceRepair(record.name) ? "本轮未通过" : status}</Tag>
                </div>
                <div className="workflow-stage-meta">
                  {record.duration_ms !== undefined && (
                    <span>用时 {durationLabel(record.duration_ms)}</span>
                  )}
                  {elapsed !== undefined && Number.isFinite(elapsed) && (
                    <span>已运行 {durationLabel(elapsed)}</span>
                  )}
                  {record.attempt !== undefined && record.attempt > 1 && (
                    <span>第 {record.attempt} 次尝试</span>
                  )}
                </div>
                {record.status === "reused" && (
                  <p>复用此前保存且输入一致的阶段结果。</p>
                )}
                {record.error && <p className="stage-error">{record.error}</p>}
              </li>
            );
          })}
        </ol>
      </details>
      <WorkflowCoverage summary={stageSummaries(records)} context="本次任务" />
      {active && (
        <p className="muted workflow-stage-note">
          阶段完成后保存结果；当前执行情况以后台记录为准。关闭页面不影响任务。
        </p>
      )}
    </section>
  );
}
