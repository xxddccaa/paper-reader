import {chromium, expect} from '../web/node_modules/@playwright/test/index.mjs';
import {writeFile} from 'node:fs/promises';
import {join} from 'node:path';

const base = process.env.BROWSER_SMOKE_BASE;
const output = process.env.BROWSER_SMOKE_OUTPUT;
if (!base || !output || !process.env.BROWSER_SMOKE_PASSWORD) throw new Error('Run through browser_live.py');
const browser = await chromium.launch({headless: true});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(base);
  await page.getByLabel('用户名', {exact: true}).fill(process.env.BROWSER_SMOKE_USERNAME);
  await page.getByLabel('密码', {exact: true}).fill(process.env.BROWSER_SMOKE_PASSWORD);
  await page.getByRole('button', {name: '登录', exact: true}).click();
  await expect(page.getByRole('heading', {name: '论文库', exact: true})).toBeVisible();
  const response = await page.request.get(base + '/api/v1/workspaces');
  expect(response.status()).toBe(200);
  const workspaces = await response.json();
  expect(workspaces).toHaveLength(1);
  const wid = workspaces[0].id;
  const detailResponse = await page.request.get(`${base}/api/v1/workspaces/${wid}/readings/${process.env.BROWSER_SMOKE_READING}`);
  expect(detailResponse.status()).toBe(200);
  const detail = await detailResponse.json();
  await page.goto(`${base}/readings/${detail.id}`);
  await expect(page.getByRole('heading', {name: detail.paper_title, exact: true})).toBeVisible();
  const published = Boolean(detail.current_revision_id);
  const current = detail.revisions.find(revision => revision.id === detail.current_revision_id);
  const stageRecords = detail.jobs[0]?.stage_records ?? [];
  const packet = current?.reading_packet;
  let packetRendered = false;
  if (stageRecords.length) {
    const stages = page.getByRole('region', {name: '任务阶段记录', exact: true});
    await expect(stages).toBeVisible();
    await expect(stages.locator('ol > li')).toHaveCount(stageRecords.length);
  }
  if (published) {
    await expect(page.getByRole('tab', {name: '精华版', exact: true})).toBeVisible();
    await expect(page.locator('.markdown').first()).not.toBeEmpty();
    if ([1,2].includes(packet?.version) && packet.claims?.length) {
      await page.getByRole('tab', {name: '结论与条件', exact: true}).click();
      const claims = page.getByRole('region', {name: '结构化结论与条件', exact: true});
      await expect(claims).toBeVisible();
      await expect(claims.locator('.packet-claim')).toHaveCount(packet.claims.length);
      await expect(claims).toContainText(packet.claims[0].statement);
      await claims.locator('details').first().locator('summary').click();
      if((packet.claims[0].evidence[0].evidence_type??'text_quote')==='text_quote')await expect(claims.locator('blockquote').first()).toContainText(packet.claims[0].evidence[0].quote);
      await page.screenshot({path: join(output, 'real-reading-packet.png'), fullPage: true, animations: 'disabled'});
      packetRendered = true;
      await page.getByRole('tab', {name: '精华版', exact: true}).click();
    }
  }
  await page.screenshot({path: join(output, 'real-reading-desktop.png'), fullPage: true, animations: 'disabled'});
  await page.setViewportSize({width: 390, height: 844});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({path: join(output, 'real-reading-mobile.png'), fullPage: true, animations: 'disabled'});
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByRole('button', {name: '退出登录'}).click();
  await expect(page.getByRole('heading', {name: '登录工作空间'})).toBeVisible();
  expect((await page.request.get(base + '/api/v1/auth/me')).status()).toBe(401);
  expect(errors).toEqual([]);
  const report = {ok: true, fixtures: false, published_result_rendered: published, reading_id: detail.id,
    workflow_mode: detail.workflow_mode ?? 'native', stage_count: stageRecords.length,
    reading_packet_rendered: packetRendered, claim_count: packet?.claims?.length ?? 0,
    status: detail.jobs[0]?.status, checks: ['real login', 'tenant membership', 'reading API/route', 'desktop', 'mobile overflow', 'logout revocation', 'no browser exceptions']};
  await writeFile(join(output, 'browser-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
