# Paper Reader Web

基于 MemBench 参考工程的 React 18、Ant Design 5、Vite 和 TanStack Query 工作台。使用同源 `/api/v1` 接口和服务端会话，不包含演示账号或预置解读内容。

## 开发与构建

```bash
npm ci
npm run dev
npm run typecheck
npm run build
```

开发服务器将 `/api` 代理到 `http://127.0.0.1:8000`，保留浏览器请求的 Host，供后端检查同源 Origin。生产环境由后端提供 `dist/` 静态资源。

## 接口类型

`src/generated-api.ts` 由后端 OpenAPI 生成，业务页面通过 `src/types.ts` 的别名使用。证据和附件的嵌套列表以项目共享契约为准。

后端导出 `../docs/openapi.json` 后运行：

```bash
npm run generate:api
npm run typecheck
```

变更接口时同步更新契约、生成文件与相关浏览器验证。不要手工修改生成文件。

## 浏览器验证

```bash
PLAYWRIGHT_BROWSERS_PATH=../../paper-reader-runtime/cache/playwright npx playwright install chromium
npm run test:e2e
```

配置通过绝对路径解析相邻 runtime 目录；可用 `PAPER_READER_RUNTIME_DIR` 覆盖。截图、报告与 trace 保存在 `test-runs/frontend/`。可通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` 指定已有 Chromium。

浏览器测试显式拦截 HTTP 接口，验证登录、租户切换、PDF/arXiv 导入、模板、模型配置、证据、修订和后台任务操作。它们不运行生产模型，不代表论文解读质量评测。生产程序不会回退到这些测试数据。

## 依赖兼容约定

React 保持 18.3.1，Ant Design 保持 5.27.4。React Router DOM 7.18.4 使用现有的声明式 `BrowserRouter`，不启用框架或 RSC 模式。Vite 固定 6.4.4。

KaTeX 固定 0.18.2，并在 `rehype-katex` 与 `micromark-extension-math` 下使用定向 override，避免这些包的旧版本范围重新安装存在已知漏洞的 KaTeX。更新数学渲染相关依赖后，应同时验证公式、Markdown 和 `npm audit`，不要运行会降级插件的 `npm audit fix --force`。
