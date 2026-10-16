import { test, expect, type Page, type Route } from "@playwright/test";

// HTTP/SSE fixtures exercise the browser contract, not model answer quality.
const stamp = "2026-10-14T09:00:00Z";
const paper = {
  id: "p1",
  workspace_id: "w1",
  title: "DSpark",
  source_type: "arxiv",
  source_url: "https://arxiv.org/abs/2601.12345",
  sha256: null,
  metadata_json: {},
  created_at: stamp,
};
const revision = {
  id: "v1",
  number: 1,
  parent_revision_id: null,
  instruction: "",
  brief_markdown: "# DSpark 方法\n\n已完成的论文解读。",
  full_markdown: "# 完整解读\n\n原有解读仍然可读。",
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
  title: "解释置信度调度",
  created_at: stamp,
  updated_at: stamp,
};
const evidence = {
  id: "E1",
  source_id: "main",
  source_title: "DSpark 原文",
  quote: "Confidence schedules determine the token generation order.",
  section_id: "p5",
  page: 5,
  kind: "paper",
  verified: true,
};
const makeMessage = (
  id: string,
  role: "user" | "assistant",
  sequence: number,
  content: string,
  status = "succeeded",
) => ({
  id,
  conversation_id: "c1",
  role,
  sequence,
  content,
  status,
  evidence_index: [] as any[],
  verification: {},
  usage: {},
  error: null as string | null,
  job_id: role === "assistant" ? `j-${id}` : null,
  created_at: stamp,
  updated_at: stamp,
});
const question = makeMessage("m1", "user", 1, "什么是置信度调度？");
const answer = makeMessage(
  "m2",
  "assistant",
  2,
  "先选择有把握的位置。[E1](evidence:E1)",
);
answer.evidence_index = [evidence];

type FixtureOptions = {
  messages?: ReturnType<typeof makeMessage>[];
  saved?: boolean;
  older?: ReturnType<typeof makeMessage>[];
  latestRevision?: boolean;
};
async function fixtures(page: Page, options: FixtureOptions = {}) {
  const requests: { path: string; method: string; body: any }[] = [];
  const streams: { route: Route; after: string | null }[] = [];
  let topics = options.saved ? [structuredClone(conversation)] : [];
  let messages = structuredClone(options.messages ?? []);
  let older = options.older ? structuredClone(options.older) : [];
  let sendResponseLost = false;
  const requestIds = new Map<
    string,
    { user_message: any; assistant_message: any; job_id: string }
  >();
  if (options.saved)
    await page.addInitScript(() =>
      localStorage.setItem("paper-chat:u1:w1:r1", "c1"),
    );
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    const method = request.method();
    let body: any;
    try {
      body = request.postDataJSON();
    } catch {
      body = null;
    }
    requests.push({ path, method, body });
    let result: any = {};
    let status = 200;
    if (path === "/auth/me")
      result = { id: "u1", username: "研究员", is_admin: true };
    else if (path === "/workspaces")
      result = [{ id: "w1", name: "研究组", role: "admin" }];
    else if (path === "/workspaces/w1/papers/p1") result = paper;
    else if (path === "/workspaces/w1/readings/r1")
      result = {
        id: "r1",
        paper_id: "p1",
        paper_title: "DSpark",
        skill_id: "deeppapernote",
        workflow_mode: "guided",
        current_revision_id: options.latestRevision ? "v2" : "v1",
        created_at: stamp,
        revisions: options.latestRevision
          ? [{ ...revision, id: "v2", number: 2 }, revision]
          : [revision],
        jobs: [
          {
            id: "reading-job",
            workspace_id: "w1",
            reading_id: "r1",
            kind: "read",
            status: "succeeded",
            stage: "completed",
            progress: 100,
            attempt: 1,
            cancel_requested: false,
            error: null,
            usage: {},
            created_at: stamp,
            updated_at: stamp,
          },
        ],
      };
    else if (path === "/workspaces/w1/readings/r1/conversations") {
      if (method === "POST") {
        result = {
          ...conversation,
          id: topics.length ? "c2" : "c1",
          revision_id: body.revision_id,
          title: "新对话",
        };
        topics.unshift(result);
        status = 201;
      } else result = topics;
    } else if (/\/messages\/[^/]+\/events$/.test(path)) {
      streams.push({ route, after: url.searchParams.get("after") });
      return;
    } else if (/\/conversations\/[^/]+$/.test(path)) {
      const id = path.split("/").at(-1)!;
      const before = url.searchParams.get("before");
      result = {
        conversation: topics.find((item) => item.id === id),
        messages: before
          ? older
          : messages.filter((item) => item.conversation_id === id),
        next_before:
          !before && older.length ? (messages[0]?.sequence ?? null) : null,
      };
    } else if (/\/messages$/.test(path) && method === "POST") {
      const id = path.split("/").at(-2)!;
      const prior = requestIds.get(body.client_request_id);
      if (prior) result = prior;
      else {
        const sequence = messages.length ? messages.at(-1)!.sequence + 1 : 1;
        const user = {
          ...makeMessage(`m${sequence}`, "user", sequence, body.content),
          conversation_id: id,
        };
        const assistant = {
          ...makeMessage(
            `m${sequence + 1}`,
            "assistant",
            sequence + 1,
            "",
            "queued",
          ),
          conversation_id: id,
        };
        messages.push(user, assistant);
        result = {
          user_message: user,
          assistant_message: assistant,
          job_id: assistant.job_id,
        };
        requestIds.set(body.client_request_id, structuredClone(result));
      }
      if (sendResponseLost) {
        sendResponseLost = false;
        await route.abort("failed");
        return;
      }
      status = 202;
    } else if (/\/messages\/[^/]+\/cancel$/.test(path)) {
      const id = path.split("/").at(-2)!;
      const message = messages.find((item) => item.id === id)!;
      message.status = "cancelled";
      result = message;
    } else if (/\/messages\/[^/]+\/retry$/.test(path)) {
      const id = path.split("/").at(-2)!;
      const message = messages.find((item) => item.id === id)!;
      message.status = "queued";
      message.content = "";
      message.error = null;
      message.job_id = `retry-${id}`;
      result = { assistant_message: message, job_id: message.job_id };
      status = 202;
    }
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(result),
    });
  });
  return {
    requests,
    streams,
    get messages() {
      return messages;
    },
    loseNextSend() {
      sendResponseLost = true;
    },
    async emit(
      index: number,
      events: { id: number; event: string; data: any }[],
    ) {
      await expect.poll(() => streams.length).toBeGreaterThan(index);
      await streams[index].route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: events
          .map(
            (item) =>
              `id: ${item.id}\nevent: ${item.event}\ndata: ${JSON.stringify(item.data)}\n\n`,
          )
          .join(""),
      });
    },
    complete(id: string, content: string, evidenceIndex: any[] = []) {
      const item = messages.find((message) => message.id === id)!;
      item.content = content;
      item.evidence_index = evidenceIndex;
      item.status = "succeeded";
      return structuredClone(item);
    },
  };
}
const chat = (page: Page) =>
  page.getByRole("region", { name: "追问论文", exact: true });

