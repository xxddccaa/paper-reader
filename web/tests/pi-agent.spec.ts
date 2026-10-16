import { expect, test, type Page, type Route } from "@playwright/test";

// Browser contract fixtures only: these tests do not claim real-model execution.
const stamp = "2026-10-15T09:00:00Z";
const task = {
  goal: "完整解读论文，核查正文与附录中的结论",
  current_action: "缩小读取范围，重新核对附录 A 的引文",
  accepted_work: ["已保存方法分析和 6 条证据", "已保存正文草稿"],
  issue: "附录请求超时，已有正文和图表检查结果保留。",
  coverage: {
    scope: "paper",
    body: { read: 12, total: 12 },
    appendix: { read: 1, total: 3 },
    figures: { read: 2, total: 4 },
    tables: { read: 1, total: 2 },
    references: { indexed: 15, total: 15 },
    complete: false,
    missing_sections: ["A2", "A3"],
    missing_figures: ["F3", "F4"],
    missing_tables: ["T2"],
  },
  remaining_work: ["完成附录 A 的证据定位", "完成独立事实与内容复核"],
  elapsed_seconds: 3600,
  remaining_seconds: 0,
  tool_calls: 198,
  remaining_tool_calls: 302,
  can_continue: true,
};
const job = {
  id: "j1",
  workspace_id: "w1",
  reading_id: "r1",
  kind: "read",
  status: "waiting",
  execution_engine: "pi-agent-v1",
  agent_task: task,
  stage: "agent",
  progress: 0,
  attempt: 1,
  cancel_requested: false,
  error: task.issue,
  usage: {},
  created_at: stamp,
  updated_at: stamp,
  stage_records: [
    { name: "extract", status: "failed", error: "legacy-stage-not-current" },
  ],
};
const published = {
  id: "v1",
  number: 1,
  parent_revision_id: null,
  instruction: "",
  brief_markdown: "# 已发布的解读\n\n这是此前已经核查的内容。",
  full_markdown: "# 完整解读\n\n已发布正文。",
  evidence_index: [],
  verification: {},
  provenance: {},
  artifacts: [],
  created_at: stamp,
};
const conversation = {
  id: "c1",
  reading_id: "r1",
  revision_id: "v1",
  title: "解释附录中的条件",
  created_at: stamp,
  updated_at: stamp,
};
function makeMessage(
  id: string,
  sequence: number,
  role: "user" | "assistant",
  status: string,
  content: string,
) {
  return {
    id,
    sequence,
    role,
    status,
    content,
    conversation_id: "c1",
    job_id: role === "assistant" ? "chat-j1" : null,
    evidence_index: [],
    verification: {},
    usage: {},
    error: null,
    execution_engine: role === "assistant" ? "pi-agent-v1" : "legacy",
    agent_task:
      role === "assistant"
        ? {
            ...structuredClone(task),
            goal: "解释附录中的适用条件",
            remaining_seconds: 0,
            elapsed_seconds: 600,
          }
        : null,
    created_at: stamp,
    updated_at: stamp,
  };
}
async function fixture(
  page: Page,
  options: {
    status?: string;
    legacy?: boolean;
    chat?: string;
    published?: boolean;
    canContinue?: boolean;
    verification?: Record<string, unknown>;
  } = {},
) {
  const requests: { path: string; method: string }[] = [];
  const streams: Route[] = [];
  const firstJob = {
    ...structuredClone(job),
    status: options.status ?? "waiting",
  };
  firstJob.agent_task.can_continue =
    options.canContinue ?? firstJob.status === "waiting";
  if (options.legacy) {
    firstJob.execution_engine = "legacy";
    firstJob.status = "failed";
  }
  let jobs = [firstJob];
  const messages = options.chat
    ? [
        makeMessage("m1", 1, "user", "succeeded", "请解释附录中的适用条件。"),
        makeMessage(
          "m2",
          2,
          "assistant",
          options.chat,
          "已经保存、仍需核查的回答草稿。",
        ),
      ]
    : [];
  if (messages[1]?.agent_task)
    messages[1].agent_task.can_continue = options.chat === "waiting";
  if (options.chat)
    await page.addInitScript(() =>
      localStorage.setItem("paper-chat:u1:w1:r1", "c1"),
    );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api/v1", "");
    const method = request.method();
    requests.push({ path, method });
    let result: unknown = {};
    let status = 200;
    if (path === "/auth/me")
      result = { id: "u1", username: "研究员", is_admin: true };
    else if (path === "/workspaces")
      result = [{ id: "w1", name: "研究组", role: "admin" }];
    else if (path === "/workspaces/w1/papers/p1")
      result = {
        id: "p1",
        title: "Agent Reading",
        source_type: "arxiv",
        source_url: "https://arxiv.org/abs/2601.12345",
        metadata_json: {},
        created_at: stamp,
      };
    else if (path === "/workspaces/w1/readings/r1")
      result = {
        id: "r1",
        paper_id: "p1",
        paper_title: "Agent Reading",
        skill_id: "deeppapernote",
        workflow_mode: "guided",
        current_revision_id: options.published || options.chat ? "v1" : null,
        revisions:
          options.published || options.chat
            ? [
                {
                  ...published,
                  verification: options.verification ?? published.verification,
                },
              ]
            : [],
        jobs,
      };
    else if (/\/jobs\/[^/]+\/continue$/.test(path)) {
      if (options.legacy)
        jobs = [
          {
            ...structuredClone(job),
            id: "j2",
            status: "queued",
            created_at: "2026-10-15T10:00:00Z",
          },
          ...jobs,
        ];
      else jobs[0].status = "queued";
      jobs[0].agent_task = {
        ...jobs[0].agent_task,
        can_continue: false,
        remaining_seconds: 3600,
        remaining_tool_calls: 802,
        issue: "",
        current_action: "等待 Agent 接着处理",
      };
      jobs[0].error = "";
      result = { job_id: jobs[0].id };
      status = 202;
    } else if (/\/jobs\/[^/]+\/cancel$/.test(path)) {
      jobs[0].status = "cancelled";
      jobs[0].agent_task.can_continue = true;
      result = jobs[0];
    } else if (path === "/workspaces/w1/readings/r1/conversations")
      result = options.chat ? [conversation] : [];
    else if (path === "/workspaces/w1/conversations/c1")
      result = { conversation, messages, next_before: null };
    else if (/\/messages\/[^/]+\/events$/.test(path)) {
      streams.push(route);
      return;
    } else if (/\/messages\/[^/]+\/continue$/.test(path)) {
      messages[1].status = "queued";
      messages[1].agent_task = {
        ...messages[1].agent_task!,
        can_continue: false,
        remaining_seconds: 600,
        remaining_tool_calls: 120,
        issue: "",
      };
      result = { assistant_message: messages[1], job_id: messages[1].job_id };
      status = 202;
    }
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(result),
    });
  });
  return { requests, jobs, messages, streams };
}

