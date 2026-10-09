# Provider boundaries

The user requires independent provider implementations with the same folder structure.

- Each provider owns `__init__.py`, `client.py`, `parser.py`, `downloader.py`, `handler.py`, and `urls.py` inside `src/downloader_bot/downloaders/<site>/`.
- Site-specific URL rules, HTTP headers, sessions, authentication, extraction, format discovery, HLS parsing, and handlers belong to that provider. Do not import another provider or a shared concrete video/HLS implementation from provider code.
- Every provider implements the shared `Downloader.inspect` / `Downloader.resolve` contract directly. Shared data models, the abstract base, and generic Telegram/database/transfer infrastructure remain outside provider folders.
- Provider HTTP sessions are separate. Credentials and cookies must stay in the owning provider's session.
- During concurrent work, keep changes inside the provider being developed and its tests. Read the latest composition files before editing routing or startup; preserve registrations and changes made by other work.
- Do not refactor provider logic into a common implementation merely because two providers currently have similar code. Their structure is shared; their implementation is independent by explicit user request.
- Validate provider boundaries with the architecture tests and run the tests affected by the change. Avoid replacing or reverting another task's work.
