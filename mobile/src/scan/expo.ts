/**
 * Device implementations of the scan ports (§11).
 *
 * - Scanner: react-native-document-scanner-plugin wraps VisionKit
 *   (VNDocumentCameraViewController) on iOS and the ML Kit Document Scanner on
 *   Android: automatic edge detection, auto-crop, perspective correction,
 *   rotation and multiple pages.
 * - Analyzer: downsizes the page with expo-image-manipulator, decodes it with
 *   jpeg-js (pure JS, no Buffer) and runs the checks in ./quality.ts.
 * - QR: expo-camera reads codes from the saved page image.
 */
import { scanFromURLAsync } from "expo-camera";
import { ImageManipulator, SaveFormat } from "expo-image-manipulator";
import { decode as decodeJpeg } from "jpeg-js";
import DocumentScannerPlugin, { ResponseType, ScanDocumentResponseStatus } from "react-native-document-scanner-plugin";
import { fromBase64 } from "../lib/bytes";
import { discardTemporary } from "../offline/expo/files";
import { assessQuality, measureQuality, rgbaToGray } from "./quality";
import type { DocumentScanner, PageAnalyzer, QrDetector, ScanOutcome } from "./types";

/** Width the quality checks were calibrated for (see quality.ts). */
const ANALYSIS_WIDTH = 960;

export const nativeDocumentScanner: DocumentScanner = {
  async scan(options = {}): Promise<ScanOutcome> {
    try {
      const response = await DocumentScannerPlugin.scanDocument({
        croppedImageQuality: 92,
        responseType: ResponseType.ImageFilePath,
        ...(options.maxPages ? { maxNumDocuments: options.maxPages } : {}),
      });
      if (response.status === ScanDocumentResponseStatus.Cancel) return { status: "cancelled" };
      const uris = (response.scannedImages ?? []).filter((u) => typeof u === "string" && u.length > 0);
      if (uris.length === 0) return { status: "cancelled" };
      const all = uris.map((u) => (u.startsWith("file://") ? u : `file://${u}`));
      // iOS ignores maxNumDocuments: keep the first pages, remove the rest's plaintext files.
      const keep = options.maxPages ? all.slice(0, options.maxPages) : all;
      for (const extra of all.slice(keep.length)) discardTemporary(extra);
      return { status: "captured", pages: keep.map((uri) => ({ uri })) };
    } catch {
      // Permission refused, no camera, or the native module is not in this build.
      return { status: "unavailable" };
    }
  },
};

export const devicePageAnalyzer: PageAnalyzer = {
  async analyze(uri: string) {
    const context = ImageManipulator.manipulate(uri);
    let rendered: Awaited<ReturnType<typeof context.renderAsync>> | null = null;
    try {
      rendered = await context.resize({ width: ANALYSIS_WIDTH }).renderAsync();
      const saved = await rendered.saveAsync({ base64: true, format: SaveFormat.JPEG, compress: 0.92 });
      // The downsized copy is plaintext evidence on disk: remove it at once.
      discardTemporary(saved.uri);
      if (!saved.base64) return [];
      const jpeg = decodeJpeg(fromBase64(saved.base64), { useTArray: true, formatAsRGBA: true, maxMemoryUsageInMB: 64 });
      return assessQuality(measureQuality(rgbaToGray(jpeg.data, jpeg.width, jpeg.height)));
    } catch {
      return []; // No opinion rather than a false warning.
    } finally {
      rendered?.release();
      context.release();
    }
  },
};

export const deviceQrDetector: QrDetector = {
  async detect(uri: string) {
    try {
      const results = await scanFromURLAsync(uri, ["qr"]);
      return results.map((r) => r.data).filter((d) => typeof d === "string" && d.length > 0);
    } catch {
      return [];
    }
  },
};
