import { Alert, Tag } from "antd";
import type { AgentTask } from "./types";

const duration = (seconds: number) => {
  if (!Number.isFinite(seconds)) return "—";
  const value = Math.max(0, Math.floor(seconds));
  if (value < 60) return `${value} 秒`;
  return `${Math.floor(value / 60)} 分 ${value % 60} 秒`;
};
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
const recordOf = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const count = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value.toLocaleString("zh-CN")
    : "—";

function WorkList({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="agent-work-list" aria-label={title}>
      <h4>{title}</h4>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </section>
  );
}

export function AgentCoverage({
  coverage: value,
  compact = false,
}: {
  coverage: unknown;
  compact?: boolean;
}) {
  const coverage = recordOf(value);
  const rows = [
    ["body", "正文阅读", "read"],
    ["appendix", "附录阅读", "read"],
    ["figures", "插图查看", "read"],
    ["tables", "表格查看", "read"],
    ["references", "参考文献索引", "indexed"],
  ].filter(([key]) => Object.keys(recordOf(coverage[key])).length > 0);
  if (!rows.length) return null;
  const unfinished = [
    ...strings(coverage.missing_sections),
    ...strings(coverage.missing_figures),
    ...strings(coverage.missing_tables),
  ];
  return (
    <details className="agent-task-coverage" open={!compact}>
      <summary>
        {coverage.scope === "question" ? "当前问题的资料覆盖" : "论文资料覆盖"}
      </summary>
      <dl>
        {rows.map(([key, label, field]) => {
          const item = recordOf(coverage[key]);
          return (
            <div key={key}>
              <dt>{label}</dt>
              <dd>
                {count(item[field])} / {count(item.total)}
              </dd>
            </div>
          );
        })}
      </dl>
      <p className="muted">
        阅读覆盖与内容核查分别记录；已查看的资料仍需完成相应复核。
      </p>
      {!!unfinished.length && (
        <details>
          <summary>尚待查看 · {unfinished.length} 项</summary>
          <ul>
            {unfinished.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </details>
      )}
    </details>
  );
}

export default function AgentTaskProgress({
  task,
  status,
  compact = false,
}: {
  task?: AgentTask | null;
  status: string;
  compact?: boolean;
}) {
  const active = ["queued", "running"].includes(status);
  if (!task)
    return active ? (
      <p className="muted workflow-stage-note" role="status">
        Pi Agent 正在接收任务。关闭页面不影响执行。
      </p>
    ) : null;
  return (
    <section
      className={`agent-task-progress${compact ? " agent-task-compact" : ""}`}
      aria-label="Agent 任务进度"
    >
      <div className="agent-task-goal">
        <Tag color="blue">Pi Agent</Tag>
        <strong>{task.goal || "完成论文任务并核查结果"}</strong>
      </div>
      {task.current_action && (
        <p
          className="agent-current-action"
          role={active ? "status" : undefined}
        >
          <span>{active ? "正在处理" : "最近进展"}</span>
          {task.current_action}
        </p>
      )}
      {task.issue && (
        <Alert
          type="warning"
          showIcon
          message={status === "waiting" ? "任务待继续" : "当前待解决的问题"}
          description={task.issue}
        />
      )}
      {status === "waiting" && (
        <p className="agent-waiting-note">
          已保存工作和草稿。
          {task.can_continue
            ? "继续后将从已有进展接着处理。"
            : "需先解决当前问题，再继续任务。"}
          {compact ? "回答仍待核验。" : "全部必要核查通过后才会发布解读。"}
        </p>
      )}
      <dl className="agent-task-budget" aria-label="任务用量">
        <div>
          <dt>累计用时</dt>
          <dd>{duration(task.elapsed_seconds)}</dd>
        </div>
        <div>
          <dt>剩余时间</dt>
          <dd>{duration(task.remaining_seconds)}</dd>
        </div>
        <div>
          <dt>累计工具调用</dt>
          <dd>{count(task.tool_calls)}</dd>
        </div>
        <div>
          <dt>剩余工具调用</dt>
          <dd>{count(task.remaining_tool_calls)}</dd>
        </div>
      </dl>
      {task.can_continue && ["waiting", "failed", "cancelled"].includes(status) && (
        <p className="muted workflow-stage-note">
          继续将增加 {compact ? 10 : 60} 分钟和 {compact ? 120 : 500} 次工具调用额度，累计用量会保留。
        </p>
      )}
      <div className="agent-work-columns">
        <WorkList title="已保存的工作" items={strings(task.accepted_work)} />
        <WorkList title="接下来需要完成" items={strings(task.remaining_work)} />
      </div>
      <AgentCoverage coverage={task.coverage} compact={compact} />
      {active && !compact && (
        <p className="muted workflow-stage-note">
          Agent 会根据原文和核查反馈调整后续工作。关闭页面不影响任务。
        </p>
      )}
    </section>
  );
}
