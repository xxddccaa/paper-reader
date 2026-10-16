import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {projectLJGMarkdown} from '../src/ljg-projection.js';

const filename = '20260820T091641--paper-技能留下动作关系__paper.md';
const fixture = new URL('./fixtures/ljg-markdown/', import.meta.url);
const note = readFileSync(new URL(filename, fixture), 'utf8');
const map = readFileSync(new URL('paper-map.md', fixture), 'utf8');
const sha256 = (text: string) => createHash('sha256').update(text).digest('hex');

function validate(markdown = note, paperMap = map, name = filename) {
  const directory = mkdtempSync(join(tmpdir(), 'ljg-markdown-test-'));
  try {
    const notePath = join(directory, name);
    const mapPath = join(directory, 'paper-map.md');
    writeFileSync(notePath, markdown);
    writeFileSync(mapPath, paperMap);
    const process = spawnSync(globalThis.process.execPath, [
      new URL('../dist/validate-ljg.js', import.meta.url).pathname, notePath, '--map', mapPath,
    ], {encoding: 'utf8'});
    assert.equal(process.stderr, '');
    const report = JSON.parse(process.stdout);
    assert.equal(process.status, report.ok ? 0 : 1);
    assert.equal(readFileSync(notePath, 'utf8'), markdown, 'validation must not rewrite native note');
    assert.equal(readFileSync(mapPath, 'utf8'), paperMap, 'validation must not rewrite map');
    assert.equal(report.adapter.note_sha256, sha256(markdown));
    assert.equal(report.adapter.map_sha256, sha256(paperMap));
    return report;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

test('real pinned validator accepts valid Markdown/map and binds the unchanged source and projection', () => {
  const report = validate();
  assert.equal(report.ok, true, report.errors.join('\n'));
  assert.deepEqual(report.errors, []);
  assert.equal(report.stats.top_headings, 3);
  assert.equal(report.stats.paper_map_version, '3');
  assert.equal(report.stats.evidence_count, 3);
  assert.equal(report.adapter.version, '1.3.0');
  assert.equal(report.adapter.projection, 'markdown-to-org-v1');
  assert.equal(report.adapter.upstream_validator_executed, true);
  assert.equal(report.adapter.projection_sha256, sha256(projectLJGMarkdown(note)));
});

test('strict YAML rejects malformed, duplicate and unsupported metadata without running upstream', async t => {
  const cases: Record<string, string> = {
    'unquoted colon in title': note.replace('"Demystifying Skills"', 'BitFit: Simple Fine-tuning'),
    'duplicate source': note.replace('authors:', 'source: "https://example.com/other"\nauthors:'),
    'duplicate title': note.replace('authors:', 'title: "another title"\nauthors:'),
    'duplicate mapped tags': note.replace('authors:', 'filetags: "paper"\nauthors:'),
    'list date from unfilled upstream template': note.replace('"[2026-08-20 Thu 09:16]"', '[2026-08-20 Thu 09:16]'),
    'nested metadata': note.replace('"Demystifying Skills"', '{nested: title}'),
    'block scalar': note.replace('"Demystifying Skills"', '|\n  Demystifying Skills'),
    'alias': note.replace('"Demystifying Skills"', '*missing'),
    'anchor': note.replace('"Demystifying Skills"', '&anchor "Demystifying Skills"'),
    'unknown YAML tag': note.replace('"Demystifying Skills"', '!custom "Demystifying Skills"'),
    'empty required metadata': note.replace('"Demystifying Skills"', '""'),
    'injected header through quoted newline': note.replace('"Demystifying Skills"', '"Title\\n#+source: https://example.com"'),
    'inline Org delimiter in metadata': note.replace('"Demystifying Skills"', '"Title #+begin_example"'),
    'escaped Org delimiter in metadata': note.replace('"Demystifying Skills"', '"Title #\\x2bend_example"'),
  };
  for (const [name, broken] of Object.entries(cases)) await t.test(name, () => {
    const report = validate(broken);
    assert.equal(report.ok, false);
    assert.match(report.errors.join('\n'), /YAML/);
    assert.equal(report.adapter.upstream_validator_executed, false);
    assert.equal(report.adapter.projection_sha256, null);
  });
});

test('upstream required metadata, source and identifier checks remain mandatory', async t => {
  const cases: Array<[string, string, string, RegExp]> = [
    ['missing authors', note.replace(/^authors:.*\n/m, ''), filename, /authors/],
    ['missing source', note.replace(/^source:.*\n/m, ''), filename, /source/],
    ['relative source', note.replace('https://arxiv.org/abs/2608.14036', 'source.pdf'), filename, /source/],
    ['map source mismatch', note.replace('https://arxiv.org/abs/2608.14036', 'https://example.com/other'), filename, /原文位置不一致/],
    ['identifier mismatch', note, filename.replace('091641', '091642'), /IDENTIFIER/],
  ];
  for (const [label, broken, name, pattern] of cases) await t.test(label, () => {
    const report = validate(broken, map, name);
    assert.equal(report.ok, false);
    assert.equal(report.adapter.upstream_validator_executed, true);
    assert.match(report.errors.join('\n'), pattern);
  });
});

test('exact note anchors and real narrative review records remain mandatory', async t => {
  for (const [name, brokenMap, pattern] of [
    ['English evidence anchor', map.replace('正文对应表述：每一百次多完成六次', '正文对应表述：six more tasks per hundred'), /对应表述没有出现在正文/],
    ['paraphrased early anchor', map.replace('前两节的问题与贡献表述：结账请求', '前两节的问题与贡献表述：结算订单'), /前两个一级标题/],
    ['missing real review record', map.replace('NARRATIVE_PASS', ''), /NARRATIVE_PASS/],
  ] as const) await t.test(name, () => {
    const report = validate(note, brokenMap);
    assert.equal(report.ok, false);
    assert.equal(report.adapter.upstream_validator_executed, true);
    assert.match(report.errors.join('\n'), pattern);
  });
});

test('fences preserve literal Markdown headings and enforce actual upstream diagram width', () => {
  const addDiagram = (diagram: string) => note.replace('# 技能库扩大以后', `${diagram}\n\n# 技能库扩大以后`);
  const diagram = '```text\n# literal heading inside diagram\ninput -> output\n```';
  const accepted = validate(addDiagram(diagram));
  assert.equal(accepted.ok, true, accepted.errors.join('\n'));
  assert.equal(accepted.stats.top_headings, 3);
  assert.equal(accepted.stats.example_blocks, 1);
  const oversized = validate(addDiagram(`~~~ascii\n${'中'.repeat(41)}\n~~~`));
  assert.equal(oversized.ok, false);
  assert.equal(oversized.stats.max_diagram_width, 82);
  assert.match(oversized.errors.join('\n'), /超过 80 显示列/);
  const unterminated = validate(addDiagram('```text\ninput -> output'));
  assert.equal(unterminated.ok, false);
  assert.match(unterminated.errors.join('\n'), /未闭合/);
});

test('raw Org cannot spoof metadata, sections or close an example block', async t => {
  for (const injection of [
    '#+source: https://example.com', '* Pretend section', '```\n#+end_example\n* Pretend section\n```',
    'text\u2028#+source: https://example.com', 'text\u2029* Pretend section',
    `\`\`\`\ninline #+end_example\n${'中'.repeat(50)}\n\`\`\``,
    'inline #+BEGIN_EXAMPLE', 'inline #+EnD_ExampleS',
  ]) {
    await t.test(injection, () => {
      const report = validate(`${note}\n${injection}\n`);
      assert.equal(report.ok, false);
      assert.equal(report.adapter.upstream_validator_executed, false);
      assert.match(report.errors.join('\n'), /歧义 Org/);
    });
  }
});

test('CRLF input hashes bind original bytes while projection uses normalized newlines', () => {
  const crlf = note.replaceAll('\n', '\r\n');
  const report = validate(crlf);
  assert.equal(report.ok, true, report.errors.join('\n'));
  assert.notEqual(report.adapter.note_sha256, sha256(note));
  assert.equal(report.adapter.projection_sha256, sha256(projectLJGMarkdown(note)));
});
