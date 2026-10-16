export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
const errorMessages: Record<string, string> = {
  "Please sign in": "请先登录。",
  "Session expired": "登录已过期，请重新登录。",
  "Account unavailable": "账号当前不可用，请联系平台管理员。",
  "Invalid username or password": "用户名或密码不正确，请重新输入。",
  "Username already exists": "用户名已被使用，请更换名称。",
  "Platform administrator required": "此操作需要平台管理员权限。",
  "Workspace administrator required": "此操作需要团队管理员权限。",
  "Workspace not found": "工作空间不存在，或你已不在该团队中。",
  "Resource not found": "内容不存在，或你没有访问权限。",
  "User not found": "未找到该账号，请核对账号 ID。",
  "Member not found": "该账号不是当前团队成员。",
  "Cannot remove the last workspace administrator":
    "团队至少需要一位管理员，请先为另一位成员授予管理员权限。",
  "Private model endpoints are disabled by deployment policy":
    "此部署尚未允许内网模型地址，请联系部署管理员配置。",
  "Model endpoint hostname cannot be resolved":
    "无法解析模型接口地址，请检查 Base URL。",
  "Output token limit cannot exceed the context window":
    "单次最大输出不能超过上下文窗口。",
  "Copy the built-in template to edit it": "请先复制内置模板，再修改副本。",
  "Only the owner or workspace administrator may edit this template":
    "只有模板创建者或团队管理员可以编辑此模板。",
  "Use an arXiv identifier or an arxiv.org URL":
    "请输入 arXiv 论文编号或 arxiv.org 链接。",
  "Invalid arXiv identifier": "arXiv 编号格式不正确，请检查后重试。",
  "File is not a PDF": "请选择有效的 PDF 文件。",
  "PDF exceeds the configured upload size":
    "PDF 超过了当前部署的上传大小限制。",
  "PDF is empty": "PDF 文件为空，请重新选择文件。",
  "PDF is encrypted or has no readable pages":
    "PDF 已加密或没有可读页面，请上传可正常打开的版本。",
  "PDF could not be opened": "无法打开此 PDF，请检查文件是否完整。",
  "Selected skill is unavailable": "所选阅读 Skill 暂不可用，请重新选择。",
  "This reading already has an active task":
    "该解读仍有任务正在运行，请完成或取消后重试。",
  "Revision is stale; reload the latest result":
    "解读已有新版本，请刷新后再提交修订。",
  "Revision base is stale; create a new revision task":
    "修订基础已更新，请在最新版本上重新提交要求。",
  "This reading has a result; revise it or create another reading":
    "已有可阅读的结果，请提交修订要求或重新创建解读。",
  "Only a stopped task can be continued": "任务仍在运行或已完成，请刷新后查看最新状态。",
  "Only a stopped answer can continue": "回答仍在运行或已完成，请刷新后查看最新状态。",
  "Only stopped assistant answers can continue": "只能继续尚未完成的助手回答。",
  "Only the latest answer can continue": "只能继续当前话题的最新一条回答。",
  "Original question is unavailable": "原始问题已不可用，请开启新话题。",
  "Answer revision no longer matches the conversation": "回答与对话所用的解读版本不一致，请刷新后开启新话题。",
};
export const base = "/api/v1";
export const workspacePath = (workspaceId: string, path: string) =>
  `/workspaces/${encodeURIComponent(workspaceId)}${path}`;
export const resourceUrl = (workspaceId: string, path: string) =>
  base + workspacePath(workspaceId, path);
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const response = await fetch(base + path, {
    ...options,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((e: { msg?: string }) => e.msg).join("；")
          : `请求失败（${response.status}），请重试。`;
    if (response.status === 401 && !path.startsWith("/auth/"))
      window.dispatchEvent(new Event("session-expired"));
    throw new ApiError(response.status, errorMessages[message] ?? message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
export function safeSourceUrl(
  value: string | undefined | null,
): string | undefined {
  if (!value) return undefined;
  try {
    const parsed = new URL(value, window.location.origin);
    return ["https:", "http:"].includes(parsed.protocol)
      ? parsed.href
      : undefined;
  } catch {
    return undefined;
  }
}
