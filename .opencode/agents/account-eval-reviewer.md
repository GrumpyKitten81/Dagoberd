---
description: Read-only AccountEval reviewer for correctness, data integrity, security, and test risks.
mode: subagent
color: "#ff6b6b"
steps: 8
permissions:
  - action: edit
    resource: "*"
    effect: deny
  - action: shell
    resource: "*"
    effect: deny
---

Read and follow `.agents/prompts/account-eval-reviewer.md`.
