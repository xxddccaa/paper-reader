import { useEffect } from "react";
import { Alert, App, Button, Form, Input, InputNumber, Space, Switch } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post } from "./api";
import { ErrorNotice, LoadingBlock } from "./common";
import type { components } from "./generated-api";

type NetworkConfig = components["schemas"]["SourceNetwork"];

export default function NetworkSettings() {
  const [form] = Form.useForm<NetworkConfig>();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["admin", "network"],
    queryFn: () => api<NetworkConfig>("/admin/network"),
  });
  useEffect(() => {
    if (settings.data) form.setFieldsValue(settings.data);
  }, [settings.data, form]);
  const save = useMutation({
    mutationFn: (values: NetworkConfig) => api<NetworkConfig>("/admin/network", { method: "PUT", body: JSON.stringify(values) }),
    onSuccess: (values) => {
      queryClient.setQueryData(["admin", "network"], values);
      void message.success("网络设置已保存，下一次论文下载立即生效。");
    },
  });
  const test = useMutation({
    mutationFn: (values: NetworkConfig) => post<{ ok: boolean; message: string }>("/admin/network/test", values),
  });
  if (settings.isPending) return <LoadingBlock />;
  return (
    <div className="panel" style={{ padding: 24 }}>
      <ErrorNotice error={settings.error || save.error || test.error} retry={settings.refetch} />
      <p className="muted">为所有团队的论文原文、图片、检索和标题获取设置网络代理。模型连接使用各自配置的地址。</p>
      <Form form={form} layout="vertical" initialValues={{ proxy_enabled: false, proxy_url: "", timeout_seconds: 120 }}
        onFinish={(values) => save.mutate(values)} onValuesChange={() => test.reset()} style={{ maxWidth: 580 }}>
        <Form.Item label="启用论文下载代理" name="proxy_enabled" valuePropName="checked">
          <Switch />
        </Form.Item>
        <Form.Item label="HTTP 代理地址" name="proxy_url"
          extra="填写服务器可访问的代理。127.0.0.1 指运行 Paper Reader 的服务器；HTTP 代理也可用于 HTTPS 论文链接。"
          rules={[({ getFieldValue }) => ({
            validator(_, value: string) {
              if (!value?.trim() && !getFieldValue("proxy_enabled")) return Promise.resolve();
              try {
                const url = new URL(value);
                if (url.protocol === "http:" && !url.username && !url.password && url.pathname === "/" && !url.search && !url.hash)
                  return Promise.resolve();
              } catch { /* Show the same actionable format error. */ }
              return Promise.reject(new Error("请输入 HTTP 代理地址，例如 http://127.0.0.1:10826，不包含账号密码或路径"));
            },
          })]}>
          <Input placeholder="http://127.0.0.1:10826" autoComplete="off" />
        </Form.Item>
        <Form.Item label="原文下载超时（秒）" name="timeout_seconds" rules={[{ required: true, message: "请输入超时时间" }]}>
          <InputNumber min={10} max={300} precision={0} />
        </Form.Item>
        {test.data && <Alert className="block-gap" showIcon type={test.data.ok ? "success" : "error"} message={test.data.message} />}
        <Space wrap>
          <Button type="primary" htmlType="submit" loading={save.isPending}>保存网络设置</Button>
          <Button loading={test.isPending} onClick={() => { void form.validateFields().then((values) => test.mutate(values)).catch(() => {}); }}>测试 arXiv 连接</Button>
        </Space>
        <p className="muted" style={{ marginTop: 16 }}>保存后无需重启。此前因下载失败的任务，可在解读页面点击重试。</p>
      </Form>
    </div>
  );
}
