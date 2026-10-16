import { Button, Table, Tag } from "antd";
import { Link } from "react-router-dom";
import { useInfiniteQuery } from "@tanstack/react-query";
import { api, workspacePath } from "../api";
import { useWorkspace } from "../context";
import {
  ErrorNotice,
  formatDate,
  NoData,
  PageHeader,
  RefreshButton,
  StatusTag,
} from "../common";
import { currentJobStage, workflowModeLabel } from "../WorkflowProgress";
import type { Reading } from "../types";
export default function ReadingsPage() {
  const { workspace } = useWorkspace();
  const readings = useInfiniteQuery<Reading[]>({
    queryKey: ["workspace", workspace.id, "readings"],
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === 50 ? pages.length * 50 : undefined,
    queryFn: ({ pageParam }) =>
      api<Reading[]>(
        workspacePath(workspace.id, `/readings?limit=50&offset=${pageParam}`),
      ),
    refetchInterval: (query) =>
      query.state.data?.pages
        .flat()
        .some((r) => ["queued", "running"].includes(r.latest_job?.status ?? ""))
        ? 3000
        : false,
  });
  return (
    <>
      <PageHeader
        title="解读记录"
        description="查看团队的论文解读、后台进度与修订结果。"
        actions={
          <RefreshButton
            refresh={readings.refetch}
            loading={readings.isFetching}
          />
        }
      />
      <ErrorNotice error={readings.error} retry={readings.refetch} />
      <div className="panel">
        <Table<Reading>
          loading={readings.isPending}
          rowKey="id"
          dataSource={readings.data?.pages.flat()}
          scroll={{ x: 700 }}
          pagination={{ pageSize: 12, hideOnSinglePage: true }}
          locale={{
            emptyText: (
              <NoData
                text="尚未创建解读。从论文库选择一篇论文开始。"
                action={
                  <Link to="/papers">
                    <Button>打开论文库</Button>
                  </Link>
                }
              />
            ),
          }}
          columns={[
            {
              title: "论文",
              dataIndex: "paper_title",
              render: (title: string, r) => (
                <Link className="paper-title" to={`/readings/${r.id}`}>
                  {title}
                </Link>
              ),
            },
            {
              title: "阅读 Skill",
              dataIndex: "skill_id",
              width: 150,
              render: (id: string, reading) => (
                <>
                  <Tag>
                    {id === "deeppapernote"
                      ? "DeepPaperNote"
                      : id === "ljg-paper"
                        ? "ljg-paper"
                        : id}
                  </Tag>
                  <div className="table-sub">
                    {workflowModeLabel(reading.workflow_mode)}
                  </div>
                </>
              ),
            },
            {
              title: "状态",
              width: 155,
              render: (_, r) =>
                r.latest_job ? (
                  <>
                    <StatusTag value={r.latest_job.status} />
                    {["running", "failed", "waiting"].includes(r.latest_job.status) && (
                      <div className="table-sub">
                        {currentJobStage(r.latest_job)}
                      </div>
                    )}
                  </>
                ) : (
                  <span className="muted">等待任务</span>
                ),
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              width: 175,
              render: formatDate,
            },
            {
              title: "操作",
              width: 90,
              render: (_, r) => <Link to={`/readings/${r.id}`}>查看解读</Link>,
            },
          ]}
        />
        {readings.hasNextPage && (
          <div className="load-more">
            <Button
              loading={readings.isFetchingNextPage}
              onClick={() => void readings.fetchNextPage()}
            >
              加载更多解读
            </Button>
          </div>
        )}
      </div>
    </>
  );
}
