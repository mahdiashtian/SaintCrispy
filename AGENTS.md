# Provider boundaries

The user requires independent provider implementations with the same folder structure.

- Each provider owns `__init__.py`, `client.py`, `parser.py`, `downloader.py`, `handler.py`, and `urls.py` inside `src/downloader_bot/downloaders/<site>/`.
- Site-specific URL rules, HTTP headers, sessions, authentication, extraction, format discovery, HLS parsing, and handlers belong to that provider. Do not import another provider or a shared concrete video/HLS implementation from provider code.
- Every provider implements the shared `Downloader.inspect` / `Downloader.resolve` contract directly. Shared data models, the abstract base, and generic Telegram/database/transfer infrastructure remain outside provider folders.
- Provider HTTP sessions are separate. Credentials and cookies must stay in the owning provider's session.
- During concurrent work, keep changes inside the provider being developed and its tests. Read the latest composition files before editing routing or startup; preserve registrations and changes made by other work.
- Do not refactor provider logic into a common implementation merely because two providers currently have similar code. Their structure is shared; their implementation is independent by explicit user request.
- Validate provider boundaries with the architecture tests and run the tests affected by the change. Avoid replacing or reverting another task's work.

# Git workflow

- After each completed change or coherent batch of changes and successful relevant checks, commit once and push to the configured remote and current branch. This is a standing user instruction; do not wait for another request to commit or push.
- Before finishing a task, verify the working tree and upstream synchronization. If completed work remains uncommitted or unpushed, commit and push it. If there are no new changes, verify the existing push without creating an empty commit.
- Before editing, fetch upstream and integrate the work with its latest changes. Preserve the upstream application architecture, providers, and existing history. Never force-push.
- Exclude secrets, local `.env`, virtual environments, sessions, runtime logs, and research caches. Report commit and push results separately.

# Investigation memory

- Before investigating or changing a provider, read `docs/KNOWLEDGE_BASE.md` and its linked dated evidence.
- Record newly tested methods, failures, verified findings and retry conditions there. Distinguish workstation, CI and production results; never store credentials or signed URLs.
- Do not repeat a failed method under unchanged conditions in a retry loop. Revisit it when new code, authorized access, cooldown expiry or connection evidence justifies another attempt.
