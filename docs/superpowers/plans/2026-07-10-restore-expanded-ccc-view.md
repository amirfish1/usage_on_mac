# Restore Expanded CCC View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the detailed xbar dropdown when CCC supplies usage data.

**Architecture:** Normalize CCC data into the variables consumed by the existing detailed renderer. Retain the existing Chrome/cache path as the fallback and avoid a second network source on successful CCC reads.

**Tech Stack:** Python 3 standard library, `unittest`, xbar text protocol

## Global Constraints

- CCC remains the preferred source when its response is fresh.
- CCC responses render detailed pace, session, timestamp, and action rows.
- The fallback behavior for unavailable or stale CCC remains unchanged.
- Do not fetch Chrome solely for missing extra-usage billing data.

---

### Task 1: Lock The Detailed CCC Output

**Files:**
- Create: `tests/test_claude_usage.py`
- Modify: `claude-usage.5m.py`

**Interfaces:**
- Consumes: `CCC_USAGE_URL` environment variable and the CCC `/api/usage/current` JSON schema.
- Produces: xbar text output from `main()` using one shared detailed renderer.

- [ ] **Step 1: Write the failing regression test**

Create an HTTP fixture that returns fresh Claude and Codex usage, invoke the
plugin as a subprocess, and assert that output contains `Used 19% · expected`,
`Worked`, `Updated`, and `Refresh now`, while excluding `via CCC`.

- [ ] **Step 2: Verify the test fails for the compact renderer**

Run: `python3 -m unittest tests.test_claude_usage -v`

Expected: FAIL because the current CCC output includes `via CCC` and omits the
detailed rows.

- [ ] **Step 3: Normalize CCC into the detailed path**

Remove the early `render_from_ccc` return. On a fresh CCC response, map its
Claude and Codex fields to the variables already consumed by the detailed
renderer, use CCC's `fetched_at` as the display timestamp, and mark the fetch as
healthy. Leave the existing direct-fetch branch intact for CCC failure.

- [ ] **Step 4: Verify regression and syntax**

Run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass.

Run: `python3 -m py_compile claude-usage.5m.py _codex_lib.py _session_lib.py`

Expected: exit code 0.

- [ ] **Step 5: Verify live xbar text**

Run: `./claude-usage.5m.py`

Expected: detailed CCC-backed output without `via CCC`, including pace details,
the updated timestamp, and menu actions.