test("waiting reading shows saved Agent work and resumes the same task without publishing a draft", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  await page.goto("/readings/r1");
  const progress = page.getByRole("region", { name: "Agent 任务进度" });
  await expect(progress.getByText(task.goal)).toBeVisible();
  await expect(progress.getByText(task.accepted_work[0])).toBeVisible();
  await expect(progress.getByText("60 分 0 秒")).toBeVisible();
  await expect(progress.getByText("1 / 3", { exact: true })).toBeVisible();
  await expect(page.getByText("legacy-stage-not-current")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "任务阶段记录" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("tab", { name: "完整解读" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "导出 Markdown" })).toHaveCount(
    0,
  );
  await page.screenshot({ path: testInfo.outputPath("agent-waiting.png"), fullPage: true });
  await page.getByRole("button", { name: "继续任务", exact: true }).click();
  await expect(page.getByRole("button", { name: "停止任务" })).toBeVisible();
  await expect(progress.getByText(task.accepted_work[0])).toBeVisible();
  expect(state.jobs[0].id).toBe("j1");
  expect(state.requests.filter((item) => item.method === "POST")).toEqual([
    { path: "/workspaces/w1/jobs/j1/continue", method: "POST" },
  ]);
});

test("legacy failure hands over through continue and keeps the previous task available", async ({
  page,
}) => {
  const state = await fixture(page, { legacy: true });
  await page.goto("/readings/r1");
  await expect(
    page.getByRole("button", { name: "重新尝试", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "交给 Agent 继续" }).click();
  await expect(
    page.getByRole("region", { name: "Agent 任务进度" }),
  ).toBeVisible();
  await page.getByText("此前任务记录 · 1 次").click();
  await expect(page.getByText("原有流程 · 提取结论与证据")).toBeVisible();
  expect(
    state.requests
      .filter((item) => item.method === "POST")
      .map((item) => item.path),
  ).toEqual(["/workspaces/w1/jobs/j1/continue"]);
});

test("active Agent can stop and retain saved work while an existing published revision stays readable", async ({
  page,
}) => {
  const state = await fixture(page, { status: "running", published: true });
  await page.goto("/readings/r1");
  await expect(
    page.getByRole("heading", { name: "已发布的解读" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "停止任务", exact: true }).click();
  await expect(
    page.getByText("已保存的工作会保留，可以稍后继续。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "确定", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "继续任务", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(task.accepted_work[0])).toBeVisible();
  expect(
    state.requests.some(
      (item) =>
        item.path === "/workspaces/w1/jobs/j1/cancel" && item.method === "POST",
    ),
  ).toBe(true);
});

test("waiting task respects host can_continue and fits a narrow screen", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixture(page, { canContinue: false });
  await page.goto("/readings/r1");
  await expect(
    page.getByText("需先解决当前问题，再继续任务。", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "继续任务", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("agent-waiting-mobile.png"), fullPage: true });
});

test("private waiting answer preserves its pending draft and continues without a new question or generic job access", async ({
  page,
}) => {
  const state = await fixture(page, { status: "succeeded", chat: "waiting" });
  await page.goto("/readings/r1");
  const chat = page.getByRole("region", { name: "追问论文", exact: true });
  await expect(chat.getByText("待继续 · 草稿待核验")).toBeVisible();
  await expect(
    chat.getByText("草稿 · 待核验。通过核查后才会加入后续对话上下文。"),
  ).toBeVisible();
  await expect(chat.getByText(state.messages[1].content)).toBeVisible();
  await chat.getByRole("button", { name: "继续回答", exact: true }).click();
  await expect(
    chat.getByRole("button", { name: "停止回答", exact: true }),
  ).toBeVisible();
  await expect(chat.getByText(state.messages[1].content)).toBeVisible();
  await expect(
    chat.getByText(state.messages[0].content, { exact: true }),
  ).toHaveCount(1);
  expect(state.requests.filter((item) => item.method === "POST")).toEqual([
    {
      path: "/workspaces/w1/conversations/c1/messages/m2/continue",
      method: "POST",
    },
  ]);
  expect(
    state.requests.filter((item) => item.path.includes("/jobs/")),
  ).toHaveLength(0);
});

test("waiting SSE ends the chat stream and shows saved work and continuation", async ({
  page,
}) => {
  const state = await fixture(page, { status: "succeeded", chat: "running" });
  await page.goto("/readings/r1");
  await expect.poll(() => state.streams.length).toBe(1);
  const waiting = {
    ...state.messages[1],
    status: "waiting",
    content: "已保存的新草稿，等待核查。",
    agent_task: { ...state.messages[1].agent_task, can_continue: true },
  };
  await state.streams[0].fulfill({
    status: 200,
    contentType: "text/event-stream",
    body: `id: 1\nevent: waiting\ndata: ${JSON.stringify({ message_id: "m2", message: waiting })}\n\n`,
  });
  const chat = page.getByRole("region", { name: "追问论文", exact: true });
  await expect(chat.getByText(waiting.content)).toBeVisible();
  await expect(
    chat.getByRole("button", { name: "继续回答", exact: true }),
  ).toBeVisible();
  await expect(
    chat.getByRole("button", { name: "停止回答", exact: true }),
  ).toHaveCount(0);
  await expect(chat.getByText("回答完成", { exact: true })).toHaveCount(0);
});

test("an older waiting answer cannot continue after a later answer exists", async ({ page }) => {
  const state = await fixture(page, { status: "succeeded", chat: "waiting" });
  state.messages.push(
    makeMessage("m3", 3, "user", "succeeded", "接着解释实验设置。"),
    makeMessage("m4", 4, "assistant", "succeeded", "后来已经核查的回答。"),
  );
  state.messages[3].agent_task!.can_continue = false;
  await page.goto("/readings/r1");
  const chat = page.getByRole("region", { name: "追问论文", exact: true });
  await expect(chat.getByText("后来已经核查的回答。")).toBeVisible();
  await expect(chat.getByText("待继续 · 草稿待核验")).toBeVisible();
  await expect(chat.getByRole("button", { name: "继续回答", exact: true })).toHaveCount(0);
  expect(state.requests.filter((item) => item.method === "POST")).toHaveLength(0);
});

test("published Agent checks show each host verdict without inferring missing reviews from overall success", async ({
  page,
}) => {
  await fixture(page, {
    status: "succeeded",
    published: true,
    verification: {
      execution_engine: "pi-agent-v1",
      passed: true,
      evidence_check: { passed: false, issues: ["citation pending"] },
      reviews: {
        fact: { passed: true },
        product: { passed: false },
        coverage: { passed: true },
      },
      coverage: task.coverage,
    },
  });
  await page.goto("/readings/r1");
  const checks = page.getByRole("region", { name: "核查与流程状态" });
  await expect(checks.getByText("证据出处核验")).toBeVisible();
  await expect(checks.getByText("存在待核查项")).toBeVisible();
  await expect(
    checks
      .locator("dl > div")
      .filter({ has: page.getByText("图表复核", { exact: true }) }),
  ).toContainText("未提供记录");
  await expect(
    checks
      .locator("dl > div")
      .filter({ has: page.getByText("解读内容复核", { exact: true }) }),
  ).toContainText("未通过");
  await expect(
    checks
      .locator("dl > div")
      .filter({ has: page.getByText("事实与条件复核", { exact: true }) }),
  ).toContainText("已通过");
  await checks.getByText("论文资料覆盖", { exact: true }).click();
  await expect(checks.getByText("1 / 3", { exact: true })).toBeVisible();
});
