import { expect, test, type Page } from "@playwright/test";

// HTTP fixtures exercise rendering only. No backend or model is contacted.
const createdAt = "2026-10-13T10:00:00Z";
const sourceSelection = {
  format: "html",
  reason: "HTML 包含完整正文、公式、表格与附录。",
  requested_id: "2607.05147",
  resolved_id: "2607.05147v1",
};
const coverage = {
  total: 3,
  processed: 3,
  complete: true,
  sections: [
    {
      id: "S1",
      title: "方法与实验",
      kind: "body",
      status: "processed",
      segment_ids: ["segment_1"],
    },
    {
      id: "A1",
      title: "附录 A：反例",
      kind: "appendix",
      status: "processed",
      segment_ids: ["segment_2"],
    },
    {
      id: "bib",
      title: "参考文献",
      kind: "references",
      status: "indexed",
      segment_ids: [],
    },
  ],
  segments: [
    { id: "segment_1", section_ids: ["S1"], status: "succeeded" },
    { id: "segment_2", section_ids: ["A1"], status: "reused" },
  ],
  unresolved: [] as unknown[],
};
const figureChecks = {
  total: 1,
  verified: 1,
  complete: true,
  items: [
    {
      figure_id: "fig:1",
      section_id: "S1",
      caption: "固定预算下的误差趋势",
      status: "verified",
      image_sha256: "a".repeat(64),
      observation: "蓝线在给定预算范围内下降。",
      conditions: "固定模型、测试集与训练预算；仅比较图示区间。",
      uncertainties: [] as string[],
      claim_ids: ["C3"],
    },
  ],
  unresolved: [] as unknown[],
};
const paper = {
  id: "p3",
  workspace_id: "w3",
  title: "Structured reading fixture",
  source_type: "arxiv",
  source_url: "https://arxiv.org/abs/2607.05147v1",
  metadata_json: { source_mime_type: "text/html" },
  created_at: createdAt,
};
const evidence = [
  {
    id: "E1",
    source_id: "main",
    source_title: paper.title,
    section_id: "S1",
    kind: "paper",
    verified: true,
    evidence_type: "text_quote",
    verification_method: "quote_match",
    quote:
      "All comparisons use a fixed training budget and held-out test split.",
  },
  {
    id: "E2",
    source_id: "main",
    source_title: paper.title,
    section_id: "S1",
    kind: "paper",
    verified: true,
    evidence_type: "table_cell",
    verification_method: "table_cell_match",
    quote: "0.7",
    table_id: "tab:1",
    row: 0,
    column: 2,
    headers: ["Test split", "Fixed budget", "Error (%)"],
  },
  {
    id: "E3",
    source_id: "main",
    source_title: paper.title,
    section_id: "S1",
    kind: "paper",
    verified: true,
    evidence_type: "image_observation",
    verification_method: "visual_observation",
    quote: "",
    figure_id: "fig:1",
    image_sha256: "a".repeat(64),
    observation_id: "obs:1",
    observation: figureChecks.items[0].observation,
    conditions: figureChecks.items[0].conditions,
  },
];
const packet = {
  version: 2,
  paper_title: paper.title,
  research_question: "固定条件下误差如何变化？",
  claims: evidence.map((item, index) => ({
    id: `C${index + 1}`,
    kind: "result",
    basis: "observation",
    statement: [
      "比较保持训练预算一致。",
      "表格所列误差为 0.7。",
      "图中曲线在给定区间下降。",
    ][index],
    conditions: {
      model: "固定模型",
      dataset: "FixtureBench",
      split: "test",
      metric: "error",
      baseline: "",
      budget: "固定预算",
    },
    evidence: [item],
  })),
  limitations: ["不能推广至未测试的预算区间。"],
  open_questions: [],
};
const verification = {
  workflow: { mode: "guided", native_status: "not_requested" },
  server_quote_check: "passed",
  fact_review: { passed: true },
  product_review: { passed: true },
  source_selection: sourceSelection,
  section_coverage: coverage,
  figure_checks: figureChecks,
};
const revision = {
  id: "v3",
  number: 3,
  parent_revision_id: null,
  instruction: "",
  brief_markdown: "固定预算下比较误差。[E1] [E2] [E3]",
  full_markdown: "## 完整解读\n\n保留附录反例与适用条件。[E1] [E2] [E3]",
  evidence_index: evidence,
  reading_packet: packet,
  verification,
  artifacts: [],
  provenance: {},
  created_at: createdAt,
};
const job = {
  id: "j3",
  workspace_id: "w3",
  reading_id: "r3",
  kind: "read",
  status: "succeeded",
  stage: "completed",
  progress: 100,
  attempt: 1,
  cancel_requested: false,
  stage_records: [] as unknown[],
  error: null,
  usage: {},
  created_at: createdAt,
  updated_at: createdAt,
};
const reading = {
  id: "r3",
  paper_id: "p3",
  paper_title: paper.title,
  skill_id: "deeppapernote",
  workflow_mode: "guided",
  current_revision_id: "v3",
  created_at: createdAt,
  revisions: [revision],
  jobs: [job],
};
const inventoryStage = {
  name: "source_inventory",
  status: "succeeded",
  summary: {
    source_selection: sourceSelection,
    section_inventory: coverage.sections.map(({ id, title, kind }) => ({
      id,
      title,
      kind,
    })),
    figures: figureChecks.items.map(({ figure_id, section_id, caption }) => ({
      id: figure_id,
      section_id,
      caption,
    })),
  },
};
async function fixtures(page: Page, detail: unknown = reading) {
  await page.route("**/api/v1/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    let result: unknown;
    if (pathname === "/api/v1/auth/me")
      result = { id: "u3", username: "UI fixture", is_admin: true };
    else if (pathname === "/api/v1/workspaces")
      result = [{ id: "w3", name: "V3 tests", role: "admin" }];
    else if (pathname === "/api/v1/workspaces/w3/readings/r3") result = detail;
    else if (pathname === "/api/v1/workspaces/w3/readings/r3/conversations")
      result = [];
    else if (pathname === "/api/v1/workspaces/w3/papers/p3") result = paper;
    else if (pathname === "/api/v1/workspaces/w3/readings")
      result = [{ ...reading, latest_job: (detail as typeof reading).jobs[0] }];
    else throw new Error(`Unexpected UI fixture request: ${pathname}`);
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(result),
    });
  });
}

