import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Collapse,
  Drawer,
  Grid,
  Input,
  Select,
  Space,
  Spin,
  Tag,
} from "antd";
import {
  CommentOutlined,
  PlusOutlined,
  SendOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  api,
  ApiError,
  post,
  resourceUrl,
  safeSourceUrl,
  workspacePath,
} from "./api";
import { useWorkspace } from "./context";
import { ErrorNotice } from "./common";
import Markdown from "./Markdown";
import EvidenceContent, { EvidenceLabels } from "./EvidenceContent";
import AgentTaskProgress from "./AgentTaskProgress";
import type { Evidence } from "./types";
import type {
  ChatConversation,
  ChatDetail,
  ChatMessage,
  ChatRetryResult,
  ChatSendResult,
} from "./chat-types";

const requestId = () => {
  // getRandomValues also works on the deployed HTTP intranet origin.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
};
const isActive = (message: ChatMessage) =>
  ["queued", "running"].includes(message.status);
const mergeMessages = (current: ChatMessage[], incoming: ChatMessage[]) => {
  const byId = new Map(current.map((item) => [item.id, item]));
  incoming.forEach((item) => byId.set(item.id, item));
  return [...byId.values()].sort((a, b) => a.sequence - b.sequence);
};
const statusLabels: Record<string, string> = {
  queued: "等待回答",
  running: "生成中 · 引用待核验",
  succeeded: "回答完成",
  partial: "部分内容待核验",
  failed: "回答未完成",
  cancelled: "已停止",
  waiting: "待继续 · 草稿待核验",
};

