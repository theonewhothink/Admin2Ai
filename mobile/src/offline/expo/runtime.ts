/**
 * Wires the offline pipeline to real device services. One instance per JS
 * runtime, shared by the UI and the background task.
 */
import * as Crypto from "expo-crypto";
import { expoHttpSend } from "../../api/expoHttp";
import { apiEndpoint } from "../../config";
import { getSessionToken } from "../../security/session";
import { EncryptedQueueStore } from "../journal";
import { OfflinePipeline } from "../pipeline";
import { QueueRunner, systemTimers } from "../runner";
import { HttpEvidenceUploader } from "../uploader";
import type { Cipher, Clock } from "../types";
import { ExpoAesGcmCipher, expoHasher, loadOrCreateEvidenceKey } from "./crypto";
import { ExpoBlobStore, ExpoRawFile } from "./files";
import { expoNetwork } from "./network";

export interface OfflineRuntime {
  pipeline: OfflinePipeline;
  runner: QueueRunner;
  /** Shared with other sealed state (cached screens). */
  cipher: Cipher;
}

const systemClock: Clock = { now: () => Date.now() };

let runtime: OfflineRuntime | null = null;

export function getOfflineRuntime(): OfflineRuntime {
  if (runtime) return runtime;
  const cipher = new ExpoAesGcmCipher(loadOrCreateEvidenceKey);
  const transport = new HttpEvidenceUploader({
    endpoint: apiEndpoint(getSessionToken),
    send: expoHttpSend,
    random: Math.random,
    now: systemClock.now,
    timeoutMs: 120_000,
  });
  const pipeline = new OfflinePipeline({
    cipher,
    hasher: expoHasher,
    blobs: new ExpoBlobStore(),
    queue: new EncryptedQueueStore(new ExpoRawFile("queue.sealed"), cipher),
    network: expoNetwork,
    transport,
    clock: systemClock,
    random: Math.random,
    newId: () => Crypto.randomUUID(),
  });
  const runner = new QueueRunner(pipeline, expoNetwork, systemClock, systemTimers, (error) => {
    if (__DEV__) console.warn("[offline] drain failed", error);
  });
  runtime = { pipeline, runner, cipher };
  return runtime;
}