test("creates a private pinned conversation lazily and streams incremental draft before final replacement", async ({
  page,
}) => {
  const state = await fixtures(page);
  await page.goto("/readings/r1");
  await expect(chat(page).getByText("对话仅自己可见")).toBeVisible();
  expect(
    state.requests.filter((request) => request.method === "POST"),
  ).toHaveLength(0);
  await chat(page)
    .getByRole("textbox", { name: "追问内容" })
    .fill("请解释置信度调度。");
  await chat(page).getByRole("button", { name: "发送", exact: true }).click();
  await state.emit(0, [
    {
      id: 1,
      event: "delta",
      data: { message_id: "m2", text: "先选择", attempt: 1 },
    },
    {
      id: 2,
      event: "progress",
      data: {
        message_id: "m2",
        stage: "sources",
        message: "正在核对第五页原文",
      },
    },
  ]);
  await expect(chat(page).getByText("先选择", { exact: true })).toBeVisible();
  await expect(chat(page).getByText("生成中 · 引用待核验")).toBeVisible();
  await expect(page.locator(".task-heading")).toContainText("处理完成");
  expect(
    state.requests.find(
      (request) =>
        request.path.endsWith("/conversations") && request.method === "POST",
    )?.body,
  ).toEqual({ revision_id: "v1" });
  await expect.poll(() => state.streams.length).toBe(2);
  expect(state.streams[1].after).toBe("2");
  const final = state.complete(
    "m2",
    "正式回答：按置信度安排生成。[E1](evidence:E1)",
    [evidence],
  );
  await state.emit(1, [
    {
      id: 3,
      event: "delta",
      data: { message_id: "m2", text: "有把握的位置。", attempt: 1 },
    },
    { id: 4, event: "completed", data: { message: final } },
  ]);
  await expect(
    chat(page).getByText(/正式回答：按置信度安排生成/),
  ).toBeVisible();
  await expect(chat(page).getByText("先选择有把握的位置。")).toHaveCount(0);
  await expect(chat(page).getByText("回答完成", { exact: true })).toBeVisible();
  await expect(chat(page).locator(".paper-chat-user")).toHaveCount(1);
  await chat(page)
    .getByRole("button", { name: "查看证据 E1", exact: true })
    .click();
  const drawer = page.getByRole("dialog", { name: "回答证据 E1" });
  await expect(drawer).toContainText(evidence.quote);
  await expect(
    drawer.getByRole("link", { name: "打开保存的原文" }),
  ).toHaveAttribute(
    "href",
    "/api/v1/workspaces/w1/conversations/c1/messages/m2/sources/main",
  );
});

