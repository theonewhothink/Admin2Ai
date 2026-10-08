/**
 * HttpSend backed by `expo/fetch`, the WinterCG fetch that accepts Uint8Array
 * bodies (React Native's global fetch does not). Timeouts abort the request.
 */
import { fetch } from "expo/fetch";
import { ownedBytes } from "../lib/bytes";
import type { HttpResponse, HttpSend } from "./http";

export const expoHttpSend: HttpSend = async (request) => {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), request.timeoutMs);
  try {
    const body = request.body === undefined ? null : typeof request.body === "string" ? request.body : ownedBytes(request.body);
    const response = await fetch(request.url, {
      method: request.method,
      headers: request.headers,
      body,
      signal: controller.signal,
    });
    // Read the body inside the timeout window so a stalled stream also aborts.
    const text = await response.text();
    const result: HttpResponse = {
      status: response.status,
      header: (name) => response.headers.get(name),
      text: async () => text,
    };
    return result;
  } finally {
    clearTimeout(timer);
  }
};
