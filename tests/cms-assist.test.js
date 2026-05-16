"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { maskToken, parseFlags, resolveConfig, validateConfig } = require("../bin/cms-assist");

test("parseFlags handles run options", () => {
  assert.deepEqual(
    parseFlags([
      "--base-url",
      "https://cms.example.test",
      "--token",
      "secret",
      "--space",
      "main",
      "--timeout",
      "5000",
      "--watch",
      "--mock",
    ]),
    {
      "base-url": "https://cms.example.test",
      token: "secret",
      space: "main",
      timeout: "5000",
      watch: true,
      mock: true,
    },
  );
});

test("resolveConfig uses environment fallback", () => {
  const original = { ...process.env };
  process.env.CMS_BASE_URL = "https://env.example.test";
  process.env.CMS_API_TOKEN = "env-token";
  process.env.CMS_SPACE_ID = "env-space";
  process.env.CMS_TIMEOUT_MS = "1234";
  process.env.CMS_MOCK = "true";

  try {
    assert.deepEqual(resolveConfig({}), {
      baseUrl: "https://env.example.test",
      token: "env-token",
      spaceId: "env-space",
      timeoutMs: 1234,
      watch: false,
      mock: true,
    });
  } finally {
    process.env = original;
  }
});

test("validateConfig reports missing values", () => {
  assert.deepEqual(validateConfig({
    baseUrl: "",
    token: "",
    spaceId: "",
    timeoutMs: 15000,
    watch: false,
    mock: false,
  }), [
    "CMS_BASE_URL/base-url is required",
    "CMS_API_TOKEN/token is required",
    "CMS_SPACE_ID/space is required",
  ]);
});

test("maskToken avoids printing secrets", () => {
  assert.equal(maskToken("abcdef123456"), "abcd...3456");
  assert.equal(maskToken("short"), "********");
  assert.equal(maskToken(""), "(missing)");
});
