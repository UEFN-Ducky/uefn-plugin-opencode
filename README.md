# OpenCode gateway for UEFN Ducky

[OpenCode](https://opencode.ai), the open-source coding agent, as a coding agent in Ducky chats.

## Out of the box

1. Install **OpenCode** from Store → Gateways and enable it.
2. Pick **OpenCode** in a chat's model picker and send a message.

There is nothing else to set up:

- **No install:** Ducky downloads its own OpenCode (`@opencode/cli` 2.2.0 from npm's registry,
  checked against a pinned SHA-512) into its app data. It is never added to PATH, and an
  OpenCode you installed yourself is left alone.
- **No second sign-in:** API keys you saved in Ducky (Anthropic, OpenAI, Gemini, Kimi, xAI)
  are handed to OpenCode. With no key, OpenCode's free model answers.
- **Subscriptions:** Settings → LLMs → OpenCode → Log in signs in ChatGPT, GitHub Copilot or
  SuperGrok through OpenCode's own sign-in.

## How it runs

- One local OpenCode server (`opencode serve` on 127.0.0.1, random in-memory password) is
  shared by every chat. It starts on the first message and stops after 10 idle minutes.
- Each chat gets its own `ducky-<chat>` MCP entry with Ducky's tools and that chat's run
  identity. Session rules hide every other chat's entry.
- OpenCode enforces its own permissions. Its approval requests show as Ducky approval cards,
  always answered "once" (an "always" grant would cover the whole project).
- Ask and Plan use OpenCode's read-only `plan` agent; Agent uses `build`.
- One event stream per server, routed by session; text is pushed in 50 ms batches.

## Tests

From the UEFN Ducky repo root (the tests import the app host):

```bash
py -3 -m pytest --import-mode=importlib <path>/uefn-plugin-opencode/backend
```

`backend/fixtures/*.jsonl` are real OpenCode 2.2 event transcripts recorded with
`scripts/record_fixture.py`.
