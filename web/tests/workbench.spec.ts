import { test, expect, type Page } from "@playwright/test";

// Explicit HTTP fixtures: these tests validate UI behavior, never model quality.
const user = { id: "u1", username: "研究员", is_admin: true };
const workspaces = [
  { id: "w1", name: "模型研究组", role: "admin" },
  { id: "w2", name: "评测研究组", role: "admin" },
];
const paper = {
  id: "p1",
  workspace_id: "w1",
  title: "Model merging under domain shift",
  source_type: "arxiv",
  source_url: "https://arxiv.org/abs/2401.12345",
  sha256: null,
  metadata_json: { source_mime_type: "application/pdf", title_status: "ready" },
  created_at: "2026-10-12T09:00:00Z",
};
const template = {
  id: "t1",
  name: "研究贡献与可迁移经验",
  is_builtin: true,
  owner_id: null,
  current_version: {
    id: "tv1",
    number: 1,
    brief_markdown: "# 一句话贡献\n\n# 核心方法\n\n# 经验与边界",
    full_markdown: "# 研究问题\n\n# 方法与实验\n\n# 证据与限制",
    instructions: "少写背景，保留实验条件。经验必须有证据和适用边界。",
    created_at: paper.created_at,
  },
};
const model = {
  id: "m1",
  name: "团队研究模型",
  base_url: "https://model.example/v1",
  model: "fixture-model",
  protocol: "openai-completions",
  thinking_format: "default",
  vision: true,
  context_window: 128000,
  max_tokens: 8192,
  has_api_key: true,
};
const evidence = {
  id: "E1",
  source_id: "main",
  source_title: paper.title,
  quote:
    "Merging improves accuracy by 2.1 percentage points on the in-domain evaluation.",
  section: "4.2 Results",
  page: 5,
  kind: "paper",
  verified: true,
};
const revision = {
  id: "v1",
  number: 1,
  parent_revision_id: null,
  instruction: "",
  brief_markdown:
    "# 一句话贡献\n\n比较不同领域训练后模型合并的收益与边界。\n\n## 关键成果\n\n同领域评测准确率提升 **2.1 个百分点**。[E1](evidence:E1)\n\n## 核心方法\n\n采用 $\\theta = \\sum_i w_i \\theta_i$ 合并参数。\n\n## 经验与边界\n\n需先确认任务与基础模型一致，再评估合并权重。不能据此断言跨领域总能收益。",
  full_markdown:
    "# 核心方法\n\n| 输入 | 操作 | 输出 |\n| --- | --- | --- |\n| 同基座模型 | 加权合并 | 新模型 |\n\n## 实验条件\n\n同领域评测结果提升 2.1 个百分点。[E1]\n\n<script>window.__unsafe=1</script>\n\n[危险链接](javascript:alert(1))",
  evidence_index: [evidence],
  verification: { status: "passed", external_coverage: "limited" },
  provenance: { skill: "deeppapernote", model: "fixture-model" },
  artifacts: [
    { name: "native-note.md", path: "note.md", mime_type: "text/markdown" },
  ],
  created_at: paper.created_at,
};
const job = {
  id: "j1",
  workspace_id: "w1",
  reading_id: "r1",
  kind: "read",
  status: "succeeded",
  stage: "completed",
  progress: 100,
  attempt: 1,
  cancel_requested: false,
  error: null,
  usage: { input_tokens: 500 },
  created_at: paper.created_at,
  updated_at: paper.created_at,
};
const reading = {
  workflow_mode: "guided",
  id: "r1",
  paper_id: "p1",
  paper_title: paper.title,
  skill_id: "deeppapernote",
  current_revision_id: "v1",
  created_at: paper.created_at,
  latest_job: job,
};
async function fixtures(
  page: Page,
  options: {
    loggedOut?: boolean;
    running?: boolean;
    thinkingFormat?: "default" | "qwen-chat-template";
  } = {},
) {
  let loggedIn = !options.loggedOut;
  let workflowMode = reading.workflow_mode;
  let currentModel = {
    ...structuredClone(model),
    thinking_format: options.thinkingFormat ?? "default",
  };
  let templates = [structuredClone(template)];
  let latestJob = {
    ...job,
    ...(options.running
      ? { status: "running", stage: "reading", progress: 35 }
      : {}),
  };
  let revisions = [structuredClone(revision)];
  const requests: { method: string; path: string; body: any }[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname.replace("/api/v1", "");
    const method = request.method();
    let body: any;
    try {
      body = request.postDataJSON();
    } catch {
      body = request.postData();
    }
    requests.push({ method, path: pathname, body });
    let result: any;
    let status = 200;
    if (pathname === "/auth/me") {
      result = loggedIn ? user : { detail: "请先登录" };
      status = loggedIn ? 200 : 401;
    } else if (pathname === "/auth/login") {
      loggedIn = true;
      result = user;
    } else if (pathname === "/auth/logout") {
      loggedIn = false;
      result = { ok: true };
    } else if (pathname === "/workspaces") {
      result =
        method === "POST"
          ? { id: "w3", name: body.name, role: "admin" }
          : workspaces;
    } else if (pathname === "/admin/users") {
      result =
        method === "POST"
          ? { id: "u2", username: body.username, is_admin: body.is_admin }
          : [user];
    } else if (pathname === "/skills")
      result = [
        {
          id: "deeppapernote",
          name: "DeepPaperNote",
          description: "阅读全文、整理证据、检查并修订原生笔记。",
          version: "fixture",
          available: true,
        },
        {
          id: "ljg-paper",
          name: "ljg-paper",
          description: "从贡献与机制理解论文。",
          version: "fixture",
          available: true,
        },
      ];
    else if (pathname === "/workspaces/w2/papers") result = [];
    else if (
      pathname.endsWith("/papers/upload") ||
      (pathname.endsWith("/papers") && method === "POST")
    )
      result = { ...paper, title: body?.title || paper.title };
    else if (pathname.endsWith("/papers")) result = [paper];
    else if (pathname.endsWith("/papers/p1")) result = paper;
    else if (pathname.endsWith("/models/m1/test"))
      result = { ok: true, message: "连接与工具调用可用" };
    else if (pathname.endsWith("/models")) {
      if (method === "GET") result = [currentModel];
      else {
        const { api_key: _key, ...safeBody } = body;
        currentModel = { ...currentModel, ...safeBody };
        result = currentModel;
      }
    } else if (pathname.endsWith("/models/m1")) {
      const { api_key: _key, ...safeBody } = body;
      currentModel = { ...currentModel, ...safeBody };
      result = currentModel;
    } else if (pathname.endsWith("/members"))
      result =
        method === "GET"
          ? [{ user_id: "u1", username: user.username, role: "admin" }]
          : body;
    else if (pathname.endsWith("/templates/t1/versions"))
      result = [template.current_version];
    else if (pathname.endsWith("/templates")) {
      if (method === "POST") {
        const created = {
          ...template,
          id: "t2",
          name: body.name,
          is_builtin: false,
          owner_id: "u1",
          current_version: { ...template.current_version, ...body, id: "tv2" },
        };
        templates.push(created);
        result = created;
      } else result = templates;
    } else if (pathname.endsWith("/readings/r1/revisions")) {
      latestJob = {
        ...job,
        id: "j2",
        kind: "revise",
        status: "queued",
        stage: "queued",
        progress: 0,
        created_at: "2026-10-12T10:00:00Z",
      };
      result = { job_id: "j2" };
    } else if (pathname.endsWith("/jobs/j1/cancel")) {
      latestJob = { ...latestJob, status: "cancelled", stage: "cancelled" };
      result = latestJob;
    } else if (pathname.endsWith("/jobs/j1/retry")) {
      latestJob = { ...latestJob, status: "queued", stage: "queued" };
      result = { job_id: "j2" };
    } else if (method === "GET" && pathname.endsWith("/readings/r1/conversations"))
      result = [];
    else if (pathname.endsWith("/readings/r1"))
      result = {
        ...reading,
        workflow_mode: workflowMode,
        revisions,
        jobs: [latestJob],
      };
    else if (pathname.endsWith("/readings")) {
      if (method === "POST") {
        workflowMode = body.workflow_mode ?? "guided";
        result = { reading_id: "r1", job_id: "j1" };
      } else
        result = [
          { ...reading, workflow_mode: workflowMode, latest_job: latestJob },
        ];
    } else {
      status = 404;
      result = { detail: "未定义的测试接口 " + pathname };
    }
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(result),
    });
  });
  return requests;
}

