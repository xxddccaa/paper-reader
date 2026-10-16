import {isMap, isScalar, parseDocument} from 'yaml';

export const LJG_ADAPTER_VERSION = '1.3.0';
export const LJG_PROJECTION_VERSION = 'markdown-to-org-v1';

/** A validation-only syntax projection for the pinned md template. Never rewrites the note. */
export function projectLJGMarkdown(markdown: string): string {
  const normalized = markdown.replace(/\r\n/g, '\n');
  const frontmatter = normalized.match(/^---\n([\s\S]*?)\n---(?:\n|$)/);
  if (!frontmatter) throw new Error('笔记必须以完整的 YAML frontmatter 开始（--- 包围的单行标量字段）。');
  const yaml = frontmatter[1];
  const document = parseDocument(yaml, {strict: true, uniqueKeys: true, prettyErrors: false});
  if (document.errors.length || document.warnings.length) {
    const details = [...document.errors, ...document.warnings].map(item => item.message).join('; ');
    throw new Error(`YAML frontmatter 无效：${details}。含冒号加空格的值请加引号；字段不得重复。`);
  }
  if (!isMap(document.contents) || document.contents.anchor) {
    throw new Error('YAML frontmatter 必须为字段映射，不支持锚点、别名或嵌套结构。');
  }
  const headers: string[] = [];
  const names = new Set<string>();
  for (const pair of document.contents.items) {
    if (!isScalar(pair.key) || typeof pair.key.value !== 'string' || !/^[a-z][a-z_]*$/.test(pair.key.value)) {
      throw new Error('YAML 字段名必须为小写字母和下划线。');
    }
    const key = pair.key.value;
    const value = pair.value;
    if (!isScalar(value) || typeof value.value !== 'string' || value.anchor || pair.key.anchor ||
        /[\x00-\x1f\x7f\x85\u2028\u2029]/.test(value.value) || /#\+(?:begin_example|end_example)/i.test(value.value) ||
        (value.range && /[\r\n]/.test(yaml.slice(value.range[0], value.range[1])))) {
      throw new Error(`YAML 字段 ${key} 必须为单行字符串；日期、含冒号的标题等请加引号，不支持数组、块标量、别名或 Org example 分隔符。`);
    }
    const header = key === 'tags' ? 'filetags' : key;
    if (names.has(header)) throw new Error(`YAML 字段 ${header} 重复（tags 与 filetags 也不能同时提供）。`);
    names.add(header);
    if (!value.value.trim()) throw new Error(`YAML 字段 ${key} 不能为空。`);
    headers.push(`#+${header}: ${value.value}`);
  }

  const body = normalized.slice(frontmatter[0].length).split('\n');
  const projected: string[] = [];
  let fence: {marker: string; length: number} | undefined;
  for (const line of body) {
    // Upstream searches globally, even within example blocks. Do not let literal
    // Org directives/headings masquerade as generated metadata or real sections.
    if (/^\s*#\+/.test(line) || /^\*+\s/.test(line) || /[\r\u2028\u2029]/.test(line) || /#\+(?:begin_example|end_example)/i.test(line)) {
      throw new Error('Markdown 正文含歧义 Org 指令或星号标题；请使用 # 标题、- 列表，围栏内也不得使用 #+ 指令、行首星号标题或任何位置的 Org example 分隔符。');
    }
    if (fence) {
      const end = line.match(/^ {0,3}(`+|~+)[ \t]*$/);
      if (end && end[1][0] === fence.marker && end[1].length >= fence.length) {
        projected.push('#+end_example');
        fence = undefined;
      } else {
        projected.push(line);
      }
      continue;
    }
    const start = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (start) {
      if (start[1][0] === '`' && start[2].includes('`')) throw new Error('Markdown 代码围栏的语言标记不能包含反引号。');
      fence = {marker: start[1][0], length: start[1].length};
      projected.push('#+begin_example');
      continue;
    }
    const heading = line.match(/^ {0,3}(#{1,6})[ \t]+(.*)$/);
    projected.push(heading ? `${'*'.repeat(heading[1].length)} ${heading[2]}` : line);
  }
  if (fence) throw new Error('Markdown 代码围栏未闭合，无法校验图示。');
  return `${headers.join('\n')}\n\n${projected.join('\n')}`;
}
