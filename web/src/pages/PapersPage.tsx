import { useState } from "react";
import {
  App,
  Button,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Upload,
} from "antd";
import { InboxOutlined, PlusOutlined, ReadOutlined } from "@ant-design/icons";
import {
  useMutation,
  useInfiniteQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, post, resourceUrl, workspacePath, safeSourceUrl } from "../api";
import { useWorkspace } from "../context";
import {
  ErrorNotice,
  formatDate,
  LoadingBlock,
  NoData,
  PageHeader,
  RefreshButton,
} from "../common";
import ReadingDialog from "../ReadingDialog";
import PaperTitle, { titlePending } from "../PaperTitle";
import type { Paper } from "../types";
export default function PapersPage() {
  const { workspace } = useWorkspace();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const path = (p: string) => workspacePath(workspace.id, p);
  const [search, setSearch] = useState("");
  const [importOpen, setImportOpen] = useState(false);
  const [selected, setSelected] = useState<Paper | null>(null);
  const [form] = Form.useForm();
  const papers = useInfiniteQuery<Paper[]>({
    queryKey: ["workspace", workspace.id, "papers", "feed", search],
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) =>
      lastPage.length === 50 ? pages.length * 50 : undefined,
    queryFn: ({ pageParam }) =>
      api<Paper[]>(
        path(
          `/papers?search=${encodeURIComponent(search)}&limit=50&offset=${pageParam}`,
        ),
      ),
    refetchInterval: (query) =>
      query.state.data?.pages.flat().some((paper) => titlePending(paper) || paper.metadata_json.title_status === "waiting_model")
        ? 3000 : false,
  });
  const rows = papers.data?.pages.flat() ?? [];
  const imported = (paper: Paper) => {
    void queryClient.invalidateQueries({
      queryKey: ["workspace", workspace.id, "papers"],
    });
    setImportOpen(false);
    form.resetFields();
    void message.success("论文已加入团队论文库");
    setSelected(paper);
  };
  const arxiv = useMutation({
    mutationFn: (values: { arxiv: string; title?: string }) =>
      post<Paper>(path("/papers"), values),
    onSuccess: imported,
  });
  const upload = useMutation({
    mutationFn: (file: File) => {
      const data = new FormData();
      data.append("file", file);
      return api<Paper>(path("/papers/upload"), { method: "POST", body: data });
    },
    onSuccess: imported,
  });
  const refreshTitle = useMutation({
    mutationFn: (paper: Paper) => post<Paper>(path(`/papers/${paper.id}/title`), {}),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspace", workspace.id, "papers"] });
      void message.success("已开始重新获取论文原题和中文译名");
    },
    onError: (error: Error) => void message.error(error.message),
  });
  return (
    <>
      <PageHeader
        title="论文库"
        description="团队共同积累论文，从贡献、方法到可迁移的经验。"
        actions={
          <Button
            type="primary"
            icon={<PlusOutlined aria-hidden />}
            onClick={() => {
              arxiv.reset();
              upload.reset();
              setImportOpen(true);
            }}
          >
            导入论文
          </Button>
        }
      />
      <ErrorNotice error={papers.error} retry={papers.refetch} />
      <div className="panel">
        <div className="toolbar">
          <Input.Search
            aria-label="搜索论文"
            placeholder="搜索原题或中文译名"
            allowClear
            onSearch={setSearch}
            style={{ maxWidth: 350 }}
          />
          <div className="toolbar-spacer" />
          <span className="muted">已加载 {rows.length} 篇论文</span>
          <RefreshButton refresh={papers.refetch} loading={papers.isFetching} />
        </div>
        {papers.isPending ? (
          <LoadingBlock />
        ) : (
          <Table<Paper>
            rowKey="id"
            dataSource={rows}
            pagination={{ pageSize: 10, hideOnSinglePage: true }}
            scroll={{ x: 620 }}
            locale={{
              emptyText: (
                <NoData
                  text={
                    search
                      ? "没有找到匹配的论文"
                      : "论文库还是空的。导入 PDF 或 arXiv 论文，开始第一次解读。"
                  }
                />
              ),
            }}
            columns={[
              {
                title: "论文",
                dataIndex: "title",
                render: (_: string, paper) => (
                  <>
                    <PaperTitle paper={paper} />
                    {titlePending(paper) && (
                      <div className="table-sub" role="status">正在获取论文原题与中文译名…</div>
                    )}
                    {typeof paper.metadata_json.title_error === "string" && paper.metadata_json.title_error && (
                      <div className="table-sub" role="status">{paper.metadata_json.title_error}</div>
                    )}
                    <div className="table-sub">
                      {safeSourceUrl(paper.source_url) ? (
                        <a
                          href={safeSourceUrl(paper.source_url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          查看来源
                        </a>
                      ) : (
                        <a
                          href={resourceUrl(
                            workspace.id,
                            `/papers/${paper.id}/file`,
                          )}
                          target="_blank"
                          rel="noreferrer"
                        >
                          打开 PDF
                        </a>
                      )}
                      {!titlePending(paper) && (
                        <Button
                          type="link"
                          size="small"
                          className="paper-title-retry"
                          loading={refreshTitle.isPending && refreshTitle.variables?.id === paper.id}
                          onClick={() => refreshTitle.mutate(paper)}
                        >
                          {paper.metadata_json.title_status === "ready" ? "重新获取标题" : "重试获取标题"}
                        </Button>
                      )}
                    </div>
                  </>
                ),
              },
              {
                title: "来源",
                dataIndex: "source_type",
                width: 85,
                render: (source: string) => (
                  <Tag>{source === "arxiv" ? "arXiv" : "PDF"}</Tag>
                ),
              },
              {
                title: "导入时间",
                dataIndex: "created_at",
                width: 165,
                render: formatDate,
              },
              {
                title: "操作",
                width: 115,
                render: (_, paper) => (
                  <Button
                    icon={<ReadOutlined aria-hidden />}
                    onClick={() => setSelected(paper)}
                  >
                    解读论文
                  </Button>
                ),
              },
            ]}
          />
        )}
        {papers.hasNextPage && (
          <div className="load-more">
            <Button
              loading={papers.isFetchingNextPage}
              onClick={() => void papers.fetchNextPage()}
            >
              加载更多论文
            </Button>
          </div>
        )}
      </div>
      <Modal
        title="导入论文"
        open={importOpen}
        onCancel={() => setImportOpen(false)}
        footer={null}
        destroyOnHidden
      >
        <Tabs
          items={[
            {
              key: "arxiv",
              label: "arXiv 链接或编号",
              children: (
                <Form
                  layout="vertical"
                  form={form}
                  onFinish={(values) => arxiv.mutate(values)}
                >
                  <ErrorNotice error={arxiv.error} />
                  <Form.Item
                    label="论文链接或编号"
                    name="arxiv"
                    rules={[
                      { required: true, message: "请输入 arXiv 链接或编号" },
                    ]}
                  >
                    <Input placeholder="https://arxiv.org/abs/2401.12345 或 2401.12345" />
                  </Form.Item>
                  <Form.Item label="自定义标题（可选）" name="title" extra="留空会在导入后自动获取论文原题；英文标题下方显示中文译名。">
                    <Input placeholder="默认使用论文原版标题" />
                  </Form.Item>
                  <Button
                    type="primary"
                    htmlType="submit"
                    loading={arxiv.isPending}
                  >
                    导入并选择解读方式
                  </Button>
                </Form>
              ),
            },
            {
              key: "pdf",
              label: "上传 PDF",
              children: (
                <Space direction="vertical" style={{ width: "100%" }}>
                  <ErrorNotice error={upload.error} />
                  <Upload.Dragger
                    accept=".pdf,application/pdf"
                    multiple={false}
                    showUploadList={false}
                    disabled={upload.isPending}
                    beforeUpload={(file) => {
                      if (
                        file.type !== "application/pdf" &&
                        !file.name.toLowerCase().endsWith(".pdf")
                      ) {
                        void message.error("请选择 PDF 文件");
                        return false;
                      }
                      upload.mutate(file);
                      return false;
                    }}
                  >
                    <p className="ant-upload-drag-icon">
                      <InboxOutlined aria-hidden />
                    </p>
                    <p>
                      {upload.isPending
                        ? "正在上传，请稍候…"
                        : "点击选择或拖拽 PDF 到这里"}
                    </p>
                    <p className="ant-upload-hint">
                      上传后自动识别论文原题，并为英文标题添加中文译名。
                    </p>
                  </Upload.Dragger>
                </Space>
              ),
            },
          ]}
        />
      </Modal>
      <ReadingDialog paper={rows.find((paper) => paper.id === selected?.id) ?? selected} onClose={() => setSelected(null)} />
    </>
  );
}