test("login, import arXiv, select reading method, evidence and revision", async ({
  page,
}, info) => {
  const requests = await fixtures(page, { loggedOut: true });
  await page.goto("/");
  await page.getByLabel("用户名", { exact: true }).fill("研究员");
  await page.getByLabel("密码", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "论文库", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "导入论文", exact: true }).click();
  await page.getByLabel("论文链接或编号").fill("2401.12345");
  await page.getByRole("button", { name: "导入并选择解读方式" }).click();
  await expect(
    page.getByRole("dialog", { name: "开始论文解读" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "开始解读", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: paper.title, exact: true }),
  ).toBeVisible();
  await expect(page.locator(".katex").first()).toBeVisible();
  expect(
    requests.find((r) => r.path.endsWith("/readings") && r.method === "POST")
      ?.body,
  ).toEqual({
    paper_id: "p1",
    skill_id: "deeppapernote",
    model_connection_id: "m1",
    template_id: "t1",
    workflow_mode: "guided",
  });
  await page.getByRole("button", { name: "查看证据 E1" }).click();
  await expect(page.locator(".source-quote")).toContainText(
    "2.1 percentage points",
  );
  await expect(
    page.getByRole("link", { name: "定位原文 PDF" }),
  ).toHaveAttribute("href", /\/papers\/p1\/file#page=5$/);
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("tab", { name: "完整解读" }).click();
  await expect(page.locator(".markdown table")).toBeVisible();
  expect(await page.evaluate(() => (window as any).__unsafe)).toBeUndefined();
  await expect(page.locator('.markdown a[href^="javascript:"]')).toHaveCount(0);
  await page.getByRole("tab", { name: "精华版", exact: true }).click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.locator(".workbench-sidebar")).toHaveCSS("width", "208px");
  await expect
    .poll(
      async () =>
        (await page.locator(".workbench-sidebar").boundingBox())?.width,
    )
    .toBe(208);
  await expect(page.locator(".ant-message-notice")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("reading-desktop.png"),
    animations: "disabled",
    fullPage: true,
  });
  await page.getByLabel("修订要求").fill("方法讲得太抽象，请补充输入与输出。");
  await page.getByRole("button", { name: "提交修订" }).click();
  await expect(page.getByText("等待处理", { exact: true })).toBeVisible();
  expect(
    requests.find((r) => r.path.endsWith("/revisions") && r.method === "POST")
      ?.body.base_revision_id,
  ).toBe("v1");
  await expect(page.locator(".markdown")).toContainText("核心方法");
});

