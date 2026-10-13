import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.ai import llm, mcp
from app.ai.loop import run_agent_loop
from app.config import settings
from app.handlers import vault

log = logging.getLogger(__name__)

_PROCESS_INSTRUCTIONS = """\
## Inbox Processing

Today is {date}. You are processing the user's Obsidian inbox — a queue of raw captured thoughts.
Your job: file every entry into its correct permanent home, then clear the inbox.

### Step 1 — Orient yourself

Always start by calling:
1. `read_inbox` — get all current entries
2. `list_directory` with `path: "notes"` — understand the existing folder structure

### Step 2 — Process each entry

For every entry in the inbox:

**a. Classify it:**
- `idea` — creative or exploratory thought, not immediately actionable
- `project` — a multi-step goal (warrants a vault note AND possibly a Todoist project)
- `task` — a single concrete action → Todoist add-tasks
- `reference` — a fact, link, or resource to keep; if a person is the source, note them as the source in the resource note rather than filing it under them
- `person` — biographical info *about* a specific person (birthday, contact details, relationship updates) → `notes/personal/people/` folder
- `area` — relates to an ongoing life area (health, finance, home, etc.)

**Person vs. resource distinction:** A person note holds facts *about the person* — birthday, where they
live, what they do, relationships, something they need, something they said. **Anything researched, bought,
recommended, or looked up is a topic note, never a person note**, no matter who prompted it. Gift research,
a product, a restaurant, a book, a tool: create or update `notes/personal/wiki/<topic>.md` and link it from
the person's note with one line of context (`Gift idea: [[peugeot-clef-du-vin]] — for his wine collection`).
A person note that has become a product page is wrong. People in user context have aliases (e.g. "Dad" → his
full name as listed there) — resolve these to their full name when filing.

**b. Search** for related existing notes with `search_notes` before deciding to create.
Use `excludePaths: ["archive", "meta"]` to skip machine-generated files, and `pathPrefix`
(e.g. `"notes/personal"`) to narrow a search.

**c. Act:**
- Related note found → append to it: `write_note` with `mode: "append"`, or `patch_note` for a targeted edit. **Never call `write_note` in its default overwrite mode on an existing note** — it would destroy the note's content and will be refused.
- New substantial topic → `write_note` at the right vault path (new file), passing frontmatter via the `frontmatter` parameter
- Actionable single task → Todoist `add-tasks` (use project ID from user context; call find-projects if unsure)
- Project-level idea → create a vault note AND a Todoist project if it needs tasks
- Shopping/grocery item → Todoist shopping list (strip the verb: "hummus", not "buy hummus")
- Trivial or already handled → note the reason in the log, still include it

> You may also be given **additional read-only sources** (daily notes and recently-modified
> notes) below the request. Treat items in them exactly like inbox entries for *filing* purposes,
> but never clear, patch, move, or delete those source notes — only the inbox is cleared.

### Step 3 — Audit yesterday's capture-time Todoist adds

If a **"Todoist tasks created at capture time"** block appears below the request, review it. The capture
agent files each memo in the moment, with no vault context, and it can misjudge a log or an idea as a task.
Nothing else ever reviews those, so this is the only correction pass.

For each listed task, decide whether it is genuinely something the user still has to **do**:
- **Genuine task** (an action not yet performed) → leave it completely alone.
- **Not a task** — a record of something already done ("Cycle to work" logged after the ride), a
  measurement or observation, or an idea rather than an action → file the content into the vault where it
  belongs (same rules as any other entry), then call `find-tasks` to get its real ID and `complete-tasks`
  to clear it. Never guess a task ID.

Be conservative: if it is genuinely ambiguous, leave the task in place and note the doubt in the log.
Record every correction in the processing log with the original text and where the content went.

### Step 4 — Archive and clear

Once every inbox entry (and any actionable item from the additional sources) has been handled, and the
capture-time Todoist adds have been audited,
call `archive_inbox` exactly once with a markdown log. If the inbox itself was empty, still call
it once so the run is recorded. Log each processed item:

```
# Inbox Processing — {date}

## [original timestamp from entry]
**Captured:** "original entry text verbatim"
**Action:** Created notes/personal/wiki/garden-redesign.md · tags: [project, garden, home]

## [timestamp]
**Captured:** "call the dentist"
**Action:** Added Todoist task in Health project, due tomorrow 9am
```

### Note conventions

**Vault folder structure** — always use these existing paths, never create top-level folders:
- People notes: `notes/personal/people/Name.md`
- Personal topics, projects, hobbies, wiki: `notes/personal/wiki/topic.md`
- Work notes: `notes/work/wiki/topic.md`
- Daily notes: `notes/daily/YYYY-MM-DD.md`

**Person notes** (`notes/personal/people/Name.md`) — these are the notes the user asks questions of later
("what do I know about X?"), so they must be *findable* and *connected*. Follow the vault's existing shape
(shown below), not an invented one:

```markdown
---
type: person
aliases: [Dad, John Smith]
tags: [person, family]
created: {date}
---
# Notable Info

Lives in Glasgow, works as a hydrologist.
Met climbing on the [[Skye]] trip.
Birthday: 3 March

# Knows

- Introduced me to [[Alex (skye)]]

## See Also

- [[Alex (skye)]]
- [[climbing]]
```

Rules for person notes, all of which matter for retrieval:
- **`aliases:` is required.** List every name the user actually says — nickname, full name, relationship
  word. `aliases: [Dad, John Smith]` makes both `[[Dad]]` and `[[John Smith]]` resolve to one note and makes
  "what do I know about my dad" answerable. If the note exists without aliases, add them while you are there.
- **Every person note must link out.** Before writing, `search_notes` for the people, places, employers,
  and topics the entry mentions, and `[[wikilink]]` the ones that exist under a `## See Also` heading. A
  person note with no links is an island — links are how the assistant later finds context that is not in
  the note itself.
- **Link both ways when it is cheap.** If you add "introduced me to [[X]]", append the reciprocal line to
  X's note too.
- **Append, never restate.** Add new facts under the existing headings with `patch_note` or
  `write_note` mode `append`; do not rewrite a person note to accommodate one new fact.
- Prefer one note per person. Only use a combined note (`TJ and Rachel.md`) where the vault already has one.

**Frontmatter** on every new note (pass as the `frontmatter` parameter to `write_note`):
```yaml
type: wiki
tags: [topic, tag]
created: {date}
```
- Tags: lowercase singular nouns. Represent the *subject*, not the entry type.
- Prefer appending to an existing note over creating a near-duplicate.
- New notes should be substantive — expand on the captured thought rather than just repeating it verbatim.
- Use `list_directory` with a subdirectory (e.g. `path: "notes/personal/wiki"`) to find existing notes before creating.

### Rules

- Process **every** entry — never silently skip (but do record skips in the log with a reason)
- When in doubt whether to create or append, search first
- Always use project IDs from user context; call `find-projects` if the right project is not listed
"""


