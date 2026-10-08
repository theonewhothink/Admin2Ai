"use client";

import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { isActive } from "@/components/shell/nav-items";
import { ChatClient } from "./ChatClient";
import { OPEN_CHAT_EVENT, type OpenChatDetail } from "./open-chat";
import styles from "./drawer.module.css";

/**
 * The chat on every page: a button in the corner (on phones, just above the bottom bar) opens a panel with the
 * same chat as the Ask page. The conversation stays while the owner moves between pages. A page can open it with
 * a message ready to send (openChat). The Ask page has the chat already.
 */
export function ChatDrawer({ examples }: { examples: string[] }) {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [used, setUsed] = useState(false); // mount the chat on first open, then keep it
  const [draft, setDraft] = useState<{ text: string; id: number } | undefined>(undefined);
  const panel = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const onAsk = isActive(pathname, "/ask");

  useEffect(() => {
    const onOpen = (e: Event) => {
      const text = (e as CustomEvent<OpenChatDetail>).detail?.text ?? "";
      setUsed(true);
      setOpen(true);
      setDraft({ text, id: Date.now() });
    };
    window.addEventListener(OPEN_CHAT_EVENT, onOpen);
    return () => window.removeEventListener(OPEN_CHAT_EVENT, onOpen);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        window.setTimeout(() => opener.current?.focus(), 0);
      }
    };
    window.addEventListener("keydown", onKey);
    const t = window.setTimeout(() => panel.current?.querySelector<HTMLInputElement>("input:not([type=password])")?.focus(), 50);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.clearTimeout(t);
    };
  }, [open]);

  if (onAsk) return null;

  return (
    <>
      {!open ? (
        <button
          ref={opener}
          type="button"
          className={styles.launcher}
          aria-haspopup="dialog"
          aria-expanded={false}
          onClick={() => {
            setUsed(true);
            setOpen(true);
          }}
        >
          <Icon name="ask" size={20} strokeWidth={1.8} />
          <span className={styles.launcherLabel}>Chat</span>
        </button>
      ) : null}
      {used ? (
        <div ref={panel} role="dialog" aria-label="Ask" className={styles.panel} hidden={!open}>
          <div className={styles.head}>
            <p className="h3">Ask</p>
            <button
              type="button"
              className="btn btn-quiet"
              aria-label="Close"
              onClick={() => {
                setOpen(false);
                window.setTimeout(() => opener.current?.focus(), 0);
              }}
            >
              <Icon name="x" size={18} />
            </button>
          </div>
          <div className={styles.body}>
            <ChatClient examples={examples} compact draft={draft} />
          </div>
        </div>
      ) : null}
    </>
  );
}