test("restores snapshot after refresh without duplicating replayed text or user messages", async ({
  page,
}) => {
  const state = await fixtures(page, {
    saved: true,
    messages: [
      question,
      makeMessage("m2", "assistant", 2, "已经生成的内容", "running"),
    ],
  });
  await page.goto("/readings/r1");
  await expect(
    chat(page).getByText("已经生成的内容", { exact: true }),
  ).toBeVisible();
  await state.emit(0, [
    {
      id: 1,
      event: "delta",
      data: { message_id: "m2", text: "已经生成的内容" },
    },
  ]);
  await expect(
    chat(page).getByText("已经生成的内容", { exact: true }),
  ).toBeVisible();
  await expect.poll(() => state.streams.length).toBe(2);
  await state.emit(1, [
    {
      id: 1,
      event: "delta",
      data: { message_id: "m2", text: "已经生成的内容" },
    },
    { id: 2, event: "delta", data: { message_id: "m2", text: "，继续回答。" } },
  ]);
  await expect(
    chat(page).getByText("已经生成的内容，继续回答。", { exact: true }),
  ).toBeVisible();
  await expect(chat(page).locator(".paper-chat-user")).toHaveCount(1);
  const final = state.complete("m2", "已恢复的最终回答。");
  await state.emit(2, [
    { id: 3, event: "completed", data: { message: final } },
  ]);
  await page.reload();
  await expect(chat(page).getByText("已恢复的最终回答。")).toBeVisible();
  await expect(chat(page).locator(".paper-chat-user")).toHaveCount(1);
});

test("stops and retries the same answer without creating another question or changing reading status", async ({
  page,
}) => {
  const state = await fixtures(page, {
    saved: true,
    messages: [
      question,
      makeMessage("m2", "assistant", 2, "尚未完成", "running"),
    ],
  });
  await page.goto("/readings/r1");
  await chat(page)
    .getByRole("button", { name: "停止回答", exact: true })
    .click();
  await expect(chat(page).getByText("已停止", { exact: true })).toBeVisible();
  await chat(page)
    .getByRole("button", { name: "重新回答", exact: true })
    .click();
  await expect(
    chat(page).getByRole("button", { name: "停止回答", exact: true }),
  ).toBeVisible();
  await expect(chat(page).locator(".paper-chat-user")).toHaveCount(1);
  await expect(page.locator(".task-heading")).toContainText("处理完成");
  expect(
    state.requests.some(
      (request) =>
        request.path === "/workspaces/w1/conversations/c1/messages/m2/retry",
    ),
  ).toBe(true);
  expect(
    state.requests.filter(
      (request) =>
        request.path.includes("/jobs/") || request.path.endsWith("/revisions"),
    ),
  ).toHaveLength(0);
});

test("citations with repeated IDs remain scoped to their individual answers and history paginates", async ({
  page,
}) => {
  const external = {
    ...evidence,
    source_id: "related1",
    source_title: "相关论文",
    quote: "A different method uses fixed ordering for generation.",
    source_url: "https://example.org/paper",
    kind: "external",
    verified: false,
  };
  const later = {
    ...makeMessage(
      "m4",
      "assistant",
      4,
      "另一篇论文使用固定顺序。[E1](evidence:E1)",
      "partial",
    ),
    evidence_index: [external],
  };
  await fixtures(page, {
    saved: true,
    messages: [makeMessage("m3", "user", 3, "和相关论文比较呢？"), later],
    older: [question, answer],
  });
  await page.goto("/readings/r1");
  await chat(page).getByRole("button", { name: "查看更早的消息" }).click();
  await expect(chat(page).locator(".paper-chat-assistant")).toHaveCount(2);
  await chat(page)
    .locator('[data-message-id="m2"]')
    .getByRole("button", { name: "查看证据 E1", exact: true })
    .click();
  await expect(page.getByRole("dialog", { name: "回答证据 E1" })).toContainText(
    evidence.quote,
  );
  await page
    .getByRole("dialog", { name: "回答证据 E1" })
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  await chat(page)
    .locator('[data-message-id="m4"]')
    .getByRole("button", { name: "查看证据 E1", exact: true })
    .click();
  const drawer = page.getByRole("dialog", { name: "回答证据 E1" });
  await expect(drawer).toContainText(external.quote);
  await expect(drawer).toContainText("外部资料");
  await expect(drawer).toContainText("待核验");
  await expect(
    drawer.getByRole("link", { name: "打开保存的原文" }),
  ).toHaveAttribute(
    "href",
    "/api/v1/workspaces/w1/conversations/c1/messages/m4/sources/related1",
  );
});