# Only the tools that are genuinely local to the processor lifecycle. All
# search/read/create/append operations go through the obsidian-mcp tools so the
# model sees exactly one tool per capability.
_PROCESSOR_LOCAL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_inbox",
        "description": "Read all current entries from the Obsidian inbox.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "archive_inbox",
        "description": (
            "Archive all processed entries with their decisions, then clear the inbox. "
            "Call this exactly once — after every entry has been handled."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "processed_log": {
                    "type": "string",
                    "description": "Markdown document summarising each entry and the action taken",
                }
            },
            "required": ["processed_log"],
        },
    },
]


def _build_system_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return llm.build_system_prompt(_PROCESS_INSTRUCTIONS.format(date=today))


async def _dispatch_tool(name: str, inp: dict) -> str:
    if name == "read_inbox":
        return await vault.read_inbox_working()
    if name == "archive_inbox":
        return await vault.archive_inbox(inp["processed_log"])
    guard = vault.overwrite_guard(name, inp)
    if guard is not None:
        return guard
    if mcp.has_tool(name):
        return await mcp.call_tool(name, inp)
    return f"Unknown tool: {name}"


def _unreadable(content: str) -> bool:
    return content.startswith(("Note not found", "Error"))


async def _gather_extra_context() -> str:
    """Build a read-only context block from the daily notes (today + yesterday)
    and any other vault notes modified in the last `recent_context_hours`.

    These supplement the inbox: the processor files actionable items from them but
    must never clear, move, or delete them — only the inbox is archived/cleared.
    """
    now = datetime.now(timezone.utc)
    sections: list[str] = []
    daily_paths: set[str] = set()

    for label, day in (("yesterday", now - timedelta(days=1)), ("today", now)):
        path = f"notes/daily/{day.strftime('%Y-%m-%d')}.md"
        daily_paths.add(path)
        content = await vault.read_from_vault(path)
        if content.strip() and not _unreadable(content):
            sections.append(
                f"### Daily note — {label} ({day.strftime('%Y-%m-%d')}) · `{path}`\n{content.strip()}"
            )

    others = [p for p in vault.list_recent_files(settings.recent_context_hours) if p not in daily_paths]
    if others:
        listing = "\n".join(f"- `{p}`" for p in others)
        sections.append(
            "### Other notes modified in the last "
            f"{settings.recent_context_hours}h\n"
            "Read each with `read_note` and file any actionable or keep-worthy items "
            "that are not already captured:\n" + listing
        )

    return "\n\n".join(sections)


