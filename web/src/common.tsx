import type { ReactNode } from "react";
import { Alert, Button, Empty, Skeleton, Space, Tag } from "antd";
import { ReloadOutlined } from "@ant-design/icons";

export const messageOf = (error: unknown) =>
  error instanceof Error ? error.message : "操作失败，请重试。";
export const formatNumber = (value: number | null | undefined) =>
  value == null ? "暂无" : value.toLocaleString("zh-CN");
export const formatDate = (value: string) =>
  new Date(value).toLocaleString("zh-CN", { hour12: false });

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <Space wrap>{actions}</Space>
    </div>
  );
}

export function ErrorNotice({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => unknown;
}) {
  return error ? (
    <Alert
      className="block-gap"
      type="error"
      showIcon
      message={messageOf(error)}
      action={
        retry ? (
          <Button size="small" onClick={() => void retry()}>
            重试
          </Button>
        ) : undefined
      }
    />
  ) : null;
}

export function RefreshButton({
  refresh,
  loading,
}: {
  refresh: () => unknown;
  loading?: boolean;
}) {
  return (
    <Button
      icon={<ReloadOutlined aria-hidden />}
      loading={loading}
      onClick={() => void refresh()}
    >
      刷新
    </Button>
  );
}

export function NoData({ text, action }: { text: string; action?: ReactNode }) {
  return (
    <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={text}>
      {action}
    </Empty>
  );
}

export function LoadingBlock() {
  return (
    <div className="panel loading-panel">
      <Skeleton active paragraph={{ rows: 5 }} />
    </div>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="json-block">
      {value == null ? "暂无内容" : JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function StatusTag({ value }: { value: string }) {
  const values: Record<string, [string, string]> = {
    active: ["启用", "success"],
    paused: ["暂停", "warning"],
    queued: ["等待处理", "default"],
    running: ["正在解读", "processing"],
    waiting: ["待继续", "warning"],
    succeeded: ["解读完成", "success"],
    partial: ["部分完成", "warning"],
    cancelled: ["已取消", "default"],
    failed: ["失败", "error"],
  };
  const [label, color] = values[value] ?? [value || "未知", "default"];
  return <Tag color={color}>{label}</Tag>;
}