test("keeps the existing conversation pinned and starts a new topic against the latest version", async ({
  page,
}) => {
  const state = await fixtures(page, {
    saved: true,
    messages: [question, answer],
    latestRevision: true,
  });
  await page.goto("/readings/r1");
  await expect(
    chat(page).getByText("基于版本 1", { exact: false }),
  ).toBeVisible();
  await chat(page).getByRole("button", { name: "基于新版本开始对话" }).click();
  await expect(
    chat(page).getByText("基于版本 2", { exact: true }),
  ).toBeVisible();
  await chat(page)
    .getByRole("textbox", { name: "追问内容" })
    .fill("新版本有什么变化？");
  await chat(page).getByRole("button", { name: "发送", exact: true }).click();
  await expect
    .poll(
      () =>
        state.requests.filter(
          (request) =>
            request.path.endsWith("/conversations") &&
            request.method === "POST",
        ).length,
    )
    .toBe(1);
  expect(
    state.requests.find(
      (request) =>
        request.path.endsWith("/conversations") && request.method === "POST",
    )?.body.revision_id,
  ).toBe("v2");
});

test("reuses the request identity after a lost send response", async ({
  page,
}) => {
  const state = await fixtures(page, { saved: true, messages: [] });
  state.loseNextSend();
  await page.goto("/readings/r1");
  await chat(page)
    .getByRole("textbox", { name: "追问内容" })
    .fill("请求丢失后继续发送");
  await chat(page).getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    chat(page).getByRole("button", { name: "发送", exact: true }),
  ).toBeEnabled();
  await chat(page).getByRole("button", { name: "发送", exact: true }).click();
  await expect(chat(page).locator(".paper-chat-user")).toHaveCount(1);
  const sends = state.requests.filter(
    (request) =>
      request.method === "POST" && request.path.endsWith("/messages"),
  );
  expect(sends).toHaveLength(2);
  expect(sends[0].body.client_request_id).toBe(sends[1].body.client_request_id);
  expect(sends[0].body.client_request_id).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
});

test("opens mobile follow-up drawer and preserves the conversation when closed", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixtures(page, { saved: true, messages: [question, answer] });
  await page.goto("/readings/r1");
  await page.getByRole("button", { name: "追问论文", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "追问论文", exact: true });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("什么是置信度调度？");
  await drawer.getByRole("textbox", { name: "追问内容" }).fill("接着上一问");
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "追问论文", exact: true }).click();
  await expect(drawer.getByRole("textbox", { name: "追问内容" })).toHaveValue(
    "接着上一问",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("shows quotation matching separately from an unsuccessful independent review", async ({
  page,
}) => {
  const partial = {
    ...answer,
    status: "partial",
    verification: {
      quote_match: { passed: true, issues: [] },
      independent_review: { passed: false, issues: ["结论仍需补充实验条件"] },
      issues: ["结论仍需补充实验条件"],
      limitations: ["外部资料未能完整获取"],
      verified_evidence_count: 1,
    },
  };
  await fixtures(page, { saved: true, messages: [question, partial] });
  await page.goto("/readings/r1");
  const verification = chat(page).locator(".paper-chat-verification");
  await expect(verification.locator(":scope > div").nth(0)).toHaveText(
    "引文匹配通过",
  );
  await expect(verification.locator(":scope > div").nth(1)).toHaveText(
    "内容复核需要核查",
  );
  await expect(
    chat(page).getByText("部分内容待核验", { exact: true }),
  ).toBeVisible();
  await verification.getByText("核查说明与资料范围").click();
  await expect(verification).toContainText("结论仍需补充实验条件");
  await expect(verification).toContainText("外部资料未能完整获取");
});

test("reconciles a completed HTTP snapshot if the terminal stream event is lost", async ({
  page,
}) => {
  const state = await fixtures(page, {
    saved: true,
    messages: [
      question,
      makeMessage("m2", "assistant", 2, "草稿内容", "running"),
    ],
  });
  await page.goto("/readings/r1");
  await expect(chat(page).getByText("草稿内容", { exact: true })).toBeVisible();
  state.complete("m2", "从已保存记录恢复的回答");
  await expect(
    chat(page).getByText("从已保存记录恢复的回答", { exact: true }),
  ).toBeVisible({ timeout: 10000 });
  await expect(
    chat(page).getByRole("button", { name: "停止回答", exact: true }),
  ).toHaveCount(0);
  await expect(chat(page).getByText("回答完成", { exact: true })).toBeVisible();
});