def _recent_webhook_task_adds() -> list[str]:
    """Todoist tasks the capture agent created in the last two days, from the audit log.

    The capture agent decides task-vs-note in the moment with no vault context, and
    a wrong call is never reviewed by anything else — a note misfiled to the vault is
    re-filed here, but a bad Todoist task just sits in the list. This surfaces them so
    the processor can audit them (see Step 3).
    """
    now = datetime.now(timezone.utc)
    vault_root = Path(settings.vault_path)
    adds: list[str] = []
    for day in (now - timedelta(days=1), now):
        path = vault_root / settings.audit_log_path / f"{day.strftime('%Y-%m-%d')}.md"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if "**webhook**" in line and "`add-tasks`" in line:
                adds.append(line.strip().lstrip("- ").strip())
    return adds


_TASK_AUDIT_PREAMBLE = (
    "\n\n---\n\n## Todoist tasks created at capture time (last 48h)\n"
    "These were created by the capture agent as each memo arrived, without vault context. "
    "Audit them per Step 3: leave genuine to-dos alone; for anything that is actually a log, "
    "measurement, observation, or idea, file the content into the vault and then use "
    "`find-tasks` + `complete-tasks` to clear it. Look up every task ID — never guess one.\n\n"
)


_EXTRA_SOURCES_PREAMBLE = (
    "\n\n---\n\n## Additional sources to review (READ-ONLY)\n"
    "Besides the inbox, review the content below and file any actionable or keep-worthy "
    "items into their permanent homes (notes / Todoist), following the same rules and folder "
    "conventions. **Do NOT clear, overwrite, patch, move, or delete any of these sources** — "
    "they are reference input only. Only the inbox is archived and cleared. Before filing, "
    "search the vault to avoid duplicating items that are already captured. Ignore "
    "Templater/Dataview syntax (lines starting with `$=`, ```dataview``` blocks, and frontmatter).\n\n"
)


async def process_inbox() -> None:
    """Stage, process, and archive the inbox, plus file actionable items from the
    daily notes and recently-modified notes (read-only). No-op if there is nothing
    staged and no recent context to review."""
    # The processor's filing quality depends on the obsidian-mcp tool surface; a
    # run without it would flail with no way to search or write notes. The inbox
    # is preserved either way — skipped runs are picked up next cycle.
    if not mcp.has_tool("search_notes"):
        log.error("obsidian-mcp tools unavailable — skipping inbox processing (inbox preserved)")
        return

    current = await vault.begin_inbox_processing()
    # Entries are formatted with "## timestamp" headings; a header-only file has no entries.
    inbox_has_entries = bool(current.strip()) and "## " in current

    extra_context = await _gather_extra_context()
    task_adds = _recent_webhook_task_adds()

    if not inbox_has_entries and not extra_context.strip() and not task_adds:
        log.info("Inbox empty and no recent context — nothing to process")
        return

    log.info(
        "Starting inbox processing (inbox_entries=%s, extra_context=%s chars, task_adds=%s)",
        inbox_has_entries, len(extra_context), len(task_adds),
    )

    tools: list[dict[str, Any]] = [*_PROCESSOR_LOCAL_TOOLS, *mcp.tools()]

    user_message = "Please process my inbox now."
    if extra_context.strip():
        user_message += _EXTRA_SOURCES_PREAMBLE + extra_context
    if task_adds:
        user_message += _TASK_AUDIT_PREAMBLE + "\n".join(f"- {a}" for a in task_adds)

    archive_called = False

    async def dispatch(name: str, inp: dict) -> str:
        nonlocal archive_called
        result = await _dispatch_tool(name, inp)
        if name == "archive_inbox":
            archive_called = True
        return result

    try:
        await run_agent_loop(
            system_prompt=_build_system_prompt(),
            messages=[{"role": "user", "content": user_message}],
            tools=tools,
            dispatch=dispatch,
            model=settings.processor_model,
            max_turns=15,
            max_tokens=16000,
            audit_source="inbox-processor",
            effort="medium",
        )

        if not archive_called and inbox_has_entries:
            log.warning("Processor ended without calling archive_inbox — safety archiving")
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # archive_inbox snapshots the staged inbox verbatim itself, so the
            # raw entries are preserved even without a model-written log.
            await vault.archive_inbox(
                f"# Inbox Processing — {today}\n\n"
                "_Processing ended before archive_inbox was called. "
                "Raw entries preserved verbatim below._\n"
            )

        log.info("Inbox processing complete")

    except Exception:
        log.exception("Inbox processing failed")
        raise
