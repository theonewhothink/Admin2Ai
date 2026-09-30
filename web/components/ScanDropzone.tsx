"use client";

import { useId, useRef, useState } from "react";
import { browserEngine, uploadEvidence } from "@/lib/api";
import { Icon } from "./Icon";
import styles from "./scan.module.css";

interface Upload {
  id: number;
  name: string;
  size: number;
  state: "sending" | "received" | "failed";
  /** What happened, in the engine's words. */
  message?: string;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const ACCEPT = "image/*,application/pdf,text/plain,.txt,.xml,.eml,.csv";
const EXTRA = /\.(txt|xml|eml|csv)$/i;
const SAMPLE = `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/samples/edp-invoice-qr.txt`;

/** Photos, PDFs, and the text formats invoices also come in (QR text, e-invoice XML, emails, bank CSV). */
function accepted(f: File): boolean {
  return f.type.startsWith("image/") || f.type === "application/pdf" || f.type.startsWith("text/") || EXTRA.test(f.name);
}

export function ScanDropzone() {
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [over, setOver] = useState(false);
  const counter = useRef(0);
  const fileInput = useRef<HTMLInputElement>(null);
  const cameraInput = useRef<HTMLInputElement>(null);
  const hintId = useId();

  const send = (file: File) => {
    const id = ++counter.current;
    setUploads((prev) => [{ id, name: file.name, size: file.size, state: "sending" }, ...prev]);
    void uploadEvidence(file).then((res) => {
      setUploads((prev) =>
        prev.map((u) => (u.id === id ? { ...u, state: res.ok ? "received" : "failed", message: res.message } : u)),
      );
    });
  };

  const handle = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    Array.from(files).filter(accepted).forEach(send);
  };

  const [sampleBusy, setSampleBusy] = useState(false);
  const trySample = async () => {
    setSampleBusy(true);
    try {
      const res = await fetch(SAMPLE);
      if (res.ok) send(new File([await res.blob()], "edp-invoice-qr.txt", { type: "text/plain" }));
    } finally {
      setSampleBusy(false);
    }
  };

  return (
    <div className="stack-3">
      <div
        className={styles.drop}
        data-over={over}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          handle(e.dataTransfer.files);
        }}
      >
        <span className={styles.dropIcon}>
          <Icon name="upload" size={24} />
        </span>
        <p className={styles.dropTitle}>Drop receipts or invoices here</p>
        <p className="meta" id={hintId}>
          Photos, PDFs or invoice files. As many as you like.
        </p>
        <div className={styles.dropActions}>
          <button type="button" className={`btn btn-primary ${styles.cameraBtn}`} onClick={() => cameraInput.current?.click()}>
            <Icon name="camera" size={18} />
            Take a photo
          </button>
          <button type="button" className="btn btn-secondary" onClick={() => fileInput.current?.click()} aria-describedby={hintId}>
            Choose files
          </button>
        </div>
        {browserEngine ? (
          <button type="button" className={`link-quiet ${styles.sample}`} onClick={() => void trySample()} disabled={sampleBusy}>
            No receipt to hand? Try a sample EDP invoice.
          </button>
        ) : null}
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          className="visually-hidden"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => {
            handle(e.target.files);
            e.target.value = "";
          }}
        />
        <input
          ref={cameraInput}
          type="file"
          accept="image/*"
          capture="environment"
          className="visually-hidden"
          tabIndex={-1}
          aria-hidden="true"
          onChange={(e) => {
            handle(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {uploads.length > 0 ? (
        <ul className="card list" aria-live="polite">
          {uploads.map((u) => (
            <li key={u.id} className={styles.upload}>
              <span className={styles.fileIcon}>
                <Icon name="document" size={18} />
              </span>
              <span className={styles.fileMain}>
                <span className={styles.fileName}>{u.name}</span>
                <span className="meta num">{formatSize(u.size)}</span>
              </span>
              <span className={styles.fileState} data-state={u.state}>
                {u.state === "sending" ? (
                  "Sending…"
                ) : u.state === "received" ? (
                  <>
                    <Icon name="check" size={16} strokeWidth={2.2} />
                    {u.message ?? "Received. I’ll take it from here."}
                  </>
                ) : (
                  (u.message ?? "Didn’t arrive. Try again.")
                )}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
