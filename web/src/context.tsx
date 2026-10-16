import { createContext, useContext } from "react";
import type { User, Workspace } from "./types";
export interface WorkspaceContextValue {
  user: User;
  workspace: Workspace;
  workspaces: Workspace[];
}
export const WorkspaceContext = createContext<WorkspaceContextValue | null>(
  null,
);
export function useWorkspace() {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("请选择工作空间");
  return value;
}