test("workspace switching removes previous tenant data", async ({ page }) => {
  const requests = await fixtures(page);
  await page.goto("/papers");
  await expect(page.getByText(paper.title, { exact: true })).toBeVisible();
  await page.getByRole("combobox", { name: "工作空间" }).press("ArrowDown");
  await page.getByTitle("评测研究组", { exact: true }).click();
  await expect(
    page.getByText("论文库还是空的。导入 PDF 或 arXiv 论文，开始第一次解读。"),
  ).toBeVisible();
  await expect(page.getByText(paper.title, { exact: true })).toHaveCount(0);
  expect(requests.some((r) => r.path === "/workspaces/w2/papers")).toBe(true);
});

test("copy arbitrary markdown template and preview with an actual paper", async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto("/templates");
  await page.getByRole("button", { name: "复制模板" }).click();
  await page.getByLabel("模板名称").fill("工程方法表格");
  await page
    .getByLabel("精华版 Markdown")
    .fill("# 三个判断\n\n用表格比较贡献、代价、边界。");
  await page.getByRole("tab", { name: "完整版", exact: true }).click();
  await page
    .getByLabel("完整版 Markdown")
    .fill("# 输入、操作、输出\n\n保留所有关键实验设置。");
  await page.getByRole("button", { name: "保存模板" }).click();
  await expect(
    page.getByRole("heading", { name: "工程方法表格", exact: true }),
  ).toBeVisible();
  const created = requests.find(
    (r) => r.path.endsWith("/templates") && r.method === "POST",
  );
  expect(created?.body.brief_markdown).toContain("三个判断");
  const card = page.locator(".template-panel").filter({
    has: page.getByRole("heading", { name: "工程方法表格", exact: true }),
  });
  await card.getByRole("button", { name: "用论文预览" }).click();
  await page.getByRole("combobox", { name: "选择预览论文" }).click();
  await page.getByTitle(paper.title, { exact: true }).click();
  await page.getByRole("button", { name: "选择解读方式", exact: true }).click();
  await page.getByRole("button", { name: "开始解读", exact: true }).click();
  await expect(page.getByRole("heading", { name: paper.title })).toBeVisible();
  expect(
    requests.find((r) => r.path.endsWith("/readings") && r.method === "POST")
      ?.body.template_id,
  ).toBe("t2");
});

test("cancel running task preserves existing result", async ({ page }) => {
  await fixtures(page, { running: true });
  await page.goto("/readings/r1");
  await expect(page.getByText("正在解读", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "取消任务", exact: true }).click();
  await page.getByRole("button", { name: "确定", exact: true }).click();
  await expect(page.getByText("已取消", { exact: true })).toBeVisible();
  await expect(page.locator(".markdown")).toContainText("2.1 个百分点");
});

test("model editing preserves blank key and tests connection", async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto("/settings");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByLabel("连接名称").fill("新研究模型");
  await page.getByRole("button", { name: "保存连接" }).click();
  expect(
    requests.find((r) => r.path.endsWith("/models/m1") && r.method === "PATCH")
      ?.body.api_key,
  ).toBe("");
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByText("连接与工具调用可用")).toBeVisible();
});

test("narrow reading layout and navigation remain usable", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixtures(page);
  await page.goto("/readings/r1");
  await expect(page.getByRole("heading", { name: paper.title })).toBeVisible();
  await expect(page.locator(".markdown")).toContainText("关键成果");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("reading-mobile.png"),
    animations: "disabled",
    fullPage: true,
  });
  await page.getByRole("button", { name: "展开或收起导航" }).click();
  await page.getByRole("link", { name: "论文库", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "论文库", exact: true }),
  ).toBeVisible();
});

