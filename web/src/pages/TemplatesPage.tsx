import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Form,
  Input,
  List,
  Modal,
  Select,
  Space,
  Tabs,
  Tag,
} from "antd";
import { CopyOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, workspacePath } from "../api";
import { useWorkspace } from "../context";
import {
  ErrorNotice,
  formatDate,
  LoadingBlock,
  NoData,
  PageHeader,
} from "../common";
import Markdown from "../Markdown";
import ReadingDialog from "../ReadingDialog";
import type { Paper, Template, TemplateBody, TemplateVersion } from "../types";
export default function TemplatesPage() {
  const { workspace, user } = useWorkspace();
  const path = (p: string) => workspacePath(workspace.id, p);
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [form] = Form.useForm<TemplateBody>();
  const [editor, setEditor] = useState(false);
  const [editing, setEditing] = useState<Template | null>(null);
  const [history, setHistory] = useState<Template | null>(null);
  const [previewTemplate, setPreviewTemplate] = useState<Template | null>(null);
  const [paperId, setPaperId] = useState<string>();
  const [paperSearch, setPaperSearch] = useState("");
  const [previewPaper, setPreviewPaper] = useState<Paper | null>(null);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>();
  const templates = useQuery({
    queryKey: ["workspace", workspace.id, "templates"],
    queryFn: () => api<Template[]>(path("/templates")),
  });
  const versions = useQuery({
    queryKey: ["workspace", workspace.id, "template-versions", history?.id],
    queryFn: () =>
      api<TemplateVersion[]>(path(`/templates/${history!.id}/versions`)),
    enabled: !!history,
  });
  const papers = useQuery({
    queryKey: ["workspace", workspace.id, "papers", "preview", paperSearch],
    queryFn: () =>
      api<Paper[]>(path(`/papers?search=${encodeURIComponent(paperSearch)}`)),
    enabled: !!previewTemplate,
  });
  const save = useMutation({
    mutationFn: (values: TemplateBody) =>
      editing
        ? api<Template>(path(`/templates/${editing.id}`), {
            method: "PUT",
            body: JSON.stringify(values),
          })
        : post<Template>(path("/templates"), values),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["workspace", workspace.id, "templates"],
      });
      void queryClient.invalidateQueries({
        queryKey: ["workspace", workspace.id, "template-versions"],
      });
      setEditor(false);
      void message.success("模板版本已保存");
    },
  });
  const openEditor = (template: Template | null, copy = false) => {
    save.reset();
    setEditing(copy ? null : template);
    form.setFieldsValue(
      template
        ? {
            name: copy ? `${template.name} · 副本` : template.name,
            ...{
              brief_markdown: template.current_version.brief_markdown,
              full_markdown: template.current_version.full_markdown,
              instructions: template.current_version.instructions,
            },
          }
        : { name: "", brief_markdown: "", full_markdown: "", instructions: "" },
    );
    setEditor(true);
  };
  const brief = Form.useWatch("brief_markdown", form);
  const full = Form.useWatch("full_markdown", form);
  return (
    <>
      <PageHeader
        title="解读模板"
        description="结构、篇幅和重点由你定义。同一份证据，生成精华与完整两种解读。"
        actions={
          <Button
            type="primary"
            icon={<PlusOutlined aria-hidden />}
            onClick={() => openEditor(null)}
          >
            新建模板
          </Button>
        }
      />
      <ErrorNotice error={templates.error} retry={templates.refetch} />
      {templates.isPending ? (
        <LoadingBlock />
      ) : (
        <div className="template-list">
          {templates.data?.length ? (
            templates.data.map((t) => (
              <section className="panel template-panel" key={t.id}>
                <div className="template-heading">
                  <h2>{t.name}</h2>
                  <Space>
                    <Tag>
                      {t.is_builtin
                        ? "内置模板"
                        : t.owner_id === user.id
                          ? "我的模板"
                          : "团队模板"}
                    </Tag>
                    <span className="muted">
                      版本 {t.current_version.number}
                    </span>
                  </Space>
                </div>
                <p className="muted template-description">
                  {t.current_version.instructions ||
                    "按精华版与完整版各自的 Markdown 要求生成解读。"}
                </p>
                <div className="template-structure">
                  <div>
                    <span className="small-label">精华版结构</span>
                    <pre>{t.current_version.brief_markdown.slice(0, 400)}</pre>
                  </div>
                  <div>
                    <span className="small-label">完整版结构</span>
                    <pre>{t.current_version.full_markdown.slice(0, 400)}</pre>
                  </div>
                </div>
                <div className="template-actions">
                  <Button
                    icon={<CopyOutlined aria-hidden />}
                    onClick={() => openEditor(t, true)}
                  >
                    复制模板
                  </Button>
                  {!t.is_builtin &&
                    (t.owner_id === user.id || workspace.role === "admin") && (
                      <Button onClick={() => openEditor(t)}>编辑</Button>
                    )}
                  <Button onClick={() => setHistory(t)}>历史版本</Button>
                  <Button
                    type="primary"
                    ghost
                    onClick={() => {
                      setPreviewTemplate(t);
                      setPaperId(undefined);
                    }}
                  >
                    用论文预览
                  </Button>
                </div>
              </section>
            ))
          ) : (
            <NoData text="暂无模板，创建一个符合你的阅读习惯的模板。" />
          )}
        </div>
      )}
      <Drawer
        title={editing ? "编辑模板 · 保存为新版本" : "新建解读模板"}
        width={920}
        open={editor}
        onClose={() => setEditor(false)}
        extra={
          <Button
            type="primary"
            loading={save.isPending}
            onClick={() => form.submit()}
          >
            保存模板
          </Button>
        }
      >
        <ErrorNotice error={save.error} />
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => save.mutate(values)}
        >
          <Form.Item
            name="name"
            label="模板名称"
            rules={[
              { required: true, whitespace: true, message: "请输入模板名称" },
            ]}
          >
            <Input placeholder="例如：方法复现与边界" />
          </Form.Item>
          <Form.Item name="instructions" label="共同写作要求">
            <Input.TextArea
              rows={3}
              placeholder="例如：面向机器学习工程师，减少背景铺垫，保留关键实验条件。不确定的结论明确标注。"
            />
          </Form.Item>
          <Alert
            className="block-gap"
            type="info"
            message="自由编写结构、写作要求和示例，无需模板语法。引用与证据会独立保存。"
          />
          <Tabs
            items={[
              {
                key: "brief",
                label: "精华版",
                forceRender: true,
                children: (
                  <Form.Item
                    name="brief_markdown"
                    label="精华版 Markdown"
                    rules={[
                      {
                        required: true,
                        whitespace: true,
                        message: "请输入精华版结构或要求",
                      },
                    ]}
                  >
                    <Input.TextArea
                      className="markdown-editor"
                      rows={15}
                      placeholder={
                        "# 一句话贡献\n\n# 核心方法\n\n# 可以借鉴的经验\n\n以 600–1000 字回答，每条经验注明条件与边界。"
                      }
                    />
                  </Form.Item>
                ),
              },
              {
                key: "full",
                label: "完整版",
                forceRender: true,
                children: (
                  <Form.Item
                    name="full_markdown"
                    label="完整版 Markdown"
                    rules={[
                      {
                        required: true,
                        whitespace: true,
                        message: "请输入完整版结构或要求",
                      },
                    ]}
                  >
                    <Input.TextArea
                      className="markdown-editor"
                      rows={15}
                      placeholder={
                        "# 研究问题与相关工作\n\n# 方法：输入、操作、输出\n\n# 实验成果与消融\n\n# 经验、反例与限制"
                      }
                    />
                  </Form.Item>
                ),
              },
              {
                key: "format",
                label: "查看 Markdown 排版",
                children: (
                  <>
                    <p className="muted">
                      这里只展示模板排版。保存后选择「用论文预览」，可查看真实生成结果。
                    </p>
                    <h3>精华版模板</h3>
                    <Markdown content={brief || "尚未填写"} />
                    <h3>完整版模板</h3>
                    <Markdown content={full || "尚未填写"} />
                  </>
                ),
              },
            ]}
          />
        </Form>
      </Drawer>
      <Drawer
        title={`${history?.name ?? ""} · 历史版本`}
        width={650}
        open={!!history}
        onClose={() => setHistory(null)}
      >
        <ErrorNotice error={versions.error} />
        <List
          loading={versions.isPending}
          dataSource={versions.data}
          renderItem={(v) => (
            <List.Item>
              <div style={{ width: "100%" }}>
                <h3>
                  版本 {v.number}{" "}
                  <span className="muted">
                    {v.created_at ? formatDate(v.created_at) : ""}
                  </span>
                </h3>
                <p className="muted">{v.instructions}</p>
                <Tabs
                  items={[
                    {
                      key: "brief",
                      label: "精华版",
                      children: (
                        <pre className="json-block">{v.brief_markdown}</pre>
                      ),
                    },
                    {
                      key: "full",
                      label: "完整版",
                      children: (
                        <pre className="json-block">{v.full_markdown}</pre>
                      ),
                    },
                  ]}
                />
              </div>
            </List.Item>
          )}
        />
      </Drawer>
      <Modal
        title="用实际论文预览模板"
        open={!!previewTemplate}
        onCancel={() => setPreviewTemplate(null)}
        okText="选择解读方式"
        okButtonProps={{ disabled: !paperId }}
        onOk={() => {
          setSelectedTemplateId(previewTemplate!.id);
          setPreviewPaper(papers.data?.find((p) => p.id === paperId) ?? null);
          setPreviewTemplate(null);
        }}
      >
        <p className="muted">
          为「{previewTemplate?.name}
          」创建一次真实解读。会调用团队模型，结果保存在解读记录中。
        </p>
        <ErrorNotice error={papers.error} />
        <Select
          aria-label="选择预览论文"
          onSearch={setPaperSearch}
          filterOption={false}
          style={{ width: "100%" }}
          loading={papers.isPending}
          value={paperId}
          onChange={setPaperId}
          placeholder="选择团队论文"
          showSearch
          optionFilterProp="label"
          options={papers.data?.map((p) => ({ label: p.title, value: p.id }))}
        />
        {papers.data?.length === 0 && (
          <p className="muted">请先在论文库导入一篇论文。</p>
        )}
      </Modal>
      <ReadingDialog
        paper={previewPaper}
        templateId={selectedTemplateId}
        onClose={() => setPreviewPaper(null)}
      />
    </>
  );
}
