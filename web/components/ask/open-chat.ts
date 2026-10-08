/** Open the chat on any page with a message ready to send (e.g. what "Something missing?" on Sources did not know). */
export const OPEN_CHAT_EVENT = "admin2ai:open-chat";

export interface OpenChatDetail {
  text: string;
}

export function openChat(text: string): void {
  window.dispatchEvent(new CustomEvent<OpenChatDetail>(OPEN_CHAT_EVENT, { detail: { text } }));
}