test("unauthorized resource errors are visible without another workspace data", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/missing", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ detail: "解读不存在或无权访问" }),
    }),
  );
  await page.goto("/readings/missing");
  await expect(page.getByRole("alert")).toContainText("解读不存在或无权访问");
  await expect(page.locator(".markdown")).toHaveCount(0);
});

test("logout revokes local session and returns to login", async ({ page }) => {
  await fixtures(page);
  await page.goto("/papers");
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(
    page.getByRole("heading", { name: "登录工作空间" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "论文库", exact: true }),
  ).toHaveCount(0);
});

test("PDF upload sends the file then offers reading settings", async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto("/papers");
  await page.getByRole("button", { name: "导入论文", exact: true }).click();
  await page.getByRole("tab", { name: "上传 PDF" }).click();
  await page.locator("input[type=file]").setInputFiles({
    name: "test-paper.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.7\nfixture only\n%%EOF"),
  });
  await expect(
    page.getByRole("dialog", { name: "开始论文解读" }),
  ).toBeVisible();
  expect(
    requests.some(
      (r) => r.path.endsWith("/papers/upload") && r.method === "POST",
    ),
  ).toBe(true);
});

test("paper titles update automatically with a smaller Chinese subtitle without resetting reading choices", async ({ page }) => {
  await fixtures(page);
  let ready = false;
  await page.route("**/api/v1/workspaces/w1/papers?*", async (route) => {
    await route.fulfill({ json: [{ ...paper,
      title: ready ? paper.title : "arXiv:2401.12345",
      metadata_json: ready
        ? { title_status: "ready", title_zh: "领域偏移下的模型合并" }
        : { title_status: "processing" },
    }] });
  });
  await page.goto("/papers");
  await expect(page.getByRole("status")).toContainText("正在获取论文原题");
  await page.getByRole("button", { name: "解读论文", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "开始论文解读" });
  await dialog.getByRole("radio", { name: /完整原生 Skill/ }).check();
  ready = true;
  await expect(page.locator(".ant-table .paper-title-translation")).toHaveText("领域偏移下的模型合并", { timeout: 10000 });
  await expect(dialog.getByRole("radio", { name: /原生 Skill/ })).toBeChecked();
  await expect(dialog.locator(".paper-title-translation")).toHaveText("领域偏移下的模型合并");
  const fontSizes = await page.locator(".ant-table .paper-title-translation").evaluate((el) => ({
    subtitle: parseFloat(getComputedStyle(el).fontSize),
    original: parseFloat(getComputedStyle(el.previousElementSibling!).fontSize),
  }));
  expect(fontSizes.subtitle).toBeLessThan(fontSizes.original);
  await expect(page.locator('link[rel="icon"]')).toHaveAttribute("href", "/favicon.svg");
  const icon = await page.request.get("/favicon.svg");
  expect(icon.headers()["content-type"]).toContain("image/svg+xml");
});

test("failed title extraction can be retried from the paper row", async ({ page }) => {
  await fixtures(page);
  let queued = false;
  await page.route("**/api/v1/workspaces/w1/papers?*", async (route) => {
    await route.fulfill({ json: [{ ...paper, metadata_json: queued
      ? { title_status: "pending" }
      : { title_status: "failed", title_error: "自动获取标题或中文译名失败" },
    }] });
  });
  await page.route("**/api/v1/workspaces/w1/papers/p1/title", async (route) => {
    expect(route.request().method()).toBe("POST");
    queued = true;
    await route.fulfill({ status: 202, json: { ...paper, metadata_json: { title_status: "pending" } } });
  });
  await page.goto("/papers");
  await expect(page.getByRole("status")).toContainText("自动获取标题或中文译名失败");
  await page.getByRole("button", { name: "重试获取标题" }).click();
  await expect(page.getByRole("status")).toContainText("正在获取论文原题");
  await expect(page.getByRole("button", { name: "重试获取标题" })).toHaveCount(0);
});

