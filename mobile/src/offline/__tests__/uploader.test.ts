import { describe, expect, it } from "@jest/globals";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { utf8Decode, utf8Encode } from "../../lib/bytes";
import type { HttpRequest, HttpResponse, HttpSend } from "../../api/http";
import { classifyResponse, HttpEvidenceUploader, parseReceipt, uploadFields } from "../uploader";
import { encodeMultipart, encodeMultipartSafely, escapeParam, makeBoundary, safeContentType } from "../multipart";
import type { UploadRequest } from "../types";
import { sha256 } from "./fakes";

const SHA = "a".repeat(64);
const NOW = Date.UTC(2026, 8, 28, 9, 0, 0);

const fileBytes = Uint8Array.from([0xff, 0xd8, 0xff, 0xe0, ...utf8Encode("Vodafone FT 2026/183 92.40"), 0xff, 0xd9]);

function request(overrides: Partial<UploadRequest> = {}): UploadRequest {
  return {
    idempotencyKey: "7f1c2a9e-0000-4000-8000-000000000001",
    sha256: sha256(fileBytes),
    bytes: fileBytes,
    meta: {
      source: "mobile_scan",
      format: "image",
      fileName: "scan-1.jpg",
      mimeType: "image/jpeg",
      capturedAt: "2026-09-28T10:14:03+01:00",
      captureId: "cap-1",
      page: 1,
      pageCount: 2,
      hints: { qr: ["A:500000000*B:999999990*H:ATCUD-0"], quality: ["glare"] },
    },
    ...overrides,
  };
}

/** Tiny multipart reader for assertions (not used in production). */
function readMultipart(body: Uint8Array, boundary: string): Map<string, { headers: string; data: Uint8Array }> {
  const text = Buffer.from(body).toString("latin1");
  const parts = text.split(`--${boundary}`).slice(1, -1);
  const out = new Map<string, { headers: string; data: Uint8Array }>();
  for (const part of parts) {
    const trimmed = part.slice(2, -2); // leading CRLF, trailing CRLF
    const split = trimmed.indexOf("\r\n\r\n");
    const headers = trimmed.slice(0, split);
    const name = /name="([^"]*)"/.exec(headers)?.[1] ?? "";
    out.set(name, { headers, data: Uint8Array.from(Buffer.from(trimmed.slice(split + 4), "latin1")) });
  }
  return out;
}

describe("classifyResponse", () => {
  it("accepts a receipt with a normalised sha256", () => {
    expect(classifyResponse(201, { sha256: SHA.toUpperCase(), evidence_id: "ev_1", duplicate: false }, null, NOW)).toEqual({
      kind: "receipt",
      sha256: SHA,
      evidenceId: "ev_1",
      duplicate: false,
    });
  });

  it("treats 409 with a receipt as an already stored file", () => {
    expect(classifyResponse(409, { sha256: SHA, duplicate: true }, null, NOW)).toMatchObject({ kind: "receipt", duplicate: true });
  });

  it("never invents a receipt from a 2xx without a valid hash", () => {
    expect(classifyResponse(200, {}, null, NOW)).toEqual({ kind: "retryable", status: 200 });
    expect(classifyResponse(200, undefined, null, NOW)).toEqual({ kind: "retryable", status: 200 });
    expect(classifyResponse(200, { sha256: "abc" }, null, NOW)).toEqual({ kind: "retryable", status: 200 });
  });

  it("recognises hash mismatch answers, including FastAPI's detail wrapper", () => {
    expect(classifyResponse(422, { error: "hash_mismatch", sha256: SHA }, null, NOW)).toEqual({
      kind: "mismatch",
      status: 422,
      serverSha256: SHA,
    });
    expect(classifyResponse(422, { detail: { error: "hash_mismatch" } }, null, NOW)).toEqual({ kind: "mismatch", status: 422 });
    expect(classifyResponse(400, { detail: "hash_mismatch" }, null, NOW)).toEqual({ kind: "mismatch", status: 400 });
  });

  it("maps statuses to retry, sign-in or hold", () => {
    expect(classifyResponse(401, {}, null, NOW)).toEqual({ kind: "auth", status: 401 });
    expect(classifyResponse(403, {}, null, NOW)).toEqual({ kind: "auth", status: 403 });
    expect(classifyResponse(503, {}, "30", NOW)).toEqual({ kind: "retryable", status: 503, retryAfterMs: 30_000 });
    expect(classifyResponse(429, {}, new Date(NOW + 90_000).toUTCString(), NOW)).toEqual({
      kind: "retryable",
      status: 429,
      retryAfterMs: 90_000,
    });
    expect(classifyResponse(507, {}, null, NOW)).toEqual({ kind: "retryable", status: 507 });
    expect(classifyResponse(413, {}, null, NOW)).toEqual({ kind: "rejected", status: 413 });
    expect(classifyResponse(415, {}, null, NOW)).toEqual({ kind: "rejected", status: 415 });
  });

  it("parses receipts strictly", () => {
    expect(parseReceipt({ sha256: ` ${SHA} ` })).toEqual({ sha256: SHA });
    expect(parseReceipt({ receipt: { sha256: SHA } })).toBeNull();
    expect(parseReceipt([SHA])).toBeNull();
  });
});

