"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import styles from "./AskBox.module.css";

interface AskBoxProps {
  /** Called with the question. Without it, the box opens the Ask page. */
  onAsk?: (question: string) => void;
  value?: string;
  onValueChange?: (value: string) => void;
  busy?: boolean;
  autoFocus?: boolean;
  /** Focus the box when "/" is pressed anywhere on the page. */
  slashToFocus?: boolean;
}

export function AskBox({ onAsk, value, onValueChange, busy = false, autoFocus, slashToFocus = true }: AskBoxProps) {
  const router = useRouter();
  const [local, setLocal] = useState("");
  const text = value ?? local;
  const setText = onValueChange ?? setLocal;
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!slashToFocus) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [slashToFocus]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const q = text.trim();
    if (!q || busy) return;
    if (onAsk) onAsk(q);
    else router.push(`/ask?q=${encodeURIComponent(q)}`);
  };

  return (
    <form className={styles.box} onSubmit={submit} role="search">
      <Icon name="ask" size={20} className={styles.leading} />
      <label htmlFor="ask-input" className="visually-hidden">
        Ask your business anything
      </label>
      <input
        ref={inputRef}
        id="ask-input"
        className={styles.input}
        type="text"
        autoComplete="off"
        enterKeyHint="send"
        placeholder="Ask your business anything"
        value={text}
        onChange={(e) => setText(e.target.value)}
        autoFocus={autoFocus}
      />
      {slashToFocus && !text ? (
        <kbd className={styles.kbd} aria-hidden="true">
          /
        </kbd>
      ) : null}
      <button type="submit" className={styles.send} disabled={!text.trim() || busy} aria-label="Ask">
        <Icon name="arrowUp" size={18} strokeWidth={2} />
      </button>
    </form>
  );
}