test("platform network proxy can be tested before saving and persists after reload", async ({ page }) => {
  await fixtures(page);
  let saved = { proxy_enabled: false, proxy_url: "", timeout_seconds: 120 };
  const probes: typeof saved[] = [];
  const writes: typeof saved[] = [];
  await page.route("**/api/v1/admin/network**", async (route) => {
    if (route.request().url().endsWith("/test")) {
      probes.push(route.request().postDataJSON());
      await route.fulfill({ json: { ok: true, message: "代理访问 arXiv 成功，用时 0.8 秒。" } });
    } else {
      if (route.request().method() === "PUT") {
        saved = route.request().postDataJSON();
        writes.push(saved);
      }
      await route.fulfill({ json: saved });
    }
  });
  await page.goto("/admin");
  await page.getByRole("tab", { name: "网络代理" }).click();
  await page.getByLabel("HTTP 代理地址", { exact: true }).fill("http://127.0.0.1:10826");
  await page.getByRole("switch", { name: "启用论文下载代理" }).check();
  await page.getByRole("button", { name: "测试 arXiv 连接" }).click();
  await expect(page.getByRole("alert")).toContainText("代理访问 arXiv 成功");
  expect(probes).toEqual([{ proxy_enabled: true, proxy_url: "http://127.0.0.1:10826", timeout_seconds: 120 }]);
  expect(writes).toHaveLength(0);
  await page.getByRole("button", { name: "保存网络设置" }).click();
  await expect(page.getByText("网络设置已保存，下一次论文下载立即生效。")).toBeVisible();
  expect(writes).toEqual(probes);
  await page.reload();
  await page.getByRole("tab", { name: "网络代理" }).click();
  await expect(page.getByRole("switch", { name: "启用论文下载代理" })).toBeChecked();
  await expect(page.getByLabel("HTTP 代理地址", { exact: true })).toHaveValue("http://127.0.0.1:10826");
});

test("platform network page shows failed proxy probes and rejects credential URLs", async ({ page }) => {
  await fixtures(page);
  await page.route("**/api/v1/admin/network**", async (route) => {
    await route.fulfill({ json: route.request().url().endsWith("/test")
      ? { ok: false, message: "无法访问 arXiv，请检查代理地址、端口及服务是否启动。" }
      : { proxy_enabled: true, proxy_url: "http://127.0.0.1:10826", timeout_seconds: 120 } });
  });
  await page.goto("/admin");
  await page.getByRole("tab", { name: "网络代理" }).click();
  await page.getByRole("button", { name: "测试 arXiv 连接" }).click();
  await expect(page.getByRole("alert")).toContainText("无法访问 arXiv");
  await page.getByLabel("HTTP 代理地址", { exact: true }).fill("http://user:secret@proxy.example");
  await page.getByRole("button", { name: "保存网络设置" }).click();
  await expect(page.getByText(/请输入 HTTP 代理地址，例如/)).toBeVisible();
});

test("paper pages remain reachable beyond the API default limit", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/papers?**", (route) => {
    const offset = Number(
      new URL(route.request().url()).searchParams.get("offset") || 0,
    );
    const items =
      offset === 0
        ? Array.from({ length: 50 }, (_, i) => ({
            ...paper,
            id: `p${i}`,
            title: `研究论文 ${i}`,
          }))
        : [{ ...paper, id: "older", title: "较早的研究论文" }];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(items),
    });
  });
  await page.goto("/papers");
  await page.getByRole("button", { name: "加载更多论文" }).click();
  await expect(page.getByText("已加载 51 篇论文")).toBeVisible();
  await page.locator(".ant-pagination-item-6").click();
  await expect(page.getByText("较早的研究论文", { exact: true })).toBeVisible();
});

test("platform admin who is only a team member cannot see team admin actions", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([{ ...workspaces[0], role: "member" }]),
    }),
  );
  await page.goto("/settings");
  await expect(page.getByText("团队研究模型", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "添加模型" })).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "团队成员" })).toHaveCount(0);
});

test("HTML-first reading offers the source and safe HTML download without PDF pagination", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/papers/p1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...paper,
        metadata_json: { source_mime_type: "text/html" },
      }),
    }),
  );
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...reading,
        skill_id: "ljg-paper",
        revisions: [revision],
        jobs: [job],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await page.getByRole("button", { name: "查看证据 E1" }).click();
  await expect(
    page.getByRole("link", { name: "打开来源", exact: true }),
  ).toHaveAttribute("href", paper.source_url);
  await expect(
    page.getByRole("link", { name: "下载 HTML 原文", exact: true }),
  ).toHaveAttribute("href", "/api/v1/workspaces/w1/papers/p1/file");
  await expect(page.getByRole("link", { name: "定位原文 PDF" })).toHaveCount(0);
  await expect(page.locator('a[href*="#page="]')).toHaveCount(0);
  await expect(page.getByRole("cell", { name: /页码/ })).toHaveCount(0);
});

for (const source of [
  { mime: "application/pdf", label: "定位原文 PDF", suffix: "#page=5" },
  { mime: "text/html", label: "下载 HTML 原文", suffix: "" },
]) {
  test(`publication refreshes ${source.mime} source metadata without navigation`, async ({
    page,
  }) => {
    await fixtures(page);
    let published = false;
    let paperReads = 0;
    await page.route("**/api/v1/workspaces/w1/papers/p1", (route) => {
      paperReads++;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...paper,
          metadata_json: published ? { source_mime_type: source.mime } : {},
        }),
      });
    });
    await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...reading,
          current_revision_id: published ? "v1" : null,
          revisions: published ? [revision] : [],
          jobs: [
            published
              ? job
              : { ...job, status: "running", stage: "reading", progress: 35 },
          ],
        }),
      }),
    );
    await page.goto("/readings/r1");
    await expect(page.getByText("正在解读", { exact: true })).toBeVisible();
    await expect.poll(() => paperReads).toBe(1);
    await expect(
      page.getByRole("link", { name: source.label, exact: true }),
    ).toHaveCount(0);
    published = true;
    await expect(page.getByRole("button", { name: "查看证据 E1" })).toBeVisible(
      { timeout: 10000 },
    );
    await page.getByRole("button", { name: "查看证据 E1" }).click();
    await expect(
      page.getByRole("link", { name: source.label, exact: true }),
    ).toHaveAttribute(
      "href",
      `/api/v1/workspaces/w1/papers/p1/file${source.suffix}`,
    );
    expect(paperReads).toBeGreaterThanOrEqual(2);
    await expect(page).toHaveURL(/\/readings\/r1$/);
  });
}

