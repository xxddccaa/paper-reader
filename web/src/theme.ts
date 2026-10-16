// web/src/theme.ts
import { theme, type ThemeConfig } from "antd";

export const workbenchTheme: ThemeConfig = {
  algorithm: theme.compactAlgorithm,
  token: {
    colorPrimary: "#156f65",
    colorInfo: "#156f65",
    colorBgLayout: "#f4f6f7",
    borderRadius: 5,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
    fontSize: 13,
  },
  components: {
    Table: { cellPaddingBlock: 11, headerBg: "#f6f8f9" },
    Button: { controlHeight: 32 },
    Input: { controlHeight: 32 },
    Select: { controlHeight: 32 },
  },
};