test("v3 displays source version, complete appendix coverage and distinct evidence verification methods", async ({
  page,
}) => {
  await fixtures(page);
  await page.goto("/readings/r3");
  const summary = page.getByRole("region", { name: "本版本来源与覆盖" });
  await expect(summary).toContainText(sourceSelection.reason);
  await expect(summary).toContainText("HTML");
  await expect(summary).toContainText("2607.05147v1");
  const sections = summary.getByRole("region", { name: "章节覆盖" });
  await expect(sections).toContainText("已处理 3 / 3 个章节");
  await expect(sections).toContainText("全部完成");
  await sections.locator("summary").click();
  await expect(
    sections.getByText("附录 A：反例", { exact: true }),
  ).toBeVisible();
  await expect(sections.getByText("已索引", { exact: true })).toBeVisible();
  const figures = summary.getByRole("region", { name: "图表核查" });
  await expect(figures).toContainText("已核查 1 / 1 个图表");
  await figures.locator("summary").click();
  await expect(figures).toContainText(figureChecks.items[0].conditions);
  await expect(figures).toContainText("a".repeat(64));
  await expect(page.locator(".evidence-count")).toContainText(
    "3 / 3 条证据已核验",
  );
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "证据出处核验" }),
  ).toContainText("已通过");
  await expect(
    page
      .locator(".verification-summary dl > div")
      .filter({ hasText: "原生 Skill 流程" }),
  ).toContainText("未请求");

  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const quote = page.getByRole("region", { name: "结论 C1", exact: true });
  await quote.locator("summary").click();
  await expect(quote).toContainText("原文引文");
  await expect(quote).toContainText("核验方式：引文匹配");
  await expect(quote.locator("blockquote")).toHaveText(evidence[0].quote);
  const table = page.getByRole("region", { name: "结论 C2", exact: true });
  await table.locator("summary").click();
  await expect(table).toContainText("表格单元格");
  await expect(table).toContainText("单元格已匹配");
  await expect(table).toContainText("核验方式：单元格与表头匹配");
  await expect(table).toContainText("0 / 2");
  await expect(table).toContainText("Test split → Fixed budget → Error (%)");
  await expect(table.locator("blockquote")).toHaveText("0.7");
  const image = page.getByRole("region", { name: "结论 C3", exact: true });
  await image.locator("summary").click();
  await expect(image).toContainText("观察记录已核验");
  await expect(image).toContainText("核验方式：图像观察记录核验");
  await expect(image.locator("blockquote")).toHaveCount(0);
  await expect(image.getByText("原文匹配", { exact: true })).toHaveCount(0);
  await image.getByRole("button", { name: "查看证据 E3" }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.locator("blockquote")).toHaveCount(0);
  await expect(drawer).toContainText("不是原文引文匹配");
  await expect(drawer).toContainText("obs:1");
  await expect(drawer).toContainText(figureChecks.items[0].observation);
  await drawer.getByRole("button", { name: "全部证据", exact: true }).click();
  await expect(drawer.locator(".evidence-entry")).toHaveCount(3);
  const tableEntry = drawer
    .locator(".evidence-entry")
    .filter({ hasText: "表格单元格" });
  await expect(tableEntry).toContainText("核验方式：单元格与表头匹配");
  await tableEntry.click();
  await expect(drawer).toContainText("tab:1");
  await expect(drawer).toContainText("0 / 2");
});

