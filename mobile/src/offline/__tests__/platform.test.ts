import { describe, expect, it } from "@jest/globals";
import { isOwnedTemporary, isReachable, isSafeKey, isScannerOutput } from "../platform";

const APP = "file:///var/mobile/Containers/Data/Application/0A1B";
const ROOTS = [`${APP}/Library/Caches/`, `${APP}/tmp`, "file:///private/var/mobile/Containers/Shared/AppGroup/77EE/"];

describe("isReachable", () => {
  it("is online only when connected and not known to be unreachable", () => {
    expect(isReachable({ isConnected: true, isInternetReachable: true })).toBe(true);
    expect(isReachable({ isConnected: true })).toBe(true);
    expect(isReachable({ isConnected: true, isInternetReachable: false })).toBe(false);
    expect(isReachable({ isConnected: false, isInternetReachable: true })).toBe(false);
    expect(isReachable({})).toBe(false);
  });
});

describe("isSafeKey", () => {
  it("accepts UUIDs and rejects path tricks", () => {
    expect(isSafeKey("7f1c2a9e-0000-4000-8000-000000000001")).toBe(true);
    for (const bad of ["", "../queue", "a/b", "a.sealed", "x".repeat(81)]) expect(isSafeKey(bad)).toBe(false);
  });
});

describe("isOwnedTemporary", () => {
  it("allows files inside the app's cache, tmp and App Group folders", () => {
    expect(isOwnedTemporary(`${APP}/Library/Caches/ImageManipulator/a.jpg`, ROOTS)).toBe(true);
    expect(isOwnedTemporary(`${APP}/tmp/share%20copy.pdf`, ROOTS)).toBe(true);
    // iOS reports the same folders as /private/var or /var.
    expect(isOwnedTemporary(`file:///private${APP.slice("file://".length)}/tmp/a.jpg`, ROOTS)).toBe(true);
    expect(isOwnedTemporary("file:///var/mobile/Containers/Shared/AppGroup/77EE/shared.pdf", ROOTS)).toBe(true);
  });

  it("never allows originals elsewhere, the roots themselves, or traversal", () => {
    expect(isOwnedTemporary(`${APP}/Documents/evidence-queue/x.sealed`, ROOTS)).toBe(false);
    expect(isOwnedTemporary("file:///var/mobile/Media/DCIM/100APPLE/IMG_0001.JPG", ROOTS)).toBe(false);
    expect(isOwnedTemporary("content://com.whatsapp.provider/media/1", ROOTS)).toBe(false);
    expect(isOwnedTemporary(`${APP}/tmp/`, ROOTS)).toBe(false);
    expect(isOwnedTemporary(`${APP}/tmp/../Documents/queue.sealed`, ROOTS)).toBe(false);
    expect(isOwnedTemporary(`${APP}/tmp/%2E%2E/Documents/queue.sealed`, ROOTS)).toBe(false);
    expect(isOwnedTemporary(`${APP}/tmpfoo/x`, ROOTS)).toBe(false);
    expect(isOwnedTemporary(`${APP}/tmp/%E0%A4%A`, ROOTS)).toBe(false);
  });
});

describe("isScannerOutput", () => {
  const docs = `${APP}/Documents/`;
  it("matches only the iOS scanner's page files at the top of Documents", () => {
    expect(isScannerOutput(`${docs}DOCUMENT_SCAN_1_20260928_101403.jpg`, docs)).toBe(true);
    expect(isScannerOutput(`file:///private${docs.slice("file://".length)}DOCUMENT_SCAN_2_20260928_101403.jpg`, docs)).toBe(true);
    expect(isScannerOutput(`${docs}evidence-queue/DOCUMENT_SCAN_1_20260928_101403.jpg`, docs)).toBe(false);
    expect(isScannerOutput(`${docs}queue.sealed`, docs)).toBe(false);
    expect(isScannerOutput(`${docs}DOCUMENT_SCAN_1_20260928_101403.jpg.sealed`, docs)).toBe(false);
    expect(isScannerOutput(`${APP}/Library/DOCUMENT_SCAN_1_20260928_101403.jpg`, docs)).toBe(false);
  });
});