test("Qwen compatibility persists across create and edit without exposing the API key", async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto("/settings");
  await page.getByRole("button", { name: "添加模型", exact: true }).click();
  await expect(
    page.locator(".ant-drawer-content").getByText("标准接口", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("连接名称").fill("本地 Qwen");
  await page
    .getByLabel("Base URL", { exact: true })
    .fill("http://localhost:8008/v1");
  await page.getByLabel("模型名称", { exact: true }).fill("Qwen3.8-27B");
  await page
    .getByLabel("API Key", { exact: true })
    .fill("fixture-only-test-key");
  await page.getByLabel("推理模式适配", { exact: true }).press("ArrowDown");
  await page.getByTitle("Qwen/vLLM：关闭思考", { exact: true }).click();
  await page.getByRole("button", { name: /保存连接$/ }).click();
  await expect(page.getByText("本地 Qwen", { exact: true })).toBeVisible();
  expect(
    requests.find((r) => r.path.endsWith("/models") && r.method === "POST")
      ?.body.thinking_format,
  ).toBe("qwen-chat-template");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(
    page
      .locator(".ant-drawer-content")
      .getByText("Qwen/vLLM：关闭思考", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("API Key（留空保持原密钥）", { exact: true }),
  ).toHaveValue("");
  await page.getByRole("button", { name: /保存连接$/ }).click();
  await expect(
    page.getByText("模型连接已保存", { exact: true }).last(),
  ).toBeVisible();
  await expect
    .poll(() =>
      requests.some(
        (r) => r.path.endsWith("/models/m1") && r.method === "PATCH",
      ),
    )
    .toBe(true);
  const edited = requests.find(
    (r) => r.path.endsWith("/models/m1") && r.method === "PATCH",
  );
  expect(edited?.body.thinking_format).toBe("qwen-chat-template");
  expect(edited?.body.api_key).toBe("");
});

test("switching to Anthropic hides and resets Qwen compatibility before saving", async ({
  page,
}) => {
  const requests = await fixtures(page, {
    thinkingFormat: "qwen-chat-template",
  });
  await page.goto("/settings");
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await expect(
    page
      .locator(".ant-drawer-content")
      .getByText("Qwen/vLLM：关闭思考", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("API 协议", { exact: true }).press("ArrowDown");
  await page.getByTitle("Anthropic Messages", { exact: true }).click();
  await expect(page.getByLabel("推理模式适配", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: /保存连接$/ }).click();
  await expect(page.getByText("模型连接已保存", { exact: true })).toBeVisible();
  await expect
    .poll(() =>
      requests.some(
        (r) => r.path.endsWith("/models/m1") && r.method === "PATCH",
      ),
    )
    .toBe(true);
  const edited = requests.find(
    (r) => r.path.endsWith("/models/m1") && r.method === "PATCH",
  );
  expect(edited?.body.protocol).toBe("anthropic-messages");
  expect(edited?.body.thinking_format).toBe("default");
  expect(edited?.body.api_key).toBe("");
});

const packet = {
  version: 1,
  paper_title: paper.title,
  research_question: "同一基座模型在不同任务训练后，合并是否有收益？",
  claims: [
    {
      id: "C1",
      kind: "result",
      basis: "observation",
      statement: "在本文同领域评测中，合并后的准确率高于未合并模型。",
      conditions: {
        model: "同一基座模型",
        dataset: "DomainBench",
        split: "test",
        metric: "准确率",
        baseline: "未合并模型",
        budget: "",
      },
      numeric_comparison: {
        value: 82.1,
        baseline_value: 80,
        relation: "higher",
        unit: "%",
      },
      evidence: [
        {
          id: "E1",
          source_id: "main",
          section_id: "results",
          page: 5,
          quote: evidence.quote,
        },
      ],
    },
    {
      id: "C2",
      kind: "lesson",
      basis: "inference",
      statement: "迁移到其他领域前，应先验证任务与基础模型是否兼容。",
      conditions: {
        model: "同一基座模型",
        dataset: "",
        split: "",
        metric: "",
        baseline: "",
        budget: "",
      },
      evidence: [
        {
          id: "E1",
          source_id: "main",
          section_id: "results",
          page: 5,
          quote: evidence.quote,
        },
      ],
    },
  ],
  limitations: ["现有结果不能直接推广到跨领域任务。"],
  open_questions: [
    {
      question: "跨领域合并是否仍有收益？",
      search_query: "cross domain model merging",
      reason: "本文只提供了同领域评测。",
    },
  ],
};
const guidedVerification = {
  workflow: { mode: "guided", native_status: "not_requested" },
  server_quote_check: "passed",
  fact_review: { passed: true, checked_claim_ids: ["C1", "C2"], issues: [] },
  product_review: { passed: true, issues: [] },
  cross_paper_status: "not_requested",
};
const guidedRevision = {
  ...revision,
  reading_packet: packet,
  verification: guidedVerification,
};

test("workflow native mode is explicitly selected and sent when creating a reading", async ({
  page,
}) => {
  const requests = await fixtures(page);
  await page.goto("/papers");
  await page.getByRole("button", { name: "解读论文", exact: true }).click();
  await expect(page.getByRole("radio", { name: /^模板解读/ })).toBeChecked();
  await page.getByRole("radio", { name: /完整原生 Skill/ }).check();
  await page.getByRole("button", { name: "开始解读", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: paper.title, exact: true }),
  ).toBeVisible();
  expect(
    requests.find((r) => r.path.endsWith("/readings") && r.method === "POST")
      ?.body.workflow_mode,
  ).toBe("native");
  await expect(
    page.getByText("完整原生 Skill · 阅读 Skill：DeepPaperNote", {
      exact: true,
    }),
  ).toBeVisible();
});

test("workflow shows actual stage completion and reuse while the current stage remains running", async ({
  page,
}) => {
  await fixtures(page);
  const stageRecords = [
    { name: "source", status: "succeeded", duration_ms: 1200 },
    { name: "extract", status: "reused", reused_from: "earlier-job" },
    {
      name: "fact_review",
      status: "running",
      started_at: new Date(Date.now() - 65000).toISOString(),
    },
  ];
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...reading,
        revisions: [guidedRevision],
        jobs: [
          {
            ...job,
            status: "running",
            stage: "fact_review",
            progress: 15,
            stage_records: stageRecords,
          },
        ],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await expect(page.locator(".workflow-stage-succeeded")).toContainText(
    "获取与解析原文",
  );
  await expect(page.locator(".workflow-stage-reused")).toContainText(
    "提取结论与证据",
  );
  await expect(page.locator(".workflow-stage-reused")).toContainText("已复用");
  await expect(page.locator(".workflow-stage-running")).toContainText(
    "事实与条件复核",
  );
  await expect(page.locator(".workflow-stage-running")).toContainText("进行中");
  await expect(page.locator(".workflow-stage-running")).not.toContainText(
    "已完成",
  );
  await expect(page.getByRole("progressbar")).toHaveCount(0);
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "原生 Skill 流程" }),
  ).toContainText("未请求");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "跨论文核查" }),
  ).toContainText("未请求");
});

