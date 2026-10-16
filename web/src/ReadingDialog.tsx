import { useEffect } from "react";
import { Alert, Form, Modal, Radio, Select, Space, Typography } from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api, post, workspacePath } from "./api";
import { useWorkspace } from "./context";
import { ErrorNotice } from "./common";
import PaperTitle from "./PaperTitle";
import type {
  ModelConnection,
  Paper,
  ReadingCreated,
  Skill,
  Template,
  WorkflowMode,
} from "./types";
export default function ReadingDialog({
  paper,
  onClose,
  templateId,
}: {
  paper: Paper | null;
  onClose: () => void;
  templateId?: string;
}) {
  const { workspace } = useWorkspace();
  const navigate = useNavigate();
  const path = (p: string) => workspacePath(workspace.id, p);
  const [form] = Form.useForm();
  const skills = useQuery({
    queryKey: ["skills"],
    queryFn: () => api<Skill[]>("/skills"),
    enabled: !!paper,
  });
  const models = useQuery({
    queryKey: ["workspace", workspace.id, "models"],
    queryFn: () => api<ModelConnection[]>(path("/models")),
    enabled: !!paper,
  });
  const templates = useQuery({
    queryKey: ["workspace", workspace.id, "templates"],
    queryFn: () => api<Template[]>(path("/templates")),
    enabled: !!paper,
  });
  const selectedSkill = Form.useWatch("skill_id", form);
  const workflowMode = Form.useWatch("workflow_mode", form) ?? "guided";
  const paperId = paper?.id;
  useEffect(() => {
    if (paperId) form.setFieldValue("workflow_mode", "guided");
  }, [paperId, form]);
  const selected = skills.data?.find((s) => s.id === selectedSkill);
  useEffect(() => {
    if (paperId)
      form.setFieldsValue({
        skill_id: skills.data?.find((s) => s.available)?.id,
        model_connection_id: models.data?.[0]?.id,
        template_id:
          templateId ??
          templates.data?.find((t) => t.is_builtin)?.id ??
          templates.data?.[0]?.id,
      });
  }, [paperId, skills.data, models.data, templates.data, form, templateId]);
  const create = useMutation({
    mutationFn: (values: {
      skill_id: string;
      model_connection_id: string;
      template_id: string;
      workflow_mode: WorkflowMode;
    }) =>
      post<ReadingCreated>(path("/readings"), {
        ...values,
        paper_id: paper!.id,
      }),
    onSuccess: (result) => {
      onClose();
      navigate(`/readings/${result.reading_id}`);
    },
  });
  return (
    <Modal
      title="开始论文解读"
      open={!!paper}
      onCancel={onClose}
      onOk={() => form.submit()}
      okText="开始解读"
      confirmLoading={create.isPending}
      destroyOnHidden
    >
      {paper && <PaperTitle paper={paper} />}
      <ErrorNotice
        error={skills.error || models.error || templates.error || create.error}
      />
      {models.data?.length === 0 && (
        <Alert
          className="block-gap"
          type="warning"
          showIcon
          message="团队尚未配置模型"
          description="请由团队管理员在「团队设置」中添加模型连接。"
        />
      )}
      <Form
        form={form}
        layout="vertical"
        onFinish={(values) => create.mutate(values)}
      >
        <Form.Item
          name="workflow_mode"
          label="解读流程"
          initialValue="guided"
          rules={[{ required: true }]}
        >
          <Radio.Group className="workflow-mode-options">
            <Radio value="guided">
              <strong>模板解读</strong>
              <span>Pi Agent 参考阅读指引，自主阅读、核查并按模板写作。</span>
            </Radio>
            <Radio value="native">
              <strong>完整原生 Skill</strong>
              <span>先完成原生笔记与检查，再生成模板解读。</span>
            </Radio>
          </Radio.Group>
        </Form.Item>
        <Form.Item
          name="skill_id"
          label="阅读 Skill"
          rules={[{ required: true, message: "请选择可用的阅读 Skill" }]}
        >
          <Select
            loading={skills.isPending}
            options={skills.data?.map((s) => ({
              value: s.id,
              label: s.available ? s.name : `${s.name}（暂不可用）`,
              disabled: !s.available,
            }))}
          />
        </Form.Item>
        {selected && (
          <Typography.Paragraph className="muted">
            {selected.description}
          </Typography.Paragraph>
        )}
        <Form.Item
          name="model_connection_id"
          label="解读模型"
          rules={[{ required: true, message: "请选择团队模型" }]}
        >
          <Select
            loading={models.isPending}
            options={models.data?.map((m) => ({
              value: m.id,
              label: `${m.name} · ${m.model}`,
            }))}
          />
        </Form.Item>
        <Form.Item
          name="template_id"
          label="呈现模板"
          rules={[{ required: true, message: "请选择解读模板" }]}
        >
          <Select
            loading={templates.isPending}
            options={templates.data?.map((t) => ({
              value: t.id,
              label: `${t.name}${t.is_builtin ? " · 内置" : ""}`,
            }))}
          />
        </Form.Item>
        <Space direction="vertical">
          <span className="muted">解读将在后台运行，关闭页面不影响任务。</span>
          <span className="muted">
            {workflowMode === "guided"
              ? "Agent 会保存进展、根据核查反馈调整工作，全部必要检查通过后发布解读。"
              : "完整原生流程需要通过对应 Skill 的笔记与检查要求。"}
          </span>
        </Space>
      </Form>
    </Modal>
  );
}
