import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Form,
  Input,
  List,
  Popconfirm,
  Select,
  Space,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  ArrowLeftOutlined,
  DownloadOutlined,
  FileTextOutlined,
  HistoryOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, resourceUrl, safeSourceUrl, workspacePath } from "../api";
import { useWorkspace } from "../context";
import {
  ErrorNotice,
  formatDate,
  JsonBlock,
  LoadingBlock,
  NoData,
  PageHeader,
  StatusTag,
} from "../common";
import Markdown from "../Markdown";
import PaperChat from "../PaperChat";
import WorkflowProgress, {
  currentJobStage,
  workflowModeLabel,
} from "../WorkflowProgress";
import ReadingPacketView, { isReadingPacket } from "../ReadingPacketView";
import VerificationSummary from "../VerificationSummary";
import WorkflowCoverage from "../WorkflowCoverage";
import EvidenceContent, {
  EvidenceLabels,
  evidencePreview,
  isVerifiedEvidence,
} from "../EvidenceContent";
import type { Evidence, Job, Paper, ReadingDetail } from "../types";
export default function ReadingPage() {
  const { workspace, user } = useWorkspace();
  const { readingId } = useParams();
  const path = (p: string) => workspacePath(workspace.id, p);
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [selectedVersion, setSelectedVersion] = useState<string>();
  const [view, setView] = useState("brief");
  const [evidence, setEvidence] = useState<Evidence>();
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const [form] = Form.useForm();
  const detail = useQuery({
    queryKey: ["workspace", workspace.id, "reading", readingId],
    queryFn: () => api<ReadingDetail>(path(`/readings/${readingId}`)),
    refetchInterval: (q) =>
      q.state.data?.jobs.some((j) => ["queued", "running"].includes(j.status))
        ? 2500
        : false,
  });
  const paper = useQuery({
    queryKey: [
      "workspace",
      workspace.id,
      "paper",
      detail.data?.paper_id,
      detail.data?.current_revision_id,
    ],
    queryFn: () => api<Paper>(path(`/papers/${detail.data!.paper_id}`)),
    enabled: !!detail.data,
  });
  const refresh = () => {
    void queryClient.invalidateQueries({
      queryKey: ["workspace", workspace.id, "reading", readingId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["workspace", workspace.id, "readings"],
    });
  };
  const action = useMutation({
    mutationFn: ({ job, kind }: { job: Job; kind: "retry" | "cancel" | "continue" }) =>
      post(path(`/jobs/${job.id}/${kind}`)),
    onSuccess: () => refresh(),
  });
  const revise = useMutation({
    mutationFn: (values: { instruction: string }) =>
      post(path(`/readings/${readingId}/revisions`), {
        ...values,
        base_revision_id: detail.data!.current_revision_id,
      }),
    onSuccess: () => {
      form.resetFields();
      refresh();
      setSelectedVersion(undefined);
      void message.success("修订已开始，现有版本会保留。");
    },
  });
  if (detail.isPending) return <LoadingBlock />;
  if (!detail.data)
    return <ErrorNotice error={detail.error} retry={detail.refetch} />;
  const reading = detail.data;
  const workflowMode = reading.workflow_mode ?? "native";
  const revisions = [...reading.revisions].sort((a, b) => b.number - a.number);
  const revision =
    revisions.find(
      (r) => r.id === (selectedVersion ?? reading.current_revision_id),
    ) ?? revisions[0];
  const rawPacket = revision?.reading_packet;
  const packet = isReadingPacket(rawPacket) ? rawPacket : undefined;
  const effectiveView = view === "packet" && !packet ? "brief" : view;
  const exportView = effectiveView === "brief" ? "brief" : "full";
  const latestJob = [...reading.jobs].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
  )[0];
  const active = reading.jobs.some((j) =>
    ["queued", "running"].includes(j.status),
  );
  const agentExecution = latestJob?.execution_engine === "pi-agent-v1";
  const canHandOver = latestJob && !agentExecution &&
    (["failed", "cancelled"].includes(latestJob.status) ||
      (latestJob.status === "partial" && !reading.current_revision_id));
  const historic = revision && revision.id !== reading.current_revision_id;
  const showEvidence = (item: Evidence) => {
    setEvidence(item);
    setEvidenceOpen(true);
  };
  const verifiedCount =
    revision?.evidence_index.filter(isVerifiedEvidence).length ?? 0;
  const evidenceCount = revision?.evidence_index.length ?? 0;
  const sourceMimeType = paper.data?.metadata_json.source_mime_type;
  const sourceIsPdf = sourceMimeType === "application/pdf";
  const sourceIsHtml = sourceMimeType === "text/html";
  const sourceFileUrl = resourceUrl(
    workspace.id,
    `/papers/${reading.paper_id}/file`,
  );
  const evidenceSourceUrl =
    safeSourceUrl(evidence?.source_url) ??
    (evidence?.kind === "paper"
      ? safeSourceUrl(paper.data?.source_url)
      : undefined);
  return (
    <>
      <Link className="back-link" to="/readings">
        <ArrowLeftOutlined aria-hidden /> 返回解读记录
      </Link>
      <PageHeader
        title={reading.paper_title}
        description={`${workflowModeLabel(workflowMode)} · ${workflowMode === "guided" ? "阅读指引" : "阅读 Skill"}：${reading.skill_id === "deeppapernote" ? "DeepPaperNote" : reading.skill_id}`}
        actions={
          <Space wrap>
            {paper.data?.source_url && safeSourceUrl(paper.data.source_url) ? (
              <Button
                href={safeSourceUrl(paper.data.source_url)}
                target="_blank"
                icon={<FileTextOutlined aria-hidden />}
              >
                论文原文
              </Button>
            ) : (
              <Button
                href={sourceFileUrl}
                target="_blank"
                icon={<FileTextOutlined aria-hidden />}
              >
                {sourceIsPdf
                  ? "论文 PDF"
                  : sourceIsHtml
                    ? "下载 HTML 原文"
                    : "论文原文"}
              </Button>
            )}
            {revision && (
              <Button
                icon={<DownloadOutlined aria-hidden />}
                href={resourceUrl(
                  workspace.id,
                  `/readings/${readingId}/export?revision_id=${revision.id}&view=${exportView}`,
                )}
              >
                {effectiveView === "packet" ? "导出完整解读" : "导出 Markdown"}
              </Button>
            )}
          </Space>
        }
      />
      <ErrorNotice
        error={detail.error || action.error}
        retry={detail.refetch}
      />
      {latestJob && (
        <div className="panel task-panel block-gap">
          <div className="task-heading">
            <Space wrap>
              <StatusTag value={latestJob.status} />
              <span>
                {latestJob.status === "failed"
                  ? "本次任务未能完成"
                  : latestJob.status === "partial"
                    ? "已有解读，部分证据仍需核查"
                    : latestJob.status === "succeeded"
                      ? "处理完成"
                      : latestJob.status === "cancelled"
                        ? "任务已取消"
                        : latestJob.status === "waiting"
                          ? "进展已保存，等待继续"
                          : currentJobStage(latestJob)}
              </span>
            </Space>
            <Space>
              {["queued", "running"].includes(latestJob.status) && (
                <Popconfirm
                  title={agentExecution ? "停止本次任务？" : "取消本次任务？"}
                  description={agentExecution ? "已保存的工作会保留，可以稍后继续。" : "已发布的解读版本会保留。"}
                  onConfirm={() =>
                    action.mutate({ job: latestJob, kind: "cancel" })
                  }
                >
                  <Button loading={action.isPending}>{agentExecution ? "停止任务" : "取消任务"}</Button>
                </Popconfirm>
              )}
              {!active && ((latestJob.agent_task?.can_continue && ["waiting", "failed", "cancelled"].includes(latestJob.status)) || canHandOver) && (
                <Button
                  type="primary"
                  loading={action.isPending}
                  onClick={() => action.mutate({ job: latestJob, kind: "continue" })}
                >
                  {agentExecution ? "继续任务" : "交给 Agent 继续"}
                </Button>
              )}
              {(["failed", "cancelled"].includes(latestJob.status) ||
                (latestJob.status === "partial" &&
                  !reading.current_revision_id)) &&
                !active && !agentExecution && (
                  <Button
                    loading={action.isPending}
                    onClick={() =>
                      action.mutate({ job: latestJob, kind: "retry" })
                    }
                  >
                    重新尝试
                  </Button>
                )}
            </Space>
          </div>
          <WorkflowProgress job={latestJob} mode={workflowMode} />
          {latestJob.error && latestJob.error !== latestJob.agent_task?.issue && (
            <Alert
              type={latestJob.status === "failed" ? "error" : "warning"}
              showIcon
              message={latestJob.error}
            />
          )}
          {reading.jobs.length > 1 && (
            <details className="agent-task-history">
              <summary>此前任务记录 · {reading.jobs.length - 1} 次</summary>
              {reading.jobs.filter((job) => job.id !== latestJob.id).map((job) => (
                <div key={job.id} className="agent-history-entry">
                  <Space wrap><StatusTag value={job.status} /><time>{formatDate(job.created_at)}</time></Space>
                  <p>{job.execution_engine === "pi-agent-v1" ? "Pi Agent" : "原有流程"} · {currentJobStage(job)}</p>
                  {job.error && <p className="muted">{job.error}</p>}
                </div>
              ))}
            </details>
          )}
        </div>
      )}
      {!revision ? (
        <div className="panel reading-empty">
          <NoData
            text={
              active
                ? "正在阅读全文、整理方法并核查证据。解读完成后会显示在这里。"
                : latestJob?.status === "waiting"
                  ? "工作已保存，解读尚未发布。继续任务后，Agent 会完成剩余阅读与核查。"
                  : "暂无可阅读的结果。可继续任务，或返回论文库更换解读方式。"
            }
          />
        </div>
      ) : (
        <div className="reading-grid reading-grid-with-chat">
          <div className="reading-body">
            <div className="panel reading-paper">
              {active && (
                <Alert
                  type="info"
                  showIcon
                  message={`新版本正在处理中；下方显示已发布的版本 ${revision.number}，核查状态属于此版本。`}
                />
              )}
              <div className="reading-controls">
                <Tabs
                  activeKey={effectiveView}
                  onChange={setView}
                  items={[
                    { key: "brief", label: "精华版" },
                    { key: "full", label: "完整解读" },
                    ...(packet ? [{ key: "packet", label: "结论与条件" }] : []),
                  ]}
                />
                <Select
                  aria-label="解读版本"
                  value={revision.id}
                  onChange={setSelectedVersion}
                  options={revisions.map((r) => ({
                    value: r.id,
                    label: `版本 ${r.number}${r.id === reading.current_revision_id ? " · 当前" : ""}`,
                  }))}
                />
              </div>
              {historic && (
                <Alert
                  className="block-gap"
                  type="info"
                  message="正在查看历史版本。修订操作以当前版本为基础。"
                  action={
                    <Button
                      size="small"
                      onClick={() => setSelectedVersion(undefined)}
                    >
                      回到当前版本
                    </Button>
                  }
                />
              )}
              <div className="reading-text">
                {effectiveView === "packet" && packet ? (
                  <ReadingPacketView
                    packet={packet}
                    evidence={revision.evidence_index}
                    onEvidence={showEvidence}
                  />
                ) : (
                  <Markdown
                    content={
                      effectiveView === "brief"
                        ? revision.brief_markdown
                        : revision.full_markdown
                    }
                    evidence={revision.evidence_index}
                    onEvidence={showEvidence}
                  />
                )}
              </div>
            </div>
            <div className="panel revision-form">
              <h2>让这份解读更有用</h2>
              <p className="muted">
                指出需要压缩、讲清或重新核查的地方。系统会保留当前结果，并生成新版本。
              </p>
              <ErrorNotice error={revise.error} />
              <Form
                form={form}
                layout="vertical"
                onFinish={(values) => revise.mutate(values)}
              >
                <Form.Item
                  name="instruction"
                  label="修订要求"
                  rules={[
                    {
                      required: true,
                      whitespace: true,
                      message: "请描述你希望怎样修改",
                    },
                  ]}
                >
                  <Input.TextArea
                    rows={3}
                    disabled={active || historic}
                    placeholder="例如：把方法写成输入、关键操作和输出。模型合并的结论是否过度推广？请补充适用条件。"
                  />
                </Form.Item>
                <Button
                  htmlType="submit"
                  type="primary"
                  disabled={active || historic}
                  loading={revise.isPending}
                >
                  提交修订
                </Button>
                {active && (
                  <span className="muted inline-note">
                    当前任务完成后可以继续修订。
                  </span>
                )}
              </Form>
            </div>
          </div>
          <aside className="reading-aside">
            <PaperChat
              key={`${user.id}:${workspace.id}:${reading.id}`}
              readingId={reading.id}
              revisionId={revision.id}
              currentRevisionId={reading.current_revision_id}
              revisions={revisions}
            />
            <div className="panel evidence-summary">
              <h3>
                <SafetyCertificateOutlined aria-hidden /> 证据与来源
              </h3>
              <div className="evidence-count">
                <strong>{verifiedCount}</strong>
                <span> / {evidenceCount} 条证据已核验</span>
              </div>
              <p className="muted">
                阅读时点击引用编号，可查看证据内容、核验方式和适用条件。
              </p>
              {verifiedCount < evidenceCount && (
                <Alert
                  type="warning"
                  showIcon
                  message="部分证据尚未确认核验，请结合核查报告与原文判断。"
                />
              )}
              <Button
                block
                onClick={() => {
                  setEvidence(undefined);
                  setEvidenceOpen(true);
                }}
              >
                查看全部证据
              </Button>
            </div>
            <VerificationSummary
              verification={revision.verification}
              mode={workflowMode}
              hasTypedEvidence={revision.evidence_index.some(
                (item) =>
                  item.evidence_type === "table_cell" ||
                  item.evidence_type === "image_observation" ||
                  item.verification_method === "table_cell_match" ||
                  item.verification_method === "visual_observation",
              )}
            />
            <WorkflowCoverage summary={revision.verification} />
            <div className="panel history-panel">
              <h3>
                <HistoryOutlined aria-hidden /> 修订记录
              </h3>
              <List
                dataSource={revisions}
                renderItem={(r) => (
                  <List.Item>
                    <button
                      className={`history-entry ${r.id === revision.id ? "selected" : ""}`}
                      onClick={() => setSelectedVersion(r.id)}
                    >
                      <strong>版本 {r.number}</strong>
                      <span>{r.instruction || "首次解读"}</span>
                      <time>{formatDate(r.created_at)}</time>
                    </button>
                  </List.Item>
                )}
              />
            </div>
            <Collapse
              className="diagnostics"
              items={[
                {
                  key: "artifacts",
                  label: `${workflowMode === "guided" ? "证据与核查附件" : "原始笔记与附件"}（${revision.artifacts.length}）`,
                  children: revision.artifacts.length ? (
                    <List
                      size="small"
                      dataSource={revision.artifacts}
                      renderItem={(artifact, index) => (
                        <List.Item>
                          <a
                            href={resourceUrl(
                              workspace.id,
                              `/artifacts/${revision.id}/${index}`,
                            )}
                            target="_blank"
                            rel="noreferrer"
                          >
                            {artifact.name}
                          </a>
                        </List.Item>
                      )}
                    />
                  ) : (
                    <span className="muted">本次未保存附件。</span>
                  ),
                },
                {
                  key: "verification",
                  label: "查看核查详情",
                  children: <JsonBlock value={revision.verification} />,
                },
                {
                  key: "provenance",
                  label: "版本与用量记录",
                  children: (
                    <>
                      <JsonBlock value={revision.provenance} />
                      {latestJob?.usage && (
                        <JsonBlock value={latestJob.usage} />
                      )}
                    </>
                  ),
                },
              ]}
            />
          </aside>
        </div>
      )}
      <Drawer
        title={evidence ? `证据 ${evidence.id}` : "证据与来源"}
        open={evidenceOpen}
        onClose={() => setEvidenceOpen(false)}
        width={540}
      >
        {evidence ? (
          <>
            <Space wrap className="block-gap">
              <Tag
                color={
                  evidence.kind === "inference"
                    ? "orange"
                    : evidence.kind === "external"
                      ? "blue"
                      : "cyan"
                }
              >
                {
                  {
                    paper: "本文证据",
                    external: "外部论文",
                    inference: "待验证推断",
                  }[evidence.kind]
                }
              </Tag>
              <EvidenceLabels item={evidence} />
            </Space>
            <h3>{evidence.source_title}</h3>
            <EvidenceContent item={evidence} />
            <Descriptions
              column={1}
              size="small"
              items={[
                {
                  key: "section",
                  label: "章节",
                  children: evidence.section || evidence.section_id || "未提供",
                },
                ...(evidence.kind !== "paper" || sourceIsPdf
                  ? [
                      {
                        key: "page",
                        label: "页码",
                        children: evidence.page ?? "未提供",
                      },
                    ]
                  : []),
              ]}
            />
            <Space wrap className="block-gap">
              {evidenceSourceUrl && (
                <Button href={evidenceSourceUrl} target="_blank">
                  打开来源
                </Button>
              )}
              {evidence.kind === "paper" && sourceIsPdf && (
                <Button
                  href={`${sourceFileUrl}${evidence.page ? `#page=${evidence.page}` : ""}`}
                  target="_blank"
                >
                  定位原文 PDF
                </Button>
              )}
              {evidence.kind === "paper" && sourceIsHtml && (
                <Button href={sourceFileUrl} target="_blank">
                  下载 HTML 原文
                </Button>
              )}
              <Button type="text" onClick={() => setEvidence(undefined)}>
                全部证据
              </Button>
            </Space>
          </>
        ) : (
          <List
            locale={{ emptyText: "未找到证据记录" }}
            dataSource={revision?.evidence_index ?? []}
            renderItem={(e) => (
              <List.Item>
                <button
                  className="evidence-entry"
                  onClick={() => setEvidence(e)}
                >
                  <Space wrap>
                    <Tag>{e.id}</Tag>
                    <EvidenceLabels item={e} />
                  </Space>
                  <Typography.Paragraph ellipsis={{ rows: 3 }}>
                    {evidencePreview(e)}
                  </Typography.Paragraph>
                  <span className="muted">{e.source_title}</span>
                </button>
              </List.Item>
            )}
          />
        )}
      </Drawer>
    </>
  );
}
