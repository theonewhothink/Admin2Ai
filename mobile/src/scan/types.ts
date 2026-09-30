/**
 * Ports for the capture screen (§11). The native document scanner (VisionKit on
 * iOS, ML Kit on Android) does edge detection, auto-crop, perspective
 * correction, rotation and multi-page capture; the app only sees page images.
 */
import type { QualityIssue } from "../offline/types";

export interface ScannedPage {
  /** file:// URI of the cropped, perspective-corrected page (JPEG). */
  uri: string;
}

export type ScanOutcome =
  | { status: "captured"; pages: ScannedPage[] }
  | { status: "cancelled" }
  /** No camera, permission refused, or the scanner module is missing (e.g. Expo Go). */
  | { status: "unavailable" };

export interface DocumentScanner {
  scan(options?: { maxPages?: number }): Promise<ScanOutcome>;
}

export interface PageAnalyzer {
  /** Advisory quality issues for one page. Resolves to [] when it cannot tell. */
  analyze(uri: string): Promise<QualityIssue[]>;
}

export interface QrDetector {
  /** Raw payloads of QR codes on the page (e.g. Portuguese invoice QR, §19). */
  detect(uri: string): Promise<string[]>;
}