describe("HttpEvidenceUploader", () => {
  function recorder(status: number, body: string, headers: Record<string, string> = {}) {
    const calls: HttpRequest[] = [];
    const send: HttpSend = async (req) => {
      calls.push(req);
      const res: HttpResponse = {
        status,
        header: (n) => headers[n.toLowerCase()] ?? null,
        text: async () => body,
      };
      return res;
    };
    return { calls, send };
  }

  it("posts multipart sha256 + file with an idempotency key and bearer token", async () => {
    const hash = sha256(fileBytes);
    const { calls, send } = recorder(201, JSON.stringify({ sha256: hash, evidence_id: "ev_9" }));
    const uploader = new HttpEvidenceUploader({
      endpoint: { baseUrl: "https://api.example.eu", timeoutMs: 4000, getAuthToken: async () => "tok" },
      send,
      random: () => 0.25,
      now: () => NOW,
    });
    const outcome = await uploader.upload(request());
    expect(outcome).toEqual({ kind: "receipt", sha256: hash, evidenceId: "ev_9" });

    const call = calls[0]!;
    expect(call.url).toBe("https://api.example.eu/api/evidence/upload");
    expect(call.method).toBe("POST");
    expect(call.headers["Idempotency-Key"]).toBe("7f1c2a9e-0000-4000-8000-000000000001");
    expect(call.headers.Authorization).toBe("Bearer tok");
    const boundary = /boundary=(.+)$/.exec(call.headers["Content-Type"] ?? "")?.[1] ?? "";
    const parts = readMultipart(call.body as Uint8Array, boundary);
    expect(utf8Decode(parts.get("sha256")!.data)).toBe(hash);
    expect(utf8Decode(parts.get("source")!.data)).toBe("mobile_scan");
    expect(utf8Decode(parts.get("page_count")!.data)).toBe("2");
    expect(JSON.parse(utf8Decode(parts.get("hints")!.data))).toEqual({ qr: ["A:500000000*B:999999990*H:ATCUD-0"], quality: ["glare"] });
    expect(parts.get("file")!.headers).toContain('filename="scan-1.jpg"');
    expect(parts.get("file")!.headers).toContain("Content-Type: image/jpeg");
    expect(sha256(parts.get("file")!.data)).toBe(hash);
  });

  it("returns a network outcome when the request never completes", async () => {
    const uploader = new HttpEvidenceUploader({
      endpoint: { baseUrl: "https://api.example.eu", timeoutMs: 4000 },
      send: async () => {
        throw new Error("offline");
      },
      random: () => 0,
      now: () => NOW,
    });
    expect(await uploader.upload(request())).toEqual({ kind: "network" });
  });

  it("stays queued in demo mode instead of pretending the server confirmed", async () => {
    const { calls, send } = recorder(200, "{}");
    const uploader = new HttpEvidenceUploader({ endpoint: { baseUrl: null, timeoutMs: 4000 }, send, random: () => 0, now: () => NOW });
    expect(await uploader.upload(request())).toEqual({ kind: "network" });
    expect(calls).toHaveLength(0);
  });

  it("passes Retry-After through", async () => {
    const { send } = recorder(503, "", { "retry-after": "12" });
    const uploader = new HttpEvidenceUploader({ endpoint: { baseUrl: "https://x.eu", timeoutMs: 1 }, send, random: () => 0, now: () => NOW });
    expect(await uploader.upload(request())).toEqual({ kind: "retryable", status: 503, retryAfterMs: 12_000 });
  });
});

describe("multipart encoding", () => {
  it("escapes quotes and newlines in names and normalises content types", () => {
    expect(escapeParam('a"b\r\nc.pdf')).toBe("a%22b%0D%0Ac.pdf");
    expect(safeContentType("Application/PDF")).toBe("application/pdf");
    expect(safeContentType("text/plain; charset=utf-8")).toBe("text/plain; charset=utf-8");
    expect(safeContentType("image/jpeg\r\nX-Evil: 1")).toBe("application/octet-stream");
  });

  it("encodes non-ASCII file names as UTF-8", () => {
    const body = encodeMultipart("b", [], { name: "file", fileName: "fatura-março.pdf", contentType: "application/pdf", bytes: utf8Encode("%PDF") });
    expect(utf8Decode(body)).toContain('filename="fatura-março.pdf"');
  });

  it("picks another boundary when the content contains the first one", () => {
    let calls = 0;
    const random = () => (calls++ < 24 ? 0 : 0.5);
    const clash = utf8Encode(`xx--${makeBoundary(() => 0)}yy`);
    const { boundary } = encodeMultipartSafely([], { name: "file", fileName: "f", contentType: "text/plain", bytes: clash }, random);
    expect(boundary).not.toBe(makeBoundary(() => 0));
  });

  it("matches the golden wire fixture shared with the backend test", () => {
    const fixtureDir = join(__dirname, "../../../contracts/fixtures");
    const fixturePath = join(fixtureDir, "evidence-upload.multipart");
    const pdf = utf8Encode("%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n");
    const req = request({
      idempotencyKey: "0b7e0e7c-6a55-4a8e-9b1e-2d0f3c9a1f00",
      sha256: sha256(pdf),
      bytes: pdf,
      meta: {
        source: "mobile_share",
        format: "pdf",
        fileName: "vodafone-2026-09.pdf",
        mimeType: "application/pdf",
        capturedAt: "2026-09-28T10:14:03+01:00",
        hints: { title: "Your invoice is ready" },
      },
    });
    const body = encodeMultipart(makeBoundary(() => 0), uploadFields(req), {
      name: "file",
      fileName: req.meta.fileName,
      contentType: req.meta.mimeType,
      bytes: req.bytes,
    });
    if (process.env.UPDATE_FIXTURES === "1") {
      mkdirSync(fixtureDir, { recursive: true });
      writeFileSync(fixturePath, body);
    }
    expect(Buffer.from(body).equals(readFileSync(fixturePath))).toBe(true);
  });
});
