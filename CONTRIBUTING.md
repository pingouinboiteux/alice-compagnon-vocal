# Contributing to Alice

Thank you for taking an interest in Alice.

This public repository is **not** the full private source tree. It is a curated public extraction meant to share rare technical lessons, useful code fragments, and measured pitfalls from the project.

Alice is a local French voice companion project built and tested on:

- Windows 11
- AMD GPUs
- local models
- a strict safety-first approach

The goal of this repository is not to pretend that Alice is a finished one-click product. The goal is to make the project easier to audit, understand, and improve in bounded ways.

## What kind of help is useful

The most useful help is:

- a fresh technical review on a precise file or subsystem
- a measured alternative method
- a bounded fix with a clear tradeoff
- a safety or stability audit
- documentation that makes a rare lesson easier to reuse

Examples of useful topics:

- Windows audio streaming and latency
- Windows process lifecycle and cleanup
- AMD / DirectML / ONNX / GPU selection issues
- Godot / VRM / body animation architecture
- memory extraction safety and false-memory prevention
- vision gating and deterministic safeguards

## What is less useful

Please avoid:

- very broad rewrites without a measured reason
- advice that assumes Linux, CUDA, or cloud-only infrastructure by default
- cheating, bypassing protections, or unsafe game automation ideas
- suggestions that require publishing private data or the full private repository

## Before proposing changes

Please read first:

- `README.md`
- `PIEGES.md`
- `RESULTATS.md`

Those three files explain what this public repository is for and what kind of contribution is actually relevant.

## How to help

If you want to help, the best first step is one of these:

- open an issue with a bounded technical question or review
- comment on an existing issue if you want to audit a specific area
- propose a small documentation or code improvement that fits the editorial rule of the repository

A good contribution request usually contains:

- the exact file or subsystem you reviewed
- what you think is wrong, fragile, or improvable
- why
- how you would measure the improvement
- what tradeoff or risk comes with your proposal

## Current collaboration context

Alice is still primarily developed by one person.

That means:

- there is **no paid budget** for contributors right now
- volunteer help is welcome
- small, precise, high-signal contributions are much easier to integrate than big open-ended ones

## Project principles

These principles matter more than style preferences:

- measure before claiming
- one verified step at a time
- deterministic guardrails beat vague prompt rules
- do not touch private memory or personal data casually
- stability matters more than flashy complexity

## French note

Le projet est francais a la base, mais les retours en anglais sont les bienvenus.
Si vous voulez aider sans parler francais parfaitement, ce n'est pas un probleme.
Le plus important est d'etre precis, honnete, et concret.