function AnswerVerification({ message }: { message: ChatMessage }) {
  if (isActive(message)) return null;
  const record = message.verification;
  const agent = record.execution_engine === "pi-agent-v1";
  const reviews = (record.reviews ?? {}) as Record<string, { passed?: boolean }>;
  const quote = (record.quote_match ?? (agent ? record.evidence_check : undefined)) as { passed?: boolean } | undefined;
  const review = (record.independent_review ?? (agent ? reviews.product : undefined)) as
    | { passed?: boolean; status?: string }
    | undefined;
  const issues = Array.isArray(record.issues)
    ? record.issues.filter(
        (value): value is string => typeof value === "string",
      )
    : [];
  const limitations = Array.isArray(record.limitations)
    ? record.limitations.filter(
        (value): value is string => typeof value === "string",
      )
    : [];
  const notes = [...new Set([...issues, ...limitations])];
  if (!quote && !review && !notes.length) return null;
  return (
    <div className="paper-chat-verification" aria-label="回答核查">
      <div>
        <span>引文匹配</span>
        <Tag color={quote?.passed ? "success" : "warning"}>
          {!message.evidence_index.length
            ? "未引用原文"
            : quote?.passed
              ? "通过"
              : quote
                ? "待核验"
                : "未提供记录"}
        </Tag>
      </div>
      {agent && (
        <div>
          <span>事实与条件复核</span>
          <Tag color={reviews.fact?.passed ? "success" : "warning"}>
            {reviews.fact?.passed ? "通过" : "尚未完成"}
          </Tag>
        </div>
      )}
      <div>
        <span>内容复核</span>
        <Tag color={review?.passed ? "success" : "warning"}>
          {review?.passed
            ? "通过"
            : review?.status === "not_run" || !review
              ? "尚未完成"
              : "需要核查"}
        </Tag>
      </div>
      {!!notes.length && (
        <Collapse
          ghost
          size="small"
          items={[
            {
              key: "notes",
              label: "核查说明与资料范围",
              children: (
                <ul>
                  {notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

export default function PaperChat({
  readingId,
  revisionId,
  currentRevisionId,
  revisions,
}: {
  readingId: string;
  revisionId: string;
  currentRevisionId: string | null;
  revisions: { id: string; number: number }[];
}) {
  const { workspace, user } = useWorkspace();
  const screens = Grid.useBreakpoint();
  const queryClient = useQueryClient();
  const storageKey = `paper-chat:${user.id}:${workspace.id}:${readingId}`;
  const [selected, setSelected] = useState<string | null>(() => {
    try {
      return localStorage.getItem(storageKey);
    } catch {
      return null;
    }
  });
  const [draftRevisionId, setDraftRevisionId] = useState<string>();
  const [conversation, setConversation] = useState<ChatConversation>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [olderLoading, setOlderLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>();
  const [input, setInput] = useState("");
  const [open, setOpen] = useState(false);
  const [progress, setProgress] = useState("");
  const [reconnecting, setReconnecting] = useState(false);
  const [selectedEvidence, setSelectedEvidence] = useState<{
    message: ChatMessage;
    evidence: Evidence;
  }>();
  const selectedRef = useRef(selected);
  const pendingRef = useRef(false);
  const pendingRequest = useRef<{
    content: string;
    id: string;
    conversationId: string | null;
  }>();
  const listRef = useRef<HTMLDivElement>(null);
  const followBottom = useRef(true);
  selectedRef.current = selected;
  const path = (suffix: string) => workspacePath(workspace.id, suffix);
  const listKey = [
    "workspace",
    workspace.id,
    "private-chat",
    user.id,
    readingId,
  ];
  const conversations = useQuery({
    queryKey: listKey,
    queryFn: () =>
      api<ChatConversation[]>(path(`/readings/${readingId}/conversations`)),
  });
  const refreshList = () => {
    void queryClient.invalidateQueries({ queryKey: listKey });
  };
  const choose = (id: string | null, newRevisionId?: string) => {
    setDraftRevisionId(newRevisionId);
    setError(undefined);
    setProgress("");
    setSelected(id);
    selectedRef.current = id;
    setConversation(undefined);
    setMessages([]);
    setNextBefore(null);
    setInput("");
    pendingRequest.current = undefined;
    followBottom.current = true;
    try {
      if (id) localStorage.setItem(storageKey, id);
      else localStorage.removeItem(storageKey);
    } catch {
      /* Conversation history still remains on the server. */
    }
  };
  useEffect(() => {
    setDraftRevisionId(undefined);
  }, [revisionId]);
  useEffect(() => {
    if (!selected) {
      setLoading(false);
      return;
    }
    const abort = new AbortController();
    setLoading(true);
    api<ChatDetail>(workspacePath(workspace.id, `/conversations/${selected}`), {
      signal: abort.signal,
    })
      .then((detail) => {
        if (abort.signal.aborted) return;
        setConversation(detail.conversation);
        setMessages((items) => mergeMessages(detail.messages, items));
        setNextBefore(detail.next_before);
      })
      .catch((cause) => {
        if (!abort.signal.aborted) setError(cause);
      })
      .finally(() => {
        if (!abort.signal.aborted) setLoading(false);
      });
    return () => abort.abort();
  }, [selected, workspace.id]);

  const activeMessage = messages.find(
    (item) => item.role === "assistant" && isActive(item),
  );
  const activeId = activeMessage?.id;
  const activeJob = activeMessage?.job_id;
  useEffect(() => {
    if (!selected || !activeId || !activeJob) {
      setReconnecting(false);
      return;
    }
    let disposed = false;
    let finished = false;
    let source: EventSource | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let lastEventId = "";
    let replay = "";
    let caughtUp = false;
    // A fresh page shows the HTTP snapshot while replay reconstructs its event cursor.
    // Never append a full replay to an already populated snapshot.
    const initialContent = activeMessage.content;
    const eventPath = resourceUrl(
      workspace.id,
      `/conversations/${selected}/messages/${activeId}/events`,
    );
    const update = (patch: Partial<ChatMessage>) =>
      setMessages((items) =>
        items.map((item) =>
          item.id === activeId && item.job_id === activeJob
            ? { ...item, ...patch }
            : item,
        ),
      );
    const complete = (message: ChatMessage) => {
      if (disposed || message.id !== activeId) return;
      finished = true;
      source?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      setMessages((items) => mergeMessages(items, [message]));
      setProgress("");
      setReconnecting(false);
      refreshList();
    };
    const reconcile = async () => {
      try {
        const detail = await api<ChatDetail>(
          workspacePath(workspace.id, `/conversations/${selected}`),
        );
        if (disposed || finished) return;
        const message = detail.messages.find((item) => item.id === activeId);
        if (message && (!isActive(message) || message.job_id !== activeJob))
          complete(message);
        else if (message)
          update({ agent_task: message.agent_task, execution_engine: message.execution_engine });
      } catch (cause) {
        if (disposed || finished) return;
        if (
          cause instanceof ApiError &&
          [401, 403, 404].includes(cause.status)
        ) {
          finished = true;
          source?.close();
          if (reconnectTimer) clearTimeout(reconnectTimer);
          setError(cause);
          setReconnecting(false);
        }
      }
    };
    const connect = () => {
      if (disposed || finished) return;
      source = new EventSource(
        eventPath +
          (lastEventId ? `?after=${encodeURIComponent(lastEventId)}` : ""),
      );
      source.onopen = () => {
        if (!disposed) setReconnecting(false);
      };
      const event = (kind: string) => (raw: MessageEvent<string>) => {
        if (disposed || finished) return;
        // Replayed IDs are monotonically increasing for the current job only.
        if (
          raw.lastEventId &&
          lastEventId &&
          /^\d+$/.test(raw.lastEventId) &&
          /^\d+$/.test(lastEventId) &&
          BigInt(raw.lastEventId) <= BigInt(lastEventId)
        )
          return;
        let data: {
          message_id?: string;
          text?: string;
          content?: string;
          message?: ChatMessage | string;
          agent_task?: ChatMessage["agent_task"];
        };
        try {
          data = JSON.parse(raw.data);
        } catch {
          return;
        }
        if (data.message_id && data.message_id !== activeId) return;
        if (raw.lastEventId) lastEventId = raw.lastEventId;
        if (kind === "delta") {
          replay += data.text ?? "";
          caughtUp ||=
            !initialContent || replay.length >= initialContent.length;
          if (caughtUp) update({ content: replay, status: "running" });
        } else if (kind === "replace") {
          replay = data.content ?? "";
          caughtUp = true;
          update({ content: replay, status: "running" });
        } else if (kind === "progress") {
          if (typeof data.message === "string") setProgress(data.message);
          if (data.agent_task) update({ agent_task: data.agent_task });
        } else if (data.message && typeof data.message !== "string")
          complete(data.message);
      };
      [
        "delta",
        "replace",
        "progress",
        "completed",
        "failed",
        "cancelled",
        "waiting",
      ].forEach((kind) =>
        source!.addEventListener(kind, event(kind) as EventListener),
      );
      source.onerror = () => {
        source?.close();
        if (disposed || finished) return;
        setReconnecting(true);
        void reconcile();
        reconnectTimer = setTimeout(connect, 1500);
      };
    };
    connect();
    const poll = setInterval(() => {
      void reconcile();
    }, 5000);
    return () => {
      disposed = true;
      source?.close();
      clearInterval(poll);
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
    // Content updates must not restart the stream or reset its replay cursor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, activeId, activeJob, workspace.id]);

  useEffect(() => {
    if (followBottom.current && listRef.current)
      listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [messages, progress, open]);

  const loadOlder = async () => {
    if (!selected || nextBefore === null) return;
    const id = selected;
    const scroll = listRef.current;
    const oldHeight = scroll?.scrollHeight ?? 0;
    setOlderLoading(true);
    try {
      const detail = await api<ChatDetail>(
        path(`/conversations/${id}?before=${nextBefore}`),
      );
      if (selectedRef.current !== id) return;
      followBottom.current = false;
      setMessages((items) => mergeMessages(detail.messages, items));
      setNextBefore(detail.next_before);
      requestAnimationFrame(() => {
        if (scroll) scroll.scrollTop += scroll.scrollHeight - oldHeight;
      });
    } catch (cause) {
      if (selectedRef.current === id) setError(cause);
    } finally {
      setOlderLoading(false);
    }
  };
  const send = async () => {
    const content = input.trim();
    if (
      !content ||
      pendingRef.current ||
      activeMessage ||
      loading ||
      content.length > 10000
    )
      return;
    pendingRef.current = true;
    setPending(true);
    setError(undefined);
    try {
      let id = selected;
      if (!id) {
        const created = await post<ChatConversation>(
          path(`/readings/${readingId}/conversations`),
          { revision_id: draftRevisionId ?? revisionId },
        );
        id = created.id;
        setConversation(created);
        setSelected(id);
        selectedRef.current = id;
        try {
          localStorage.setItem(storageKey, id);
        } catch {
          /* See choose(). */
        }
        refreshList();
      }
      if (
        !pendingRequest.current ||
        pendingRequest.current.content !== content ||
        pendingRequest.current.conversationId !== id
      ) {
        pendingRequest.current = {
          content,
          id: requestId(),
          conversationId: id,
        };
      }
      const result = await post<ChatSendResult>(
        path(`/conversations/${id}/messages`),
        {
          content,
          client_request_id: pendingRequest.current.id,
        },
      );
      if (selectedRef.current === id) {
        setMessages((items) =>
          mergeMessages(items, [result.user_message, result.assistant_message]),
        );
        setInput("");
        setProgress("");
        followBottom.current = true;
      }
      pendingRequest.current = undefined;
      refreshList();
    } catch (cause) {
      setError(cause);
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };
  const act = async (message: ChatMessage, action: "cancel" | "retry" | "continue") => {
    if (!selected || pendingRef.current) return;
    const id = selected;
    pendingRef.current = true;
    setPending(true);
    setError(undefined);
    try {
      const result = await post<ChatMessage | ChatRetryResult>(
        path(`/conversations/${id}/messages/${message.id}/${action}`),
      );
      if (selectedRef.current === id) {
        const updated =
          "assistant_message" in result ? result.assistant_message : result;
        setMessages((items) => mergeMessages(items, [updated]));
        setProgress(
          action === "cancel" && isActive(updated) ? "正在停止回答…" : "",
        );
      }
    } catch (cause) {
      if (selectedRef.current === id) setError(cause);
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };
  const latestAssistant = [...messages]
    .reverse()
    .find((item) => item.role === "assistant");
  const pinnedId = conversation?.revision_id ?? draftRevisionId ?? revisionId;
  const pinnedNumber = revisions.find((item) => item.id === pinnedId)?.number;
  const panel = (
    <section className="paper-chat panel" aria-label="追问论文">
      <div className="paper-chat-heading">
        <div>
          <h2>
            <CommentOutlined aria-hidden /> 追问论文
          </h2>
          <span className="muted">对话仅自己可见</span>
        </div>
        <Button
          size="small"
          icon={<PlusOutlined aria-hidden />}
          onClick={() => choose(null)}
          disabled={pending}
        >
          新话题
        </Button>
      </div>
      <div className="paper-chat-context">
        <Select
          aria-label="历史对话"
          value={selected ?? undefined}
          placeholder="新对话"
          loading={conversations.isPending}
          disabled={pending}
          onChange={(id) => choose(id)}
          options={(Array.isArray(conversations.data)
            ? conversations.data
            : []
          ).map((item) => ({ value: item.id, label: item.title }))}
          notFoundContent="还没有历史对话"
        />
        <div className="paper-chat-version">
          基于{pinnedNumber ? `版本 ${pinnedNumber}` : "所选版本"}
          {conversation && pinnedId !== currentRevisionId && (
            <Button
              size="small"
              type="link"
              onClick={() => choose(null, currentRevisionId ?? revisionId)}
              disabled={pending}
            >
              基于新版本开始对话
            </Button>
          )}
        </div>
        {conversation && pinnedId !== revisionId && (
          <span className="muted">
            对话保留原版本上下文，切换阅读版本不会改变已有对话。
          </span>
        )}
      </div>
      <ErrorNotice error={error || conversations.error} />
      <div
        className="paper-chat-messages"
        ref={listRef}
        onScroll={() => {
          const list = listRef.current;
          if (list)
            followBottom.current =
              list.scrollHeight - list.scrollTop - list.clientHeight < 80;
        }}
        aria-label="对话内容"
        aria-busy={loading}
      >
        {loading ? (
          <div className="paper-chat-loading">
            <Spin />
            <span>正在加载对话</span>
          </div>
        ) : (
          <>
            {nextBefore !== null && (
              <Button
                block
                type="text"
                onClick={() => void loadOlder()}
                loading={olderLoading}
              >
                查看更早的消息
              </Button>
            )}
            {!messages.length && (
              <div className="paper-chat-empty">
                <h3>继续理解这篇论文</h3>
                <p>
                  可以解释方法、接着上一问举例，也可以查找相关论文进行比较。回答会附上可核查的来源。
                </p>
                <Button
                  onClick={() =>
                    setInput("这篇论文的方法解决了什么问题？请结合原文解释。")
                  }
                >
                  解释核心方法
                </Button>
              </div>
            )}
            {messages.map((item) => (
              <article
                className={`paper-chat-message paper-chat-${item.role}`}
                key={item.id}
                data-message-id={item.id}
              >
                <div className="paper-chat-message-heading">
                  <strong>{item.role === "user" ? "你" : "论文助手"}</strong>
                  {item.role === "assistant" && (
                    <Tag
                      color={
                        isActive(item)
                          ? "processing"
                          : item.status === "succeeded"
                            ? "success"
                            : "warning"
                      }
                    >
                      {statusLabels[item.status]}
                    </Tag>
                  )}
                </div>
                {item.role === "user" ? (
                  <p className="paper-chat-question">{item.content}</p>
                ) : (
                  <>
                    {item.content && item.status !== "succeeded" && (
                      <p className="paper-chat-draft-note" role="note">
                        草稿 · 待核验
                        {item.execution_engine === "pi-agent-v1" && "。通过核查后才会加入后续对话上下文。"}
                      </p>
                    )}
                    {item.content ? (
                      <Markdown
                        content={item.content}
                        evidence={item.evidence_index}
                        onEvidence={(evidence) =>
                          setSelectedEvidence({ message: item, evidence })
                        }
                      />
                    ) : (
                      <p className="muted">
                        {isActive(item)
                          ? "正在整理问题与原文…"
                          : "本次没有生成可显示的回答。"}
                      </p>
                    )}
                    {item.execution_engine === "pi-agent-v1" && (
                      <AgentTaskProgress task={item.agent_task} status={item.status} compact />
                    )}
                    {isActive(item) && (
                      <div className="paper-chat-progress" role="status">
                        <Spin size="small" />
                        <span>
                          {reconnecting
                            ? "连接中断，正在恢复；回答仍在继续。"
                            : progress || "正在阅读、回答并核验引用…"}
                        </span>
                      </div>
                    )}
                    <AnswerVerification message={item} />
                    {item.error && item.error !== item.agent_task?.issue && (
                      <Alert type="warning" message={item.error} showIcon />
                    )}
                    {!isActive(item) && item.evidence_index.length > 0 && (
                      <div className="paper-chat-sources">
                        <span className="muted">回答来源</span>
                        {item.evidence_index.map((evidence) => (
                          <Button
                            size="small"
                            key={evidence.id}
                            onClick={() =>
                              setSelectedEvidence({ message: item, evidence })
                            }
                          >
                            {evidence.id} ·{" "}
                            {evidence.kind === "external"
                              ? "外部资料"
                              : "论文原文"}{" "}
                            · {evidence.verified ? "已核验" : "待核验"}
                          </Button>
                        ))}
                      </div>
                    )}
                    {!activeMessage &&
                      item.id === latestAssistant?.id &&
                      ((item.agent_task?.can_continue && ["waiting", "failed", "cancelled"].includes(item.status)) ||
                        (item.execution_engine !== "pi-agent-v1" && ["failed", "partial", "cancelled"].includes(item.status))) && (
                        <Button
                          size="small"
                          type="primary"
                          onClick={() => void act(item, "continue")}
                          loading={pending}
                        >
                          {item.execution_engine === "pi-agent-v1" ? "继续回答" : "交给 Agent 继续"}
                        </Button>
                      )}
                    {!activeMessage &&
                      item.id === latestAssistant?.id &&
                      item.execution_engine !== "pi-agent-v1" &&
                      ["failed", "partial", "cancelled"].includes(
                        item.status,
                      ) && (
                        <Button
                          size="small"
                          onClick={() => void act(item, "retry")}
                          loading={pending}
                        >
                          重新回答
                        </Button>
                      )}
                  </>
                )}
              </article>
            ))}
          </>
        )}
      </div>
      <form
        className="paper-chat-compose"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <Input.TextArea
          aria-label="追问内容"
          placeholder="继续追问这篇论文…"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          autoSize={{ minRows: 2, maxRows: 6 }}
          maxLength={10000}
          disabled={pending || loading}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <div className="paper-chat-compose-actions">
          <span className="muted">Enter 发送 · Shift + Enter 换行</span>
          {activeMessage ? (
            <Button
              icon={<StopOutlined aria-hidden />}
              onClick={() => void act(activeMessage, "cancel")}
              loading={pending}
            >
              停止回答
            </Button>
          ) : (
            <Button
              type="primary"
              htmlType="submit"
              icon={<SendOutlined aria-hidden />}
              disabled={!input.trim() || loading}
              loading={pending}
            >
              发送
            </Button>
          )}
        </div>
      </form>
    </section>
  );
  const cited = selectedEvidence?.evidence;
  return (
    <>
      {screens.lg ? (
        panel
      ) : (
        <>
          <Button
            type="primary"
            className="paper-chat-launcher"
            icon={<CommentOutlined aria-hidden />}
            onClick={() => setOpen(true)}
          >
            追问论文{activeMessage ? " · 回答中" : ""}
          </Button>
          <Drawer
            title="追问论文"
            aria-label="追问论文"
            open={open}
            onClose={() => setOpen(false)}
            width="min(100vw, 480px)"
            className="paper-chat-drawer"
            destroyOnHidden={false}
          >
            {panel}
          </Drawer>
        </>
      )}
      <Drawer
        title={cited ? `回答证据 ${cited.id}` : "回答证据"}
        aria-label={cited ? `回答证据 ${cited.id}` : "回答证据"}
        open={!!cited}
        onClose={() => setSelectedEvidence(undefined)}
        width={540}
      >
        {cited && selectedEvidence && (
          <>
            <Space wrap className="block-gap">
              <Tag color={cited.kind === "external" ? "cyan" : "blue"}>
                {cited.kind === "external" ? "外部资料" : "论文原文"}
              </Tag>
              <Tag color={cited.verified ? "success" : "warning"}>
                {cited.verified ? "已核验" : "待核验"}
              </Tag>
              <EvidenceLabels item={cited} />
            </Space>
            <h3>{cited.source_title}</h3>
            <EvidenceContent item={cited} />
            {(cited.section || cited.section_id || cited.page) && (
              <p className="muted">
                {cited.section || cited.section_id}
                {cited.page ? ` · 第 ${cited.page} 页` : ""}
              </p>
            )}
            <Space wrap>
              {safeSourceUrl(cited.source_url) && (
                <Button
                  href={safeSourceUrl(cited.source_url)}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看来源网页
                </Button>
              )}
              <Button
                href={resourceUrl(
                  workspace.id,
                  `/conversations/${selectedEvidence.message.conversation_id}/messages/${selectedEvidence.message.id}/sources/${encodeURIComponent(cited.source_id)}`,
                )}
                target="_blank"
                rel="noreferrer"
              >
                打开保存的原文
              </Button>
            </Space>
          </>
        )}
      </Drawer>
    </>
  );
}
