# Local prompt skills

Put a UTF-8 Markdown file here (for example `my-model.md`) or use a folder with
`SKILL.md` inside it. Click **Refresh skills list** on the node to update the dropdown.
Both Claude Code and Codex use the same files. No installation or agent write access is needed.
Selecting a skill resets **skill_action** to **auto**. To update that skill, select it
first, then manually choose **refresh** before running the node.

```markdown
---
name: My Model 3.1
description: Prompt conventions for this exact model version
aliases: ["My-Model-3.1", "My Model v3.1"]
require_negative_prompt: true
---

# My Model 3.1

Describe the supported prompt format, useful structure, negative-prompt behavior,
limitations, and an example. Cite documentation when available.
```

Front matter is optional. Without it, the filename (or parent folder for `SKILL.md`)
is the matching name. Supported front matter is one key per line; use a JSON array
for `aliases`. Full YAML features and multiline metadata are not supported. Put
the actual instructions in the Markdown body. Each file is limited to 32 KB.

Optional `require_negative_prompt: true` makes the host require a nonempty negative
output, retry once if missing, and reject an incomplete result. It does not change
the image model's sampler settings. Omit the field or use `false` for automatic behavior.
During create/refresh, the current request and existing skill are supplied to the
researcher so requested skill changes can be incorporated. Explicit requests for
negative prompts are saved as this preference; a later refresh preserves it unless
you explicitly request otherwise.

**Automatic matching is exact** after ignoring case, spaces and punctuation.
Different versions do not match. Add aliases for alternative spellings. Explicitly
selecting a style skill loads it alongside model skills; it does not exempt unrelated
models from research. Manual files are trusted as user-provided guidance, not verified facts.

The node writes researched skills into `learned-<name>-<hash>/SKILL.md`, with research
date and source URLs. These survive run-folder cleanup and are reused until you request
a refresh. There is no automatic expiry. A refresh replaces the corresponding generated
file, never a manual file. To adopt refreshed guidance instead of a manual file, select
the newly generated skill from the dropdown. Keep manual aliases narrow and version-specific.

To create a researched skill, set **skill_action = create**, enter **skill_topic**,
enable **web_search**, and run the node. **refresh** repeats research even for a cache hit.
Click **Refresh skills list** afterwards to refresh the dropdown and reset the action to
**auto**, or select **auto** yourself, to reuse it on future runs. Change the seed
to force execution if ComfyUI has cached the node output.

`learned-*/` folders are git-ignored, so skills researched on your machine are not committed
by accident. To share one, copy it to a manual file such as `my-model.md`.

Do not put secrets in skills: their contents are sent to the chosen CLI provider.
