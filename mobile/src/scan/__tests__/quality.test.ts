import { describe, expect, it } from "@jest/globals";
import { assessQuality, measureQuality, rgbaToGray, type GrayImage } from "../quality";
import { gaussian, noisy, scale, textPage, withGlare } from "./synthetic";

const W = 320;
const H = 400;
/** Real optics are never pixel-perfect: start from slightly soft text. */
const thin = gaussian(noisy(textPage(W, H), 2), 0.6);
const bold = gaussian(noisy(textPage(W, H, 212, 45, 3, 3, 2), 2), 0.6);

const issues = (img: GrayImage) => assessQuality(measureQuality(img));

describe("blur detection", () => {
  it("accepts sharp pages, thin or bold, whatever the ink contrast", () => {
    expect(issues(thin)).toEqual([]);
    expect(issues(bold)).toEqual([]);
    expect(issues(scale(thin, 0.4, 120))).toEqual([]);
  });

  it("does not flag mild softness", () => {
    expect(issues(gaussian(thin, 0.8))).toEqual([]);
    expect(issues(gaussian(bold, 0.8))).toEqual([]);
  });

  it("flags clearly blurred pages, even with sensor noise", () => {
    expect(issues(gaussian(thin, 1.6))).toEqual(["blurry"]);
    expect(issues(noisy(gaussian(thin, 1.6), 4))).toEqual(["blurry"]);
    expect(issues(gaussian(bold, 2.5))).toEqual(["blurry"]);
  });

  it("orders sharpness monotonically with blur", () => {
    const s = [0, 0.8, 1.6, 3].map((sigma) => measureQuality(gaussian(thin, sigma)).sharpness ?? 0);
    expect(s[0]).toBeGreaterThan(s[1]!);
    expect(s[1]).toBeGreaterThan(s[2]!);
    expect(s[2]).toBeGreaterThan(s[3]!);
  });

  it("makes no blur call on a blank page", () => {
    const blank = noisy({ width: W, height: H, data: new Uint8Array(W * H).fill(210) }, 3);
    const m = measureQuality(blank);
    expect(m.sharpness).toBeNull();
    expect(assessQuality(m)).toEqual([]);
  });
});

describe("glare and exposure", () => {
  it("flags a clipped highlight on ordinary paper", () => {
    expect(issues(withGlare(thin, 200, 150, 45, 30))).toEqual(["glare"]);
  });

  it("ignores a tiny highlight", () => {
    expect(issues(withGlare(thin, 200, 150, 8, 6))).toEqual([]);
  });

  it("does not mistake a whitened scan background for glare", () => {
    const whitened = scale(thin, 1.3);
    expect(measureQuality(whitened).medianLuma).toBe(255);
    expect(issues(whitened)).toEqual([]);
  });

  it("flags a dark page and does not also call it blurry", () => {
    expect(issues(scale(gaussian(thin, 3), 0.2))).toEqual(["too_dark"]);
  });
});

describe("image helpers", () => {
  it("converts RGBA to Rec. 601 luma", () => {
    const rgba = Uint8Array.from([255, 255, 255, 255, 255, 0, 0, 255, 0, 255, 0, 255, 0, 0, 255, 255]);
    expect(Array.from(rgbaToGray(rgba, 2, 2).data)).toEqual([255, 76, 149, 29]);
    expect(() => rgbaToGray(rgba, 3, 3)).toThrow();
  });

  it("refuses images too small to judge", () => {
    expect(() => measureQuality({ width: 2, height: 2, data: new Uint8Array(4) })).toThrow();
  });
});
