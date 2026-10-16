import React from "react";
import ReactDOM from "react-dom/client";
import "antd/dist/reset.css";
import "katex/dist/katex.min.css";
import "./styles.css";
import { AppProviders } from "./AppProviders";
import Workbench from "./Workbench";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AppProviders>
      <Workbench />
    </AppProviders>
  </React.StrictMode>,
);
