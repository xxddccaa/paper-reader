import { lazy, Suspense, useEffect, useState } from "react";
import {
  BookOutlined,
  FileTextOutlined,
  ReadOutlined,
  SettingOutlined,
  TeamOutlined,
  LogoutOutlined,
  MenuOutlined,
} from "@ant-design/icons";
import {
  Alert,
  App,
  Avatar,
  Button,
  Form,
  Input,
  Layout,
  Menu,
  Result,
  Select,
  Space,
  Spin,
} from "antd";
import {
  Link,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, ApiError } from "./api";
import { ErrorNotice, LoadingBlock, messageOf } from "./common";
import { WorkspaceContext } from "./context";
import type { User, Workspace } from "./types";
const PapersPage = lazy(() => import("./pages/PapersPage"));
const ReadingsPage = lazy(() => import("./pages/ReadingsPage"));
const ReadingPage = lazy(() => import("./pages/ReadingPage"));
const TemplatesPage = lazy(() => import("./pages/TemplatesPage"));
const SettingsPage = lazy(() => import("./pages/SettingsPage"));
const AdminPage = lazy(() => import("./pages/AdminPage"));

function Login({ onLogin }: { onLogin: (user: User) => void }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>();
  return (
    <main className="login-layout">
      <section className="login-intro">
        <BookOutlined aria-hidden />
        <h1>让论文，成为可用的经验。</h1>
        <p>读懂贡献与方法。把结果放回实验条件中，找到值得借鉴的做法与边界。</p>
        <div>
          Paper Reader <span>论文解读工作台</span>
        </div>
      </section>
      <section className="login-form panel">
        <h2>登录工作空间</h2>
        <p className="muted">使用管理员为你创建的账号。</p>
        <ErrorNotice error={error} />
        <Form
          layout="vertical"
          onFinish={async (values: { username: string; password: string }) => {
            setLoading(true);
            setError(undefined);
            try {
              onLogin(await post<User>("/auth/login", values));
            } catch (e) {
              setError(e);
            } finally {
              setLoading(false);
            }
          }}
        >
          <Form.Item
            label="用户名"
            name="username"
            rules={[{ required: true, message: "请输入用户名" }]}
          >
            <Input autoComplete="username" autoFocus />
          </Form.Item>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: "请输入密码" }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Button block type="primary" htmlType="submit" loading={loading}>
            登录
          </Button>
        </Form>
      </section>
    </main>
  );
}
export default function Workbench() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const { message } = App.useApp();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => api<User>("/auth/me"),
    retry: false,
  });
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api<Workspace[]>("/workspaces"),
    enabled: !!me.data,
  });
  const [workspaceId, setWorkspaceId] = useState<string>(
    () => localStorage.getItem("paper-reader-workspace") || "",
  );
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => {
    const expired = () => {
      queryClient.clear();
      navigate("/");
    };
    window.addEventListener("session-expired", expired);
    return () => window.removeEventListener("session-expired", expired);
  }, [queryClient, navigate]);
  const workspace =
    workspaces.data?.find((w) => w.id === workspaceId) ?? workspaces.data?.[0];
  useEffect(() => {
    if (workspace) localStorage.setItem("paper-reader-workspace", workspace.id);
  }, [workspace]);
  if (me.isPending)
    return (
      <div className="full-loader">
        <Spin size="large" tip="正在验证登录…">
          <div />
        </Spin>
      </div>
    );
  if (!me.data)
    return me.error instanceof ApiError && me.error.status === 401 ? (
      <Login
        onLogin={(user) => {
          queryClient.setQueryData(["me"], user);
          void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
        }}
      />
    ) : (
      <div className="page-content">
        <ErrorNotice error={me.error} retry={me.refetch} />
      </div>
    );
  const navigation = [
    { key: "/papers", label: "论文库", icon: <BookOutlined aria-hidden /> },
    { key: "/readings", label: "解读记录", icon: <ReadOutlined aria-hidden /> },
    {
      key: "/templates",
      label: "解读模板",
      icon: <FileTextOutlined aria-hidden />,
    },
    {
      key: "/settings",
      label: "团队设置",
      icon: <SettingOutlined aria-hidden />,
    },
    ...(me.data.is_admin
      ? [
          {
            key: "/admin",
            label: "平台管理",
            icon: <TeamOutlined aria-hidden />,
          },
        ]
      : []),
  ];
  const current = navigation.find((item) =>
    location.pathname.startsWith(item.key),
  );
  return (
    <Layout className="workbench">
      <a className="skip-link" href="#main-content">
        跳转到主要内容
      </a>
      <Layout.Sider
        className="workbench-sidebar"
        width={208}
        collapsedWidth={0}
        breakpoint="lg"
        collapsed={collapsed}
        onCollapse={setCollapsed}
        trigger={null}
      >
        <Link className="brand" to="/papers">
          <BookOutlined aria-hidden />
          <span>Paper Reader</span>
        </Link>
        <div className="sidebar-caption">论文解读工作台</div>
        <Menu
          mode="inline"
          selectedKeys={current ? [current.key] : []}
          items={navigation.map((item) => ({
            ...item,
            label: (
              <Link
                to={item.key}
                onClick={() => {
                  if (window.innerWidth < 992) setCollapsed(true);
                }}
              >
                {item.label}
              </Link>
            ),
          }))}
        />
        <div className="sidebar-note">
          研究贡献 · 核心方法
          <br />
          有依据的经验与边界
        </div>
      </Layout.Sider>
      <Layout className="workbench-main">
        <Layout.Header className="topbar">
          <Space>
            <Button
              className="menu-toggle"
              aria-label="展开或收起导航"
              type="text"
              icon={<MenuOutlined aria-hidden />}
              onClick={() => setCollapsed(!collapsed)}
            />
            <span className="topbar-label">团队</span>
            <Select
              aria-label="工作空间"
              className="workspace-select"
              value={workspace?.id}
              placeholder="暂无工作空间"
              options={workspaces.data?.map((w) => ({
                label: w.name,
                value: w.id,
              }))}
              onChange={(id) => {
                setWorkspaceId(id);
                queryClient.removeQueries({
                  predicate: (q) => q.queryKey[0] === "workspace",
                });
                navigate("/papers");
              }}
            />
          </Space>
          <Space>
            <Avatar
              size={26}
              style={{ background: "#e5eeec", color: "#156f65" }}
            >
              {me.data.username.slice(0, 1).toUpperCase()}
            </Avatar>
            <span className="username">{me.data.username}</span>
            <Button
              type="text"
              aria-label="退出登录"
              icon={<LogoutOutlined aria-hidden />}
              onClick={async () => {
                try {
                  await post("/auth/logout");
                  queryClient.clear();
                  localStorage.removeItem("paper-reader-workspace");
                  navigate("/");
                } catch (e) {
                  void message.error(messageOf(e));
                }
              }}
            />
          </Space>
        </Layout.Header>
        <Layout.Content className="page-content" id="main-content">
          <ErrorNotice error={workspaces.error} retry={workspaces.refetch} />
          {workspaces.isPending ? (
            <LoadingBlock />
          ) : (
            <Suspense fallback={<LoadingBlock />}>
              {workspace ? (
                <WorkspaceContext.Provider
                  value={{
                    user: me.data,
                    workspace,
                    workspaces: workspaces.data ?? [],
                  }}
                >
                  <div key={workspace.id}>
                    <Routes>
                      <Route
                        index
                        element={<Navigate to="/papers" replace />}
                      />
                      <Route path="/papers" element={<PapersPage />} />
                      <Route path="/readings" element={<ReadingsPage />} />
                      <Route
                        path="/readings/:readingId"
                        element={<ReadingPage />}
                      />
                      <Route path="/templates" element={<TemplatesPage />} />
                      <Route path="/settings" element={<SettingsPage />} />
                      <Route
                        path="/admin"
                        element={
                          me.data.is_admin ? (
                            <AdminPage />
                          ) : (
                            <Result status="403" title="需要平台管理员权限" />
                          )
                        }
                      />
                      <Route
                        path="*"
                        element={
                          <Result
                            status="404"
                            title="页面不存在"
                            extra={<Link to="/papers">返回论文库</Link>}
                          />
                        }
                      />
                    </Routes>
                  </div>
                </WorkspaceContext.Provider>
              ) : me.data.is_admin ? (
                <>
                  <Alert
                    className="block-gap"
                    type="info"
                    message="先创建工作空间，再为团队添加成员。"
                  />
                  <AdminPage />
                </>
              ) : (
                <Result
                  title="尚未加入工作空间"
                  subTitle="请联系管理员将你的账号加入团队。"
                  extra={
                    <Button onClick={() => void workspaces.refetch()}>
                      重新加载
                    </Button>
                  }
                />
              )}
            </Suspense>
          )}
        </Layout.Content>
      </Layout>
    </Layout>
  );
}
