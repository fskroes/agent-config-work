---
name: verify-examples
description: Three-pass verification of everything a reader will copy and run (commands, prompts, exercises, numbers, links) before docs, a tutorial, a prompt, or a report ships, with a verification log for what could not be checked. Invoke when the deliverable is text other people will act on.
---

# Verify every example you ship: three passes, minimum

- Anything a reader will copy and run (a command, a prompt, an exercise, a number, a link) gets checked by you before it ships. Not reasoned about. Run.
- Three passes minimum, and say what each pass was. Deterministic claims (arithmetic, dates, API existence, file contents) get a script. Links get fetched and the title read, not just a 200. Exercises get walked start to finish as the reader would.
- Examples rot. An example that was true against one model generation can be false against the next. Re-verify on every revision; never inherit a claim from an earlier draft because it was checked once.
- Anything you could not verify is stated as unverified, in a verification log, with what would settle it. Never launder an unchecked claim into confident prose.
- Design the exercise so it teaches under every plausible outcome. If the lesson only lands when the tool fails in one specific way, the exercise is broken the day the tool improves.
