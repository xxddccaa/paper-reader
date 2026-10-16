import { useState } from "react";
import {
  App,
  Button,
  Form,
  Input,
  Modal,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "../api";
import { ErrorNotice, PageHeader } from "../common";
import type { User, Workspace } from "../types";
import NetworkSettings from "../NetworkSettings";
export default function AdminPage() {
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [userOpen, setUserOpen] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [userForm] = Form.useForm();
  const [workspaceForm] = Form.useForm();
  const users = useQuery({
    queryKey: ["admin", "users"],
    queryFn: () => api<User[]>("/admin/users"),
  });
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api<Workspace[]>("/workspaces"),
  });
  const createUser = useMutation({
    mutationFn: (values: {
      username: string;
      password: string;
      is_admin: boolean;
    }) => post<User>("/admin/users", values),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["admin", "users"] });
      setUserOpen(false);
      userForm.resetFields();
      void message.success("账号已创建，可以将其加入工作空间。");
    },
  });
  const createWorkspace = useMutation({
    mutationFn: (values: { name: string }) =>
      post<Workspace>("/workspaces", values),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      setWorkspaceOpen(false);
      workspaceForm.resetFields();
      void message.success("工作空间已创建");
    },
  });
  return (
    <>
      <PageHeader
        title="平台管理"
        description="管理平台账号、工作空间和论文下载网络。"
      />
      <Tabs
        defaultActiveKey="users"
        items={[
          {
            key: "network",
            label: "网络代理",
            children: <NetworkSettings />,
          },
          {
            key: "users",
            label: "平台账号",
            children: (
              <>
                <ErrorNotice error={users.error} />
                <div className="panel">
                  <div className="toolbar">
                    <span className="muted">
                      账号创建后可加入多个工作空间。
                    </span>
                    <div className="toolbar-spacer" />
                    <Button
                      type="primary"
                      icon={<PlusOutlined aria-hidden />}
                      onClick={() => {
                        createUser.reset();
                        setUserOpen(true);
                      }}
                    >
                      创建账号
                    </Button>
                  </div>
                  <Table<User>
                    rowKey="id"
                    dataSource={users.data}
                    loading={users.isPending}
                    scroll={{ x: 530 }}
                    columns={[
                      { title: "用户名", dataIndex: "username" },
                      {
                        title: "平台角色",
                        dataIndex: "is_admin",
                        render: (admin: boolean) => (
                          <Tag color={admin ? "cyan" : "default"}>
                            {admin ? "平台管理员" : "普通账号"}
                          </Tag>
                        ),
                      },
                      {
                        title: "账号 ID",
                        dataIndex: "id",
                        render: (id: string) => (
                          <Typography.Text className="mono" copyable>
                            {id}
                          </Typography.Text>
                        ),
                      },
                    ]}
                  />
                </div>
              </>
            ),
          },
          {
            key: "workspaces",
            label: "工作空间",
            children: (
              <>
                <ErrorNotice error={workspaces.error} />
                <div className="panel">
                  <div className="toolbar">
                    <span className="muted">
                      空间内共享论文和结果，空间之间独立。
                    </span>
                    <div className="toolbar-spacer" />
                    <Button
                      type="primary"
                      icon={<PlusOutlined aria-hidden />}
                      onClick={() => {
                        createWorkspace.reset();
                        setWorkspaceOpen(true);
                      }}
                    >
                      创建工作空间
                    </Button>
                  </div>
                  <Table<Workspace>
                    rowKey="id"
                    dataSource={workspaces.data}
                    loading={workspaces.isPending}
                    columns={[
                      { title: "名称", dataIndex: "name" },
                      {
                        title: "成员管理",
                        render: () => (
                          <span className="muted">
                            在顶部切换到该空间后，进入团队设置。
                          </span>
                        ),
                      },
                    ]}
                  />
                </div>
              </>
            ),
          },
        ]}
      />
      <Modal
        title="创建平台账号"
        open={userOpen}
        onCancel={() => setUserOpen(false)}
        onOk={() => userForm.submit()}
        okText="创建账号"
        confirmLoading={createUser.isPending}
      >
        <ErrorNotice error={createUser.error} />
        <Form
          form={userForm}
          layout="vertical"
          initialValues={{ is_admin: false }}
          onFinish={(values) => createUser.mutate(values)}
        >
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true, whitespace: true, message: "请输入用户名" },
            ]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[
              { required: true, message: "请输入初始密码" },
              { min: 10, message: "密码至少 10 个字符" },
            ]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item
            name="is_admin"
            label="授予平台管理员权限"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title="创建工作空间"
        open={workspaceOpen}
        onCancel={() => setWorkspaceOpen(false)}
        onOk={() => workspaceForm.submit()}
        okText="创建空间"
        confirmLoading={createWorkspace.isPending}
      >
        <ErrorNotice error={createWorkspace.error} />
        <Form
          form={workspaceForm}
          layout="vertical"
          onFinish={(values) => createWorkspace.mutate(values)}
        >
          <Form.Item
            label="工作空间名称"
            name="name"
            rules={[
              {
                required: true,
                whitespace: true,
                message: "请输入工作空间名称",
              },
            ]}
          >
            <Input placeholder="例如：模型研究组" />
          </Form.Item>
          <Space>
            <span className="muted">创建后可在团队设置中添加成员。</span>
          </Space>
        </Form>
      </Modal>
    </>
  );
}
