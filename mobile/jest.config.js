/**
 * Unit tests run on the jest-expo preset (babel-preset-expo transforms, React
 * Native mocks). Pure-core tests never import native modules.
 *
 * Dates in tests are rendered in a fixed zone so "today"/"yesterday" labels are
 * deterministic on any machine.
 */
process.env.TZ = "Europe/Lisbon";

/** @type {import('jest').Config} */
module.exports = {
  preset: "jest-expo",
  testMatch: ["<rootDir>/src/**/__tests__/**/*.test.ts?(x)"],
  clearMocks: true,
};
