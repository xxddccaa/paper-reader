import type { Evidence } from "./types";
import type { components } from "./generated-api";

// Public chat contract; the nested evidence payload shares the reading renderer.
type Schemas = components["schemas"];
export type ChatStatus = Schemas["ChatMessageOut"]["status"];
export type ChatConversation = Schemas["ConversationOut"];
export type ChatMessage = Omit<Schemas["ChatMessageOut"], "evidence_index"> & {
  evidence_index: Evidence[];
};
export type ChatDetail = Omit<Schemas["ConversationDetail"], "messages"> & {
  messages: ChatMessage[];
};
export type ChatSendResult = Omit<Schemas["ChatMessageCreated"], "user_message" | "assistant_message"> & {
  user_message: ChatMessage;
  assistant_message: ChatMessage;
};
export type ChatRetryResult = Omit<Schemas["ChatRetryCreated"], "assistant_message"> & {
  assistant_message: ChatMessage;
};
