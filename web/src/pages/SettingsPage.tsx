import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, workspacePath } from "../api";
import { useWorkspace } from "../context";
import { ErrorNotice, NoData, PageHeader } from "../common";
import type {
  Member,
  ModelConnection,
  ModelThinkingFormat,
  User,
} from "../types";
interface ModelForm {
  name: string;
  base_url: string;
  model: string;
  api_key?: string;
  protocol: string;
  thinking_format: ModelThinkingFormat;
  vision: boolean;
  context_window: number;
  max_tokens: number;
}
export default function SettingsPage() {
  const { workspace, user } = useWorkspace();
  const isAdmin = workspace.role === "admin";
  const path = (p: string) => workspacePath(workspace.id, p);
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [modelOpen, setModelOpen] = useState(false);
  const [editing, setEditing] = useState<ModelConnection | null>(null);
  const [memberOpen, setMemberOpen] = useState(false);
  const [modelForm] = Form.useForm<ModelForm>();
  const [memberForm] = Form.useForm();
  const modelProtocol = Form.useWatch("protocol", modelForm);
  const models = useQuery({
    queryKey: ["workspace", workspace.id, "models"],
    queryFn: () => api<ModelConnection[]>(path("/models")),
  });
  const members = useQuery({
    queryKey: ["workspace", workspace.id, "members"],
    queryFn: () => api<Member[]>(path("/members")),
    enabled: isAdmin,
  });
  const users = useQuery({
    queryKey: ["admin", "users"],
    queryFn: () => api<User[]>("/admin/users"),
    enabled: user.is_admin && memberOpen,
  });
  const save = useMutation({
    mutationFn: (values: ModelForm) => {
      const payload = {
        ...values,
        thinking_format:
          values.protocol === "openai-completions"
            ? (values.thinking_format ?? "default")
            : "default",
      };
      return editing
        ? api(path(`/models/${editing.id}`), {
            method: "PATCH",
            body: JSON.stringify(payload),
          })
        : post(path("/models"), payload);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["workspace", workspace.id, "models"],
      });
      setModelOpen(false);
      void message.success("模型连接已保存");
    },
  });
  const test = useMutation({
    mutationFn: (id: string) =>
      post<{ ok: boolean; message: string }>(path(`/models/${id}/test`)),
    onSuccess: (r) => {
      void (r.ok
        ? message.success(r.message || "模型连接可用")
        : message.error(r.message || "模型连接未通过检查"));
    },
  });
  const addMember = useMutation({
    mutationFn: (values: { user_id: string; role: string }) =>
      post(path("/members"), values),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["workspace", workspace.id, "members"],
      });
      setMemberOpen(false);
      memberForm.resetFields();
      void message.success("团队成员已更新");
    },
  });
  const removeMember = useMutation({
    mutationFn: (id: string) =>
      api(path(`/members/${id}`), { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["workspace", workspace.id, "members"],
      });
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    },
  });
  const openModel = (model: ModelConnection | null) => {
    setEditing(model);
    save.reset();
    modelForm.setFieldsValue(
      model
        ? {
            ...model,
            api_key: "",
            thinking_format:
              model.protocol === "openai-completions"
                ? (model.thinking_format ?? "default")
                : "default",
          }
        : {
            name: "",
            base_url: "",
            model: "",
            api_key: "",
            protocol: "openai-completions",
            thinking_format: "default",
            vision: false,
            context_window: 128000,
            max_tokens: 8192,
          },
    );
    setModelOpen(true);
  };
  return (
    <>
      <PageHeader
        title="团队设置"
        description={`${workspace.name} 的模型连接与成员权限。`}
      />
      <Tabs
        items={[
          {
            key: "models",
            label: "模型连接",
            children: (
              <>
                <ErrorNotice
                  error={models.error || save.error || test.error}
                  retry={models.refetch}
                />
                <div className="panel">
                  <div className="toolbar">
                    <span className="muted">
                      模型配置在当前团队内共享，API Key 仅保存在服务端。
                    </span>
                    <div className="toolbar-spacer" />
                    {isAdmin && (
                      <Button
                        type="primary"
                        icon={<PlusOutlined aria-hidden />}
                        onClick={() => openModel(null)}
                      >
                        添加模型
                      </Button>
                    )}
                  </div>
                  <Table<ModelConnection>
                    rowKey="id"
                    dataSource={models.data}
                    loading={models.isPending}
                    scroll={{ x: 650 }}
                    pagination={false}
                    locale={{
                      emptyText: (
                        <NoData
                          text={
                            isAdmin
                              ? "添加一个支持工具调用的模型，即可开始解读。"
                              : "请联系团队管理员添加模型连接。"
                          }
                        />
                      ),
                    }}
                    columns={[
                      {
                        title: "连接名称",
                        dataIndex: "name",
                        render: (name: string, m) => (
                          <>
                            <strong>{name}</strong>
                            <div className="table-sub">{m.base_url}</div>
                          </>
                        ),
                      },
                      { title: "模型", dataIndex: "model" },
                      {
                        title: "能力",
                        width: 165,
                        render: (_, m) => (
                          <Space wrap>
                            <Tag>{m.vision ? "支持图像" : "文本"}</Tag>
                            <Tag color={m.has_api_key ? "success" : "warning"}>
                              {m.has_api_key ? "已配置密钥" : "未配置密钥"}
                            </Tag>
                          </Space>
                        ),
                      },
                      ...(isAdmin
                        ? [
                            {
                              title: "操作",
                              width: 160,
                              render: (_: unknown, m: ModelConnection) => (
                                <Space>
                                  <Button
                                    size="small"
                                    onClick={() => openModel(m)}
                                  >
                                    编辑
                                  </Button>
                                  <Button
                                    size="small"
                                    loading={
                                      test.isPending && test.variables === m.id
                                    }
                                    onClick={() => test.mutate(m.id)}
                                  >
                                    测试连接
                                  </Button>
                                </Space>
                              ),
                            },
                          ]
                        : []),
                    ]}
                  />
                </div>
              </>
            ),
          },
          ...(isAdmin
            ? [
                {
                  key: "members",
                  label: "团队成员",
                  children: (
                    <>
                      <ErrorNotice
                        error={members.error || removeMember.error}
                      />
                      <div className="panel">
                        <div className="toolbar">
                          <span className="muted">
                            管理员可管理模型、成员和团队资源；成员可阅读、修订和创建模板。
                          </span>
                          <div className="toolbar-spacer" />
                          <Button
                            type="primary"
                            icon={<PlusOutlined aria-hidden />}
                            onClick={() => {
                              addMember.reset();
                              setMemberOpen(true);
                            }}
                          >
                            添加成员
                          </Button>
                        </div>
                        <Table<Member>
                          rowKey="user_id"
                          dataSource={members.data}
                          loading={members.isPending}
                          pagination={false}
                          columns={[
                            { title: "成员", dataIndex: "username" },
                            {
                              title: "角色",
                              dataIndex: "role",
                              render: (role: string) => (
                                <Tag
                                  color={role === "admin" ? "cyan" : "default"}
                                >
                                  {role === "admin" ? "团队管理员" : "成员"}
                                </Tag>
                              ),
                            },
                            {
                              title: "操作",
                              width: 160,
                              render: (_, m) => (
                                <Space>
                                  <Button
                                    size="small"
                                    onClick={() => {
                                      memberForm.setFieldsValue({
                                        user_id: m.user_id,
                                        role: m.role,
                                      });
                                      setMemberOpen(true);
                                    }}
                                  >
                                    修改角色
                                  </Button>
                                  <Popconfirm
                                    title={`移除成员 ${m.username}？`}
                                    description="该账号将无法访问团队的论文与解读。"
                                    onConfirm={() =>
                                      removeMember.mutate(m.user_id)
                                    }
                                  >
                                    <Button danger size="small">
                                      移除
                                    </Button>
                                  </Popconfirm>
                                </Space>
                              ),
                            },
                          ]}
                        />
                      </div>
                    </>
                  ),
                },
              ]
            : []),
        ]}
      />
      <Drawer
        title={editing ? "编辑模型连接" : "添加模型连接"}
        open={modelOpen}
        width={560}
        onClose={() => setModelOpen(false)}
        extra={
          <Button
            type="primary"
            loading={save.isPending}
            onClick={() => modelForm.submit()}
          >
            保存连接
          </Button>
        }
      >
        <ErrorNotice error={save.error} />
        <Form
          form={modelForm}
          layout="vertical"
          onFinish={(values) => save.mutate(values)}
        >
          <Form.Item
            label="连接名称"
            name="name"
            rules={[{ required: true, message: "请输入便于团队识别的名称" }]}
          >
            <Input placeholder="团队研究模型" />
          </Form.Item>
          <Form.Item
            label="API 协议"
            name="protocol"
            rules={[{ required: true }]}
          >
            <Select
              onChange={(protocol) => {
                if (protocol !== "openai-completions") {
                  modelForm.setFieldValue("thinking_format", "default");
                }
              }}
              options={[
                { value: "openai-completions", label: "OpenAI 兼容接口" },
                { value: "anthropic-messages", label: "Anthropic Messages" },
              ]}
            />
          </Form.Item>
          {modelProtocol === "openai-completions" && (
            <Form.Item
              label="推理模式适配"
              name="thinking_format"
              extra="本地 Qwen/vLLM 服务若仍输出大量思考内容，可选择关闭思考。"
            >
              <Select
                options={[
                  { value: "default", label: "标准接口" },
                  { value: "qwen-chat-template", label: "Qwen/vLLM：关闭思考" },
                ]}
              />
            </Form.Item>
          )}
          <Form.Item
            label="Base URL"
            name="base_url"
            rules={[
              { required: true, message: "请输入接口地址" },
              { type: "url", message: "请输入完整的 http 或 https 地址" },
            ]}
          >
            <Input
              placeholder="https://your-model-provider.example/v1"
              autoComplete="off"
            />
          </Form.Item>
          <Form.Item
            label="模型名称"
            name="model"
            rules={[{ required: true, message: "请输入服务商提供的模型名称" }]}
          >
            <Input placeholder="服务商提供的模型 ID" />
          </Form.Item>
          <Form.Item
            label={editing ? "API Key（留空保持原密钥）" : "API Key"}
            name="api_key"
            rules={
              editing ? [] : [{ required: true, message: "请输入 API Key" }]
            }
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <div className="form-grid">
            <Form.Item
              name="context_window"
              label="上下文窗口（Token）"
              rules={[{ required: true }]}
            >
              <InputNumber min={4096} max={2000000} />
            </Form.Item>
            <Form.Item
              name="max_tokens"
              label="单次最大输出（Token）"
              rules={[{ required: true }]}
            >
              <InputNumber min={256} max={100000} />
            </Form.Item>
          </div>
          <Form.Item name="vision" label="支持图像输入" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Alert
            type="info"
            showIcon
            message="请按照模型实际能力配置。"
            description="保存后可测试连接。解读需要模型支持工具调用；图表核查还需要图像输入能力。"
          />
        </Form>
      </Drawer>
      <Modal
        title="添加成员或修改角色"
        open={memberOpen}
        onCancel={() => {
          setMemberOpen(false);
          memberForm.resetFields();
        }}
        onOk={() => memberForm.submit()}
        okText="保存成员"
        confirmLoading={addMember.isPending}
      >
        <ErrorNotice error={addMember.error || users.error} />
        <Form
          form={memberForm}
          layout="vertical"
          initialValues={{ role: "member" }}
          onFinish={(values) => addMember.mutate(values)}
        >
          <Form.Item
            label="账号"
            name="user_id"
            rules={[{ required: true, message: "请选择账号或输入账号 ID" }]}
          >
            {user.is_admin ? (
              <Select
                loading={users.isPending}
                showSearch
                optionFilterProp="label"
                options={users.data?.map((u) => ({
                  label: u.username,
                  value: u.id,
                }))}
                placeholder="选择已有账号"
              />
            ) : (
              <Input placeholder="向平台管理员获取账号 ID" />
            )}
          </Form.Item>
          <Form.Item name="role" label="团队角色" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "member", label: "成员" },
                { value: "admin", label: "团队管理员" },
              ]}
            />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