test("workflow failures keep failed stages and unresolved reviews visible", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...reading,
        revisions: [
          {
            ...guidedRevision,
            verification: {
              ...guidedVerification,
              fact_review: {
                passed: false,
                issues: [{ claim_id: "C2", message: "条件不足" }],
              },
            },
          },
        ],
        jobs: [
          {
            ...job,
            status: "failed",
            stage: "fact_review",
            stage_records: [
              { name: "source", status: "reused", reused_from: "old-job" },
              {
                name: "fact_review",
                status: "failed",
                error: "结论 C2 缺少适用条件",
              },
            ],
            error: "事实复核未通过",
          },
        ],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await expect(page.locator(".workflow-stage-failed")).toContainText(
    "结论 C2 缺少适用条件",
  );
  await expect(page.locator(".workflow-stage-failed")).not.toContainText(
    "已完成",
  );
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "事实与条件复核" }),
  ).toContainText("未通过");
  await expect(
    page.getByRole("button", { name: "重新尝试", exact: true }),
  ).toBeVisible();
});

test("workflow keeps reading active while the agent repairs evidence in a later round", async ({ page }) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) => route.fulfill({ json: {
    ...reading, revisions: [], jobs: [{ ...job, status: "running", stage: "packet_repair_2", stage_records: [
      { name: "source", status: "reused" },
      { name: "extract", status: "succeeded" },
      { name: "packet_repair", status: "failed", error: "E3: quote still differs from original" },
      { name: "packet_repair_2", status: "running", started_at: new Date().toISOString() },
    ] }],
  } }));
  await page.goto("/readings/r1");
  await expect(page.getByRole("status").filter({ hasText: "正在自动修复证据" })).toBeVisible();
  await expect(page.locator(".workflow-stage-running")).toContainText("修正证据记录 · 第 2 轮");
  await expect(page.locator(".workflow-stage-failed")).toContainText("本轮未通过");
  await expect(page.getByText("本次任务未能完成", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "取消任务" })).toBeVisible();
});

