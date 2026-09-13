# Kal-in

🇬🇧 English | 🇪🇸 [Español](README.es.md)

> This repo (formerly `kal`) is **kal-in** — kal's own reference agent,
> built on top of the kal kernel. The pure kernel (no agent, no ML —
> Access Manager, sandbox, audit log, Kernel Service Bus) is being
> extracted into its own repo, `kal`, so it can be used independently
> by other agents (see [Likay-OS](https://github.com/Kevindelb/Likay-OS)
> for the first such use). This README still describes the whole
> current codebase; it will be split accordingly once that extraction
> lands.

**A secure microkernel for intelligent capabilities.**

Most AI assistants tightly couple their features to one specific model
and provider. Kal separates intelligent capabilities from the
underlying AI engines through a secure microkernel architecture:
capabilities are isolated, sandboxed **Skills** that talk to the
kernel through a stable protocol, never to a specific model directly —
so a Skill written today keeps working when the model behind it
changes tomorrow.

Kal is local-first (Ollama, or any OpenAI-compatible endpoint — no
GPU required, everything ships CPU-first) and open source
([Apache 2.0](LICENSE)).

If you're wiring an LLM up to run code, load third-party plugins, or
touch its own source, Kal is built around that specific risk: every
Skill runs in an isolated, non-root Docker container regardless of
where it came from, a tiered permission cascade decides what any piece
of code is allowed to touch, memory content matching known credential
patterns gets redacted before it's ever kept long-term, and
self-modification requires explicit human approval before anything
reaches disk. See [Security first](#security-first) below for exactly
what this covers, and what it doesn't.

```
                          User
                           │
            VS Code extension / Web frontend
                           │
              Kernel (agent_core/orchestrator.py)
   ─────────────────────────────────────────────────
    Permission Cascade   Tool Registry   Audit Log
    Kernel Bus           Sandbox         Circuit Breaker
   ─────────────────────────────────────────────────
    Kernel Services: image · audio · speech-to-text
   ─────────────────────────────────────────────────
    Skills (sandboxed, zero standing trust)
```

## Why Kal?

| Traditional AI assistants          | Kal                                                              |
|-------------------------------------|-------------------------------------------------------------------|
| Tightly coupled to one model        | Model-agnostic — local (Ollama) or any OpenAI-compatible endpoint |
| Capabilities built into the app     | Every capability is a **Skill**, loaded independently             |
| Plugins get direct internal access  | Skills run in an ephemeral Docker container: no network, read-only filesystem, non-root, `cap_drop=ALL` by default |
| Ad-hoc / undocumented extensibility | Skills declare a manifest (permissions, dependencies, kernel services) verified before they ever run |
| Usually cloud-first                 | Local-first — CPU-only ML pipelines, no GPU or API key required   |

## Architecture

- **Kernel** (`agent_core/`) — coordinates the LLM conversation loop,
  permissions, sandboxing and auditing. It does not implement AI
  capabilities itself.
- **Kernel Services** (`kernel/services/services.py`) — shared,
  persistent services that hold a heavy resource (a loaded ML model) so
  it's never reloaded per call. Today: image generation, image
  inpainting, audio synthesis, speech-to-text.
- **Skills** (`skills/`) — sandboxed capabilities. A Skill never loads
  a model or touches the filesystem/network directly; it asks a
  Kernel Service for what it needs over the Kernel Bus, through the
  official **SDK** (`sdk/`) — never an internal kernel path.
- **Kernel Bus** (`kernel/api/`) — the JSON-RPC protocol (over a Unix
  socket, never a network port) that lets a sandboxed Skill call a
  Kernel Service without ever leaving its container.

## Security first

- Skills execute in an ephemeral Docker container per call: no
  network by default, read-only filesystem outside `/workspace`,
  non-root user, `cap_drop=ALL`, resource limits.
- A tiered permission cascade governs everything a piece of code can
  do — first-party tools, agent-proposed dynamic tools, and Skills
  each sit at a different trust ceiling, decided by *how* the code is
  registered, never by what it claims about itself.
- Static AST analysis is a first line of defense for dynamically
  created tools and self-modification proposals — a cheap filter, not
  the real security boundary (the sandbox is).
- Every sensitive action is recorded in an append-only, hash-chained
  audit log — tampering with a past entry breaks the chain visibly.
- Memory content matching known credential patterns (API keys, tokens)
  is redacted before being kept long-term; content is never shared
  with a cloud LLM provider unless explicitly marked shareable,
  checked at recall time regardless of tier.
- Skill packages are signed (Ed25519) and verified before loading —
  a tampered package is rejected outright, fail-closed.
- Installing a Skill from a remote market **requires** a valid
  signature — no exceptions. Integrity, not author trust: a curated
  publishing policy is a deliberate next step, not yet built.
- Self-modification (the agent proposing a change to its own code) is
  disabled by default, requires explicit human approval before ever
  touching disk, and is permanently blocked for core kernel modules.

## Project status

**Implemented**
- Sandboxed Skill execution (Docker isolation, deny-by-default)
- Tiered permission cascade
- Kernel Bus + 4 real Kernel Services (image generation/inpainting,
  audio synthesis, speech-to-text)
- Skill package signing (Ed25519) + guided local install
- Remote Skill install from a Git-based market, with mandatory
  signature verification
- Append-only, hash-chained audit log
- Three-tier memory (short/mid/long term)
- Self-modification pipeline (human-approval gated, disabled by default)
- Syscall-level observability via eBPF (Linux)
- VS Code extension (in-editor chat)
- Official Skill SDK (`sdk/`) — the only thing a Skill imports, pure
  stdlib, independent from the rest of the kernel
- Unified Access Manager (permissions) — filesystem and network share
  one generic grant/approval engine instead of two parallel ones

**In progress**
- Broader Kernel Service coverage (only 4 today — browser/OCR remain
  direct adapters, not yet Kernel Services)

**Planned**
- A browsable market (static site) over the same Git-based catalog
- A curated/reviewed publishing policy for the market
- A Windows-equivalent observability layer (no eBPF outside Linux)

## Roadmap

1. AI assistant (single process, local LLM) — ✓
2. Multimodal tools + real sandboxed execution — ✓
3. Kernel pivot — Skills as isolated, zero-trust plugins — ✓
4. Kernel Bus — shared services for Skills — ✓
5. Package integrity + guided install (signing) — ✓
6. Community — public repo, remote install from a market — ✓
7. Browsable market + curated publishing — next
8. Broader Kernel Service coverage, richer SDK — future

## Vision

We believe intelligent capabilities shouldn't be tied to a single
model, provider, or monolithic application. Kal is a secure
microkernel where developers build Skills instead of one-off
integrations — the model, the storage, the specific AI provider are
pluggable details behind a stable boundary, never assumptions baked
into every tool. Just as an operating system kernel enabled an
ecosystem of independent applications, Kal aims to enable an ecosystem
of independent, trustworthy AI Skills.

## Getting started

- **[Browse the Skill Market](https://carlosbv99-bit.github.io/kal/)** — see what's available before installing anything.
- `scripts/run_kal.sh` — run kal locally.
- `scripts/enable_skill.py` — install a Skill from a local folder.
- `scripts/install_from_market.py --list` — browse and install a Skill
  from the Git-based market (defaults to this repo).
- `scripts/sign_skill.py` — sign a Skill you authored.

## Documentation

The detailed engineering history of this project — every phase,
design decision, and real bug found along the way, in Spanish — lives
in [docs/HISTORY.md](docs/HISTORY.md).