test("v3 failed jobs without a revision expose latest section and figure gaps from host stage summaries", async ({
  page,
}, info) => {
  const failedCoverage = {
    ...coverage,
    processed: 1,
    complete: false,
    sections: coverage.sections.map((item, index) => ({
      ...item,
      status: index === 1 ? "failed" : index === 2 ? "pending" : "processed",
    })),
    segments: coverage.segments.map((item, index) => ({
      ...item,
      status: index ? "failed" : "succeeded",
    })),
    unresolved: [{ section_id: "A1", message: "附录反例尚未完成复核。" }],
  };
  const failedFigures = {
    ...figureChecks,
    verified: 0,
    complete: false,
    items: [
      {
        ...figureChecks.items[0],
        status: "unreadable",
        uncertainties: ["纵轴小字无法辨读，具体数值未知。"],
      },
    ],
    unresolved: ["fig:1 仍需核查，不能填入猜测数值。"],
  };
  await fixtures(page, {
    ...reading,
    current_revision_id: null,
    revisions: [],
    jobs: [
      {
        ...job,
        status: "failed",
        stage: "segment_2_review",
        error: "章节与图表核查未完成",
        stage_records: [
          inventoryStage,
          {
            name: "segment_plan",
            status: "succeeded",
            summary: {
              sections: failedCoverage.sections,
              segments: failedCoverage.segments,
            },
          },
          { name: "section_coverage", status: "succeeded", summary: coverage },
          {
            name: "section_coverage",
            status: "failed",
            summary: failedCoverage,
          },
          { name: "figure_checks", status: "failed", summary: failedFigures },
          { name: "segment_1_extract", status: "succeeded" },
          {
            name: "segment_2_review",
            status: "failed",
            error: "附录反例尚未完成复核。",
          },
          { name: "figure_1_review", status: "failed" },
          { name: "merge_review", status: "pending" },
        ],
      },
    ],
  });
  await page.goto("/readings/r3");
  const summary = page.getByRole("region", { name: "本次任务来源与覆盖" });
  await expect(summary).toContainText(sourceSelection.reason);
  await expect(summary).toContainText("已处理 1 / 3 个章节");
  await expect(summary).toContainText("已核查 0 / 1 个图表");
  await expect(
    summary.getByText("附录 A：反例", { exact: true }),
  ).toBeVisible();
  await expect(
    summary.getByText("纵轴小字无法辨读，具体数值未知。", { exact: true }),
  ).toBeVisible();
  await expect(summary).toContainText("A1：附录反例尚未完成复核。");
  await expect(summary).toContainText("fig:1 仍需核查，不能填入猜测数值。");
  await expect(summary.getByText("全部完成", { exact: true })).toHaveCount(0);
  await expect(page.locator(".workflow-stage")).toContainText([
    "整理原文目录与图表",
    "规划分段阅读",
    "汇总章节覆盖",
    "汇总章节覆盖",
    "汇总图表核查",
    "分段 1 · 提取结论与证据",
    "分段 2 · 事实与条件复核",
    "图表 1 · 观察与核查",
    "合并结论与交叉复核",
  ]);
  await expect(
    page.getByRole("button", { name: "重新尝试", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "本版本来源与覆盖" }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("v3-failed-coverage-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("v3 source inventory remains inspectable when failure precedes segment planning", async ({
  page,
}) => {
  await fixtures(page, {
    ...reading,
    current_revision_id: null,
    revisions: [],
    jobs: [
      {
        ...job,
        status: "failed",
        stage: "source_inventory",
        stage_records: [
          {
            ...inventoryStage,
            status: "failed",
            summary: {
              ...inventoryStage.summary,
              source_selection: {
                ...sourceSelection,
                format: "pdf",
                reason: "HTML 不可用，选用相同版本 PDF。",
              },
            },
          },
        ],
      },
    ],
  });
  await page.goto("/readings/r3");
  const summary = page.getByRole("region", { name: "本次任务来源与覆盖" });
  await expect(summary).toContainText("HTML 不可用，选用相同版本 PDF。");
  await expect(
    summary.getByText("附录 A：反例", { exact: true }),
  ).toBeVisible();
  await expect(
    summary.getByText("固定预算下的误差趋势", { exact: true }),
  ).toBeVisible();
  await expect(
    summary.getByText("未记录核查状态", { exact: true }),
  ).toHaveCount(4);
  await expect(summary.getByText("全部完成", { exact: true })).toHaveCount(0);
});

test("v3 segment plan shows pending sections and readable running stage in the list and detail", async ({
  page,
}) => {
  await fixtures(page, {
    ...reading,
    current_revision_id: null,
    revisions: [],
    jobs: [
      {
        ...job,
        status: "running",
        stage: "segment_12_extract",
        stage_records: [
          inventoryStage,
          {
            name: "segment_plan",
            status: "succeeded",
            summary: {
              sections: coverage.sections.map((item) => ({
                ...item,
                status: "pending",
              })),
              segments: [
                {
                  id: "segment_12",
                  section_ids: ["S1", "A1"],
                  status: "running",
                },
              ],
            },
          },
          { name: "segment_12_extract", status: "running" },
        ],
      },
    ],
  });
  await page.goto("/readings");
  await expect(page.getByRole("cell", { name: /正在解读/ })).toContainText(
    "分段 12 · 提取结论与证据",
  );
  await page.goto("/readings/r3");
  await expect(page.locator(".task-heading")).toContainText(
    "分段 12 · 提取结论与证据",
  );
  const summary = page.getByRole("region", { name: "本次任务来源与覆盖" });
  await expect(
    summary.getByText("附录 A：反例", { exact: true }),
  ).toBeVisible();
  await expect(summary.getByText("待处理", { exact: true })).toHaveCount(3);
  await expect(summary.getByRole("list", { name: "分段明细" })).toContainText(
    "segment_12",
  );
  await expect(summary.getByRole("list", { name: "分段明细" })).toContainText(
    "S1、A1",
  );
});

test("v3 missing or mismatched evidence methods never claim verified visual or cell matches", async ({
  page,
}) => {
  const incompleteEvidence = [
    evidence[0],
    { ...evidence[1], verification_method: "quote_match" },
    { ...evidence[2], verification_method: undefined, observation: undefined },
  ];
  await fixtures(page, {
    ...reading,
    revisions: [{ ...revision, evidence_index: incompleteEvidence }],
  });
  await page.goto("/readings/r3");
  await expect(page.locator(".evidence-count")).toContainText(
    "1 / 3 条证据已核验",
  );
  await page.getByRole("button", { name: "查看证据 E3", exact: true }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer).toContainText("未确认核验");
  await expect(drawer).toContainText("未提供图像观察描述");
  await expect(drawer.locator("blockquote")).toHaveCount(0);
  await expect(drawer.getByText("观察记录已核验", { exact: true })).toHaveCount(
    0,
  );
  await expect(drawer.getByText("原文匹配", { exact: true })).toHaveCount(0);
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const table = page.getByRole("region", { name: "结论 C2", exact: true });
  await table.locator("summary").click();
  await expect(table).toContainText("未确认核验");
  await expect(table.getByText("单元格已匹配", { exact: true })).toHaveCount(0);
});

test("v3 packet flags cannot supply publication verification for missing index entries or methods", async ({
  page,
}) => {
  // A producer-supplied packet can contain extra fields. They must not supply
  // verification state when the authoritative published index omits it.
  const { verification_method: _method, ...visualWithoutMethod } = evidence[2];
  await fixtures(page, {
    ...reading,
    revisions: [
      { ...revision, evidence_index: [evidence[0], visualWithoutMethod] },
    ],
  });
  await page.goto("/readings/r3");
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const table = page.getByRole("region", { name: "结论 C2", exact: true });
  await table.locator("summary").click();
  await expect(table).toContainText("未确认核验");
  await expect(table.getByRole("button", { name: "查看证据 E2" })).toHaveCount(
    0,
  );
  const image = page.getByRole("region", { name: "结论 C3", exact: true });
  await image.locator("summary").click();
  await expect(image).toContainText("未确认核验");
  await expect(image.getByText("观察记录已核验", { exact: true })).toHaveCount(
    0,
  );
  await expect(image.locator("blockquote")).toHaveCount(0);
});

test("v3 coverage never hides unresolved gaps behind complete flags", async ({
  page,
}) => {
  await fixtures(page, {
    ...reading,
    revisions: [
      {
        ...revision,
        verification: {
          ...verification,
          section_coverage: { ...coverage, unresolved: ["附录条件仍待确认。"] },
          figure_checks: {
            ...figureChecks,
            items: [
              {
                ...figureChecks.items[0],
                uncertainties: ["误差线含义未确认。"],
              },
            ],
          },
        },
      },
    ],
  });
  await page.goto("/readings/r3");
  const summary = page.getByRole("region", { name: "本版本来源与覆盖" });
  await expect(summary.getByText("尚未完成", { exact: true })).toHaveCount(2);
  await expect(
    summary.getByText("附录条件仍待确认。", { exact: true }),
  ).toBeVisible();
  await expect(
    summary.getByText("误差线含义未确认。", { exact: true }),
  ).toBeVisible();
  await expect(summary.getByText("全部完成", { exact: true })).toHaveCount(0);
});

const completeSections = coverage.sections.slice(0, 2);
const completeFigures = [
  figureChecks.items[0],
  { ...figureChecks.items[0], figure_id: "fig:2" },
];
for (const details of [
  { name: "missing arrays", sections: undefined, items: undefined },
  { name: "null arrays", sections: null, items: null },
  { name: "empty arrays", sections: [], items: [] },
  {
    name: "too few details",
    sections: completeSections.slice(0, 1),
    items: completeFigures.slice(0, 1),
  },
  {
    name: "excess details",
    sections: coverage.sections,
    items: [...completeFigures, { ...completeFigures[0], figure_id: "fig:3" }],
  },
  {
    name: "duplicate identities",
    sections: [completeSections[0], completeSections[0]],
    items: [completeFigures[0], completeFigures[0]],
  },
  {
    name: "missing identities",
    sections: [completeSections[0], { ...completeSections[1], id: undefined }],
    items: [
      completeFigures[0],
      { ...completeFigures[1], figure_id: undefined },
    ],
  },
  {
    name: "blank identities",
    sections: [completeSections[0], { ...completeSections[1], id: " " }],
    items: [completeFigures[0], { ...completeFigures[1], figure_id: " " }],
  },
]) {
  test(`v3 positive totals with ${details.name} remain incomplete in revision and job summaries`, async ({
    page,
  }) => {
    const sectionSummary = {
      ...coverage,
      total: 2,
      processed: 2,
      sections: details.sections,
      segments: [],
    };
    const figureSummary = {
      ...figureChecks,
      total: 2,
      verified: 2,
      items: details.items,
    };
    await fixtures(page, {
      ...reading,
      revisions: [
        {
          ...revision,
          verification: {
            ...verification,
            section_coverage: sectionSummary,
            figure_checks: figureSummary,
          },
        },
      ],
      jobs: [
        {
          ...job,
          status: "failed",
          stage_records: [
            {
              name: "section_coverage",
              status: "failed",
              summary: sectionSummary,
            },
            { name: "figure_checks", status: "failed", summary: figureSummary },
          ],
        },
      ],
    });
    await page.goto("/readings/r3");
    for (const context of ["本版本来源与覆盖", "本次任务来源与覆盖"]) {
      const summary = page.getByRole("region", { name: context });
      await expect(summary).toContainText("已处理 2 / 2 个章节");
      await expect(summary).toContainText("已核查 2 / 2 个图表");
      await expect(summary.getByText("尚未完成", { exact: true })).toHaveCount(
        2,
      );
      await expect(summary.getByText("全部完成", { exact: true })).toHaveCount(
        0,
      );
      await expect(summary.locator("details[open]")).toHaveCount(2);
    }
  });
}

for (const total of [0, 2]) {
  test(`v3 ${total} total completes with exactly ${total} unique detail identities`, async ({
    page,
  }) => {
    await fixtures(page, {
      ...reading,
      revisions: [
        {
          ...revision,
          verification: {
            ...verification,
            section_coverage: {
              ...coverage,
              total,
              processed: total,
              sections: completeSections.slice(0, total),
              segments: [],
            },
            figure_checks: {
              ...figureChecks,
              total,
              verified: total,
              items: completeFigures.slice(0, total),
            },
          },
        },
      ],
    });
    await page.goto("/readings/r3");
    const summary = page.getByRole("region", { name: "本版本来源与覆盖" });
    await expect(
      summary.getByRole("region", { name: "章节覆盖" }),
    ).toContainText("全部完成");
    await expect(
      summary.getByRole("region", { name: "图表核查" }),
    ).toContainText(total === 0 ? "无需核查" : "全部完成");
    await expect(summary.getByText("尚未完成", { exact: true })).toHaveCount(0);
  });
}

for (const item of evidence) {
  for (const verdict of [
    {
      name: "passed evidence verdict",
      evidenceStatus: "passed",
      quoteStatus: "not_requested",
      label: "已通过",
    },
    {
      name: "partial evidence verdict",
      evidenceStatus: "partial",
      quoteStatus: "passed",
      label: "存在待核查项",
    },
    {
      name: "failed evidence verdict",
      evidenceStatus: "failed",
      quoteStatus: "passed",
      label: "存在待核查项",
    },
    {
      name: "legacy fallback",
      evidenceStatus: undefined,
      quoteStatus: "passed",
      label: "已通过",
    },
    {
      name: "missing verdicts",
      evidenceStatus: undefined,
      quoteStatus: undefined,
      label: "未提供记录",
    },
  ]) {
    test(`v3 ${item.evidence_type} summary uses ${verdict.name}`, async ({
      page,
    }) => {
      await fixtures(page, {
        ...reading,
        revisions: [
          {
            ...revision,
            evidence_index: [item],
            verification: {
              ...verification,
              server_evidence_check: verdict.evidenceStatus,
              server_quote_check: verdict.quoteStatus,
            },
          },
        ],
      });
      await page.goto("/readings/r3");
      const label =
        item.evidence_type !== "text_quote" ||
        verdict.evidenceStatus !== undefined
          ? "证据出处核验"
          : "引文匹配";
      const row = page
        .locator(".verification-summary dl > div")
        .filter({ hasText: label });
      await expect(row.locator(".ant-tag")).toHaveText(verdict.label);
    });
  }
}

test("v3 section_lookup displays the host paragraph without claiming a model quote match", async ({
  page,
}) => {
  const paragraph =
    "All comparisons use a fixed training budget and held-out test split. The appendix preserves counterexamples outside this budget range.";
  const locator = {
    id: "E4",
    source_id: "main",
    section_id: "S1.p2",
    evidence_type: "text_quote",
    selection: "section",
  };
  const resolvedParagraph = {
    ...evidence[0],
    ...locator,
    quote: paragraph,
    verification_method: "section_lookup",
  };
  await fixtures(page, {
    ...reading,
    revisions: [
      {
        ...revision,
        evidence_index: [...evidence, resolvedParagraph],
        reading_packet: {
          ...packet,
          claims: [
            packet.claims[0],
            {
              ...packet.claims[1],
              evidence: [
                {
                  id: "E2",
                  source_id: "main",
                  section_id: "S1",
                  evidence_type: "table_cell",
                  table_id: "tab:1",
                  row: 0,
                  column: 2,
                },
              ],
            },
            packet.claims[2],
            { ...packet.claims[0], id: "C4", evidence: [locator] },
          ],
        },
        verification: { ...verification, server_evidence_check: "passed" },
      },
    ],
  });
  await page.goto("/readings/r3");
  await expect(page.locator(".evidence-count")).toContainText(
    "4 / 4 条证据已核验",
  );
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const selected = page.getByRole("region", { name: "结论 C4", exact: true });
  await selected.locator("summary").click();
  await expect(selected.locator("blockquote")).toHaveText(paragraph);
  await expect(selected).toContainText("核验方式：原文段落定位");
  await expect(selected.getByText("段落已定位", { exact: true })).toBeVisible();
  await expect(selected.getByText("原文匹配", { exact: true })).toHaveCount(0);
  await expect(selected.getByText("原文引文", { exact: true })).toHaveCount(0);
  await expect(selected.locator(".source-observation")).toHaveCount(0);

  const quote = page.getByRole("region", { name: "结论 C1", exact: true });
  await quote.locator("summary").click();
  await expect(quote).toContainText("核验方式：引文匹配");
  await expect(quote.getByText("原文匹配", { exact: true })).toBeVisible();
  const table = page.getByRole("region", { name: "结论 C2", exact: true });
  await table.locator("summary").click();
  await expect(table.locator("blockquote")).toHaveText("0.7");
  await expect(table).toContainText("Test split → Fixed budget → Error (%)");
  await expect(table).toContainText("核验方式：单元格与表头匹配");
  const image = page.getByRole("region", { name: "结论 C3", exact: true });
  await image.locator("summary").click();
  await expect(image.locator("blockquote")).toHaveCount(0);
  await expect(image).toContainText("核验方式：图像观察记录核验");

  await selected.getByRole("button", { name: "查看证据 E4" }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.locator("blockquote")).toHaveText(paragraph);
  await expect(drawer).toContainText("S1.p2");
  await expect(drawer).toContainText("核验方式：原文段落定位");
  await expect(drawer.getByText("原文匹配", { exact: true })).toHaveCount(0);
  await drawer.getByRole("button", { name: "全部证据", exact: true }).click();
  const entry = drawer
    .locator(".evidence-entry")
    .filter({ hasText: "原文段落定位" });
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText("段落已定位");
  await expect(entry.getByText("原文匹配", { exact: true })).toHaveCount(0);
});

test("v3 section selection without a host section_lookup method stays unverified", async ({
  page,
}) => {
  const selected = {
    ...evidence[0],
    selection: "section",
    verification_method: undefined,
  };
  await fixtures(page, {
    ...reading,
    revisions: [
      { ...revision, evidence_index: [selected, ...evidence.slice(1)] },
    ],
  });
  await page.goto("/readings/r3");
  await expect(page.locator(".evidence-count")).toContainText(
    "2 / 3 条证据已核验",
  );
  await page.getByRole("button", { name: "查看证据 E1", exact: true }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByText("原文段落", { exact: true })).toBeVisible();
  await expect(drawer.getByText("未确认核验", { exact: true })).toBeVisible();
  await expect(drawer.getByText("原文匹配", { exact: true })).toHaveCount(0);
  await expect(drawer.getByText("段落已定位", { exact: true })).toHaveCount(0);
});

test("v3 explicit zero figure checks differs from missing coverage and does not affect legacy revisions", async ({
  page,
}) => {
  const legacyEvidence = [
    {
      ...evidence[0],
      evidence_type: undefined,
      verification_method: undefined,
    },
  ];
  await fixtures(page, {
    ...reading,
    revisions: [
      {
        ...revision,
        verification: {
          ...verification,
          figure_checks: {
            total: 0,
            verified: 0,
            complete: true,
            items: [],
            unresolved: [],
          },
        },
      },
      {
        ...revision,
        id: "legacy-v1",
        number: 1,
        evidence_index: legacyEvidence,
        reading_packet: { ...packet, version: 1, claims: [packet.claims[0]] },
        verification: { server_quote_check: "passed" },
      },
    ],
  });
  await page.goto("/readings/r3");
  await expect(page.getByRole("region", { name: "图表核查" })).toContainText(
    "无需核查",
  );
  await page.getByRole("combobox", { name: "解读版本" }).press("ArrowDown");
  await page.getByTitle("版本 1", { exact: true }).click();
  await expect(
    page.getByRole("region", { name: "本版本来源与覆盖" }),
  ).toHaveCount(0);
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const claim = page.getByRole("region", { name: "结论 C1", exact: true });
  await claim.locator("summary").click();
  await expect(claim).toContainText("原文匹配");
  await claim.getByRole("button", { name: "查看证据 E1" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "核验方式：未提供（旧记录）",
  );
  await expect(page.getByRole("dialog").locator("blockquote")).toHaveText(
    evidence[0].quote,
  );
});

test("v3 narrow packet and evidence drawer preserve long source identifiers without horizontal overflow", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await fixtures(page);
  await page.goto("/readings/r3");
  await page.getByRole("tab", { name: "结论与条件", exact: true }).click();
  const image = page.getByRole("region", { name: "结论 C3", exact: true });
  await image.locator("summary").click();
  await expect(image).toContainText("a".repeat(64));
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await image.getByRole("button", { name: "查看证据 E3" }).click();
  await expect(page.getByRole("dialog")).toContainText("观察记录已核验");
  expect(
    await page
      .getByRole("dialog")
      .evaluate((element) => element.scrollWidth <= element.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("v3-image-evidence-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});