test("workflow labels adaptive segment repairs and stops recovery notice after validation", async ({ page }) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) => route.fulfill({ json: {
    ...reading, revisions: [], jobs: [{ ...job, status: "running", stage: "compose", stage_records: [
      { name: "segment_1_repair", status: "failed", error: "E3: wrong source locator" },
      { name: "segment_1_repair_2", status: "succeeded" },
      { name: "packet_validate", status: "succeeded" },
      { name: "compose", status: "running", started_at: new Date().toISOString() },
    ] }],
  } }));
  await page.goto("/readings/r1");
  await expect(page.locator(".workflow-stage-succeeded").filter({ hasText: "分段 1 · 修正证据记录 · 第 2 轮" })).toBeVisible();
  await expect(page.getByText(/正在自动修复证据/)).toHaveCount(0);
  await expect(page.locator(".workflow-stage-running")).toContainText("按模板写作");
});

test("workflow packet shows claim basis, missing conditions, evidence, and native status separately", async ({
  page,
}, info) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...reading,
        revisions: [guidedRevision],
        jobs: [
          {
            ...job,
            stage_records: [
              { name: "source", status: "succeeded", duration_ms: 1200 },
              { name: "extract", status: "succeeded", duration_ms: 2200 },
              {
                name: "packet_validate",
                status: "succeeded",
                duration_ms: 100,
              },
              { name: "fact_review", status: "succeeded", duration_ms: 1800 },
              { name: "compose", status: "succeeded", duration_ms: 2300 },
              {
                name: "product_review",
                status: "succeeded",
                duration_ms: 1400,
              },
            ],
          },
        ],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  await expect(page.locator(".reading-packet")).toContainText(
    packet.research_question,
  );
  const claim = page.getByRole("region", { name: "结论 C1", exact: true });
  await expect(claim).toContainText("论文观察");
  await expect(claim).toContainText("82.1 %");
  await expect(
    claim.locator(".claim-conditions > div").filter({ hasText: "预算与规模" }),
  ).toContainText("未说明");
  await expect(
    page.getByRole("region", { name: "结论 C2", exact: true }),
  ).toContainText("推断");
  await claim.locator("summary").click();
  await claim.getByRole("button", { name: "查看证据 E1" }).click();
  await expect(page.getByRole("dialog").locator(".source-quote")).toHaveText(
    evidence.quote,
  );
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "引文匹配" }),
  ).toContainText("已通过");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "原生 Skill 流程" }),
  ).toContainText("未请求");
  await expect(
    page.getByRole("link", { name: "导出完整解读" }),
  ).toHaveAttribute("href", /view=full$/);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("tab", { name: "结论与条件", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("workflow-packet-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("workflow legacy records default to native without inventing completion or packet data", async ({
  page,
}) => {
  await fixtures(page);
  const { workflow_mode: _mode, ...legacyReading } = reading;
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...legacyReading,
        revisions: [
          {
            ...revision,
            verification: {
              server_quote_check: "passed",
              agent_report: { product_review: { passed: true } },
              native_checks: [],
            },
          },
        ],
        jobs: [job],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await expect(
    page.getByText("完整原生 Skill · 阅读 Skill：DeepPaperNote", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("tab", { name: "结论与条件", exact: true }),
  ).toHaveCount(0);
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "原生 Skill 流程" }),
  ).toContainText("未提供完成状态");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "解读内容复核" }),
  ).toContainText("已通过");
  await expect(page.locator(".markdown")).toContainText("关键成果");
});

test("workflow native completion is shown only from its explicit trusted status", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings/r1", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...reading,
        workflow_mode: "native",
        revisions: [
          {
            ...revision,
            verification: {
              server_quote_check: "passed",
              workflow: { mode: "native", native_status: "completed" },
            },
          },
        ],
        jobs: [job],
      }),
    }),
  );
  await page.goto("/readings/r1");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "原生 Skill 流程" }),
  ).toContainText("已完成");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "事实与条件复核" }),
  ).toContainText("未提供记录");
});

test("workflow list keeps the current stage after a historical failure was repaired", async ({
  page,
}) => {
  await fixtures(page);
  await page.route("**/api/v1/workspaces/w1/readings?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          ...reading,
          latest_job: {
            ...job,
            status: "running",
            stage: "compose",
            stage_records: [
              { name: "fact_review", status: "failed" },
              { name: "fact_review", status: "succeeded" },
              { name: "compose", status: "pending" },
            ],
          },
        },
      ]),
    }),
  );
  await page.goto("/readings");
  await expect(page.getByRole("cell", { name: /正在解读/ })).toContainText(
    "按模板写作",
  );
  await expect(page.getByRole("cell", { name: /正在解读/ })).not.toContainText(
    "事实与条件复核",
  );
});
