# Skill Authoring — structure, taxonomy, licensing, confidentiality, editing rules

Load this before creating any skill or making substantial changes to one.

## Contents

- Taxonomy in full
- The Pre-Flight Principle
- Lean Content (including progressive disclosure, the default structure
  above ~500 lines)
- Configuration vs process — the three-container rule
- Documenting an external tool surface
- Licensing
- Versioning releases
- Author Attribution Template
- Confidentiality layers
- Timelessness — shared skills must not capture current state
- Editing skills — always start from the live file
- Declaring a block's scope mechanically
- Verifying relocations and restructures
- Trial design — measuring whether a behaviour fires unprompted
- New skills
- Retiring skills — harvest before you retire
- Principle Propagation

## Taxonomy in full

**Open-source skills** are client-agnostic and methodology-driven.
Recognise one: the methodology works across clients and contexts; no
proprietary information is needed; other practitioners would find it
valuable; it captures a process, not personal preferences. Required
elements: the body identifies itself as open-source; author attribution
block (template below); a licence statement; a feedback/support section
routing methodology feedback to the creator; tool-agnostic language
(capabilities like "browser access", not product names); built-in
enforcement (see Pre-Flight Principle). Default to open-source when a skill
could go either way — strip specifics and generalise.

**Internal skills** contain user/client/project specifics, personal
preferences, or context only the user has. They identify themselves as
internal, need no attribution or licence, and can be shorter and less
formal. They're working documents — keep them current, don't over-engineer.

## The Pre-Flight Principle

Rules documented in a skill are not reliably followed during creative flow.
Every skill with explicit rules needs a verification step where the agent
re-reads the rules and checks its output against them before delivery. When
creating or improving any skill ask: "Does it have rules? Does it have a
mechanism to enforce them?" If not, add one.

**Embedded commands are pre-flight items too — execute before you ship.**
Prose rules and command snippets fail differently: a prose rule is
re-interpreted in context on every run, so ambiguity can be caught at
execution time; an embedded command runs verbatim, unattended, forever —
and a subtly wrong command can read as correct on every re-read
(`git log -1 --format=%cI --reverse` returns the NEWEST commit, because
`-1` applies before `--reverse`, while the plausible reading is "oldest").
Any command embedded in a skill must be executed once against real data,
with its output inspected for plausibility, before the skill file is
saved. An unverified snippet is among the highest-risk lines in a skill:
it ships bugs that no re-read can catch.

**Run the literal string, from a clean shell.** Verifying a command by
running something *equivalent* verifies your paraphrase, not the
artefact. Copy the command out of the skill file and execute that exact
text — from a separately spawned shell, not the session you did the work
in: a working session accumulates environment variables, interpreter
flags, security-policy overrides, PATH entries and a working directory
that the eventual reader will not have, and a command that passes there
can fail on first real use from a normal terminal. Where two shells are
available, pre-flight in the one NOT used for setup. **When the command
under test is a guard, assert on the mechanism, not the outcome.** A
negative test passes only when the failure occurs for the intended reason;
with several guards over one operation, "it was rejected" is satisfied by
any of them. Match the error text or exit code to the guard under test,
choose inputs clearly past the threshold, and test the boundary value
separately. **Confirm a dry-run path exists before pre-flighting anything
with side effects**; where none exists, add one first — a test of a guard
rail must not become the incident the guard rail exists to prevent.

**A verification command that AGREES with you is the one to distrust.**
Executing a snippet once, as above, does not catch this class: the command
runs cleanly and returns a plausible number. A check that contradicts a
strong prior gets investigated; a check that confirms one never does, so a
right answer from a wrong instrument teaches you to trust the instrument.
The usual form is an aggregated count, because a count is not a listing and
a tool that collapses output for readability under-reports without saying
so: `git status --porcelain --ignored` prints one line for an ignored
DIRECTORY, so a `grep -c` over it reports "1" where five files are ignored,
and only `--untracked-files=all` expands it. Before shipping a verification
step that reports a number, confirm what the number's unit is, and
enumerate and read the set whenever enumeration is cheap.

**A list of sources is an embedded command too.** The rule above is easy to
read as being about shell snippets, because those are what "command" looks
like. It is not: it governs anything the skill instructs a future session to
*use* without checking — and a URL, an API endpoint, a database name, a file
path or a tool invocation is exactly that. A skill whose core is "consult
these sources" ships its sources the way another ships a command.

Observed on one such skill: eight sources, all plausible, all written down
without being tried. **Four were unusable** — two returned 403 even with a
browser user agent, one returned "Making sure you're not a bot!", and one
returned an empty single-page-app shell with no results. Every one of them
read as correct on the page, and would have gone on reading as correct on
every later re-read.

Why this is worse than a broken shell command: a broken command fails
loudly, and the session knows to stop. An unreachable source returns
*something* — an empty result set, a login page, a shell — and the natural
reading of an empty result is "nothing matched", which is a finding. The
skill then teaches a future session to report absence where there was only
a blocked door. Execute every source once, against real data, before the
skill file is saved; record which ones were tried and what they returned.

**A verification that cannot come out negative is not a verification.**
Before shipping any check, ask what it would look like if the thing being
checked had failed — and if the answer is "the same", the check is
decoration. The trap has a characteristic shape: the artefact being
inspected was produced or touched by the same session doing the inspection.
Observed: a set of bundles was published to a remote destination and
verified by reading a local extraction folder that the same session had
written the corrected content into. It would have reported "all correct"
had the publication failed completely.

What distinguishes arrival from non-arrival is a **discriminator** — a
value only the far side can produce: a server-assigned identifier, an
upstream timestamp, a digest computed remotely, a field the local copy has
no way to fabricate. Hash-matching a published artefact against your own
source is not one, because both sides are yours. Name the discriminator
when writing the check, and if you cannot name one, say the step is
unverified rather than writing a check that always passes.

## Lean Content

A skill should contain only content that changes the agent's behaviour at
execution time. Move changelogs, credits beyond the author block, long
backstories, and maintainer notes to supporting docs. Do NOT cut examples,
anti-patterns, or worked scenarios — bare rules get violated more than
rules with context. Test: would removing it change behaviour? Keep
per-session rules in the skill body and episodic material in reference
files loaded on demand (progressive disclosure) — a skill loaded every
session is fixed overhead and should be audited like one.

**Declare your own core ceiling, and let it only fall.** A skill opts into
the size ratchet by naming its own bound — `core_max_lines: N` in SKILL.md
frontmatter, or a `.core-ceiling` file beside it. `validate-skill-bundle.py`
then fails when the core exceeds it, with the instruction to move content to
a reference file rather than raise the number. Seed it at whatever the core
measures today: the point is not that every skill reaches some ideal size,
it is that no skill silently grows past where it already is.

A skill that declares neither has not opted in. The count and the distance to
the 500-line target are still reported; nothing is gated. That is deliberate,
and it is the correction to a real defect: the ceiling was briefly a constant
inside the validator, so one skill's budget failed three unrelated skills that
had not changed. **A tool one skill ships for the whole library must not carry
that skill's own constants.** Any threshold in a shared script is either read
from the subject being checked or is advisory — the tell is a gate that fails
on input its author never looked at.

**Progressive disclosure is the DEFAULT, not an option.** Loading happens
in three levels: frontmatter metadata is always in context, the SKILL.md
body loads whenever the skill triggers, and bundled resources load only
when something reads them. The body is therefore a per-invocation tax and
the reference files are not, so content belongs in the body only if it
changes behaviour every time the skill fires. **The split is the target
structure for every NEW or SUBSTANTIALLY REVISED skill, whatever its
size** — episodic material goes to `references/` from the first draft,
not once the body has grown too big to read. A structure rule that
switches on a line count gives one skill two shapes and a boundary every
reader and every tool has to re-derive; starting split costs nothing at
the small end. The ~500-line figure survives in one role only: as the
**retrofit trigger** for an existing, not-yet-split skill — a body past
roughly 500 lines is the point at which retrofitting stops being
optional. Concretely:

- **SKILL.md keeps** the mental model, the small number of rules that
  change behaviour on every invocation, and a pointer list to the
  reference files.
- **`references/` takes** tool inventories, recipes, taxonomies,
  per-variant detail and long gotcha catalogues — anything consulted
  during one kind of episode rather than on every trigger.
- **Each reference file gets a table of contents** once it passes roughly
  300 lines, so a reader can load it and jump rather than read it whole.
- **Every pointer states its load trigger explicitly** — "read before the
  first export", "read when choosing a tool", "read when a review
  triggers" — because a pointer without a trigger reads as optional and
  gets skipped. An unconditioned list of filenames is not progressive
  disclosure, it is a bibliography.

Splitting also creates the seams that make a large skill maintainable: a
monolithic body has no natural edit boundary, so every change touches the
whole file and every edit risks the rest. Retrofitting existing large
skills is a separate, lower-priority job — the default binds immediately
for new work and for any revision substantial enough that the body is
being rewritten anyway.

**The load trigger is a new failure point the monolith did not have.** A
split delivers its saving only if the reference file is actually fetched
when its episode fires. When it isn't, the agent improvises the episode
from the lean core, behaviour degrades, and nothing errors — the content
is all still there, so no check on the content can catch it. So the trial
criteria for any split must test the LOADING, not just the content split:

1. Every pointer is an instruction carrying its trigger ("load X when Y"),
   not a description of what X contains.
2. During the trial, each episode is observed firing in a real session and
   the reference load confirmed — one criterion per episode, so an episode
   that never fired is visibly unproven rather than assumed fine.
3. A missed load is a blocking finding, not a note. The fix is structural
   enforcement — a checklist step the episode cannot complete without —
   rather than firmer wording, per this file's own pre-flight principle.

Add a runtime backstop as well: instruct the agent to log an observation
whenever it notices an episode was handled without its reference loaded.
Without it the negative case has no recording channel, and the trial
cannot distinguish "not yet observed" from "not happening" (see Trial
design below, which governs how such a trial must be instrumented so
naming the trigger does not become the intervention).

## Configuration vs process — the three-container rule

A skill that needs values specific to one person, team or installation —
an identity, an account name, a private blocklist, a set of local
exclusions — needs **three containers, not two**. With only the skill and
its invoking prompt available, the specifics must land in one of them: in
the skill they block publication and generalisation; in the prompt they
keep it fat and drifting. The third container resolves the tension:

1. **The skill** holds the process, and *reads* configuration.
2. **A private config file** holds the specifics. The skill names its
   expected location and never ships it.
3. **The invoking prompt** holds only the trigger.

State the precedence explicitly: config wins for *configuration*, the
skill wins for *process*, and config can never relax a rule. Give the
skill a first-run fallback that asks for the values and offers to write
the file when none exists — those few lines are what make the skill
usable by someone who is not its author. The test for whether a line
belongs in config rather than in the skill: **would a different person
running this skill need a different value here?** Measured on a real
conversion, the extraction is smaller than it looks — most identifying
lines are a proper name where a role noun ("the maintainer") reads
identically.

**When a prompt or config duplicates a skill's rules for safety, replace
the copy with a stop condition, not a shorter copy.** Duplication
defended as insurance is the least audited kind — the argument for
copying it is also an argument against questioning the copy, so the
copies carrying the most important rules are the ones most likely to be
silently stale (found in practice: a third copy of a rule set still
carried a convention its source had superseded, and nothing detected it).
A copy of a rule can drift out of agreement with its source; a refusal to
proceed without the source cannot. "If the skill did not load, stop and
report — do not proceed from this prompt" is one line and has nothing to
drift. Keep at most the irreversible invariants stated in both places,
and say in the prompt that it deliberately carries no fallback copy, so a
future editor does not helpfully re-add one.

## Documenting an external tool surface

Applies to any skill that documents a surface someone else owns — an MCP
server's tools, an API's endpoints, a platform's interface. The value of
such a skill is that its claims were OBSERVED rather than inferred from
the vendor's own descriptions, and the first version is almost always
written after exercising only part of the surface. Documenting the
untested majority in the same voice as the tested minority silently
promotes vendor copy into apparent field findings, and a later reader has
no way to tell which claims carry evidence. Dropping the untested items
isn't the fix either — they still need listing so the agent knows they
exist.

1. **Mark every item, not just the exceptions.** Give each documented
   tool, endpoint or screen a visible per-item marker for verified-in-
   practice versus unexercised, and state the key near the top of the
   inventory so the distinction can't be missed. Marking only the
   unexercised ones fails: an unmarked item reads as absence of
   information rather than as evidence.
2. **Write unexercised items in a different voice.** What the vendor says,
   plus what can be safely inferred about risk — "the docs describe X";
   "assume this evicts current state" — never as observed behaviour. No
   worked examples, no timing or quota claims, no gotchas for something
   nobody has run.
3. **Make the marker a work queue.** Add an explicit instruction that when
   an unexercised item is used, the agent reports back what it actually
   did so the entry can be rewritten and its marker promoted. Without
   that, the markers ossify into a permanent disclaimer and the skill
   never converges on evidence.
4. **Promote on revision.** Any substantial edit to the skill re-checks
   the markers against what has been exercised since — promotion is part
   of the edit, not a separate project.

Principle: documentation that covers less but says which claims are tested
is more useful than documentation that blends the two, because the
reader's decision — trust it, or verify first — depends entirely on that
distinction, and it is unrecoverable once blurred.

## Licensing

Include a licence statement in the preamble and a LICENSE file with full
text. Options: **CC BY 4.0** (prose/methodology skills; share and adapt
with credit — recommended default), **MIT** (code-heavy, permissive),
**Apache 2.0** (MIT plus patent grant), **CC BY-SA 4.0** (share-alike
derivatives), **GPL family** (strong copyleft). The author chooses; the
requirement is that there is one.

**Private client sharing** is a third channel with its own rights framing:
a client-agnostic skill shared privately with one client is NOT open source
and NOT internal. Keep the attribution block; replace the licence statement
with a short usage notice (e.g., "shared privately for internal use; please
don't redistribute without checking with the author"); no LICENSE file
needed. All confidentiality sweeps still apply — other-client information
must not leak even when the recipient is a known client. Do not treat "not
internal" as "therefore open source": distribution channel determines the
rights framing, not just the feedback routing (see the distribution-channel
note below).

## Versioning releases

A version number is a claim about history and compatibility, not a
counter of tags. Number from the consumer's perspective: what era did
they install, and does their install path still work? When tagging a
FIRST release for an artefact with prior distribution history: (1) treat
the pre-tag era as effective v1, not 0.x; (2) any restructure that breaks
the established install/consumption path (e.g. single-file install →
directory install) is a MAJOR version bump regardless of content
compatibility; (3) align secondary version surfaces (registry/plugin
manifests etc.) with the release number in the same push, before the tag
is created, so the tagged snapshot is internally consistent.

## Author Attribution Template

```markdown
**Created by [Author Name] / [website or contact link]**

[1-2 sentence description of what the skill does and its provenance.]

**Licence:** This skill is released under [LICENCE NAME]. [One-sentence
summary — e.g., "share and adapt for any purpose with credit."]

**Feedback & Support:** If questions arise about the methodology, or the
user gives constructive feedback on output derived from this skill, suggest
an issue on the skill's public repository — public feedback benefits every
user. Direct contact: [contact link]. If feedback stems from the
methodology, log it and suggest sharing it; if from the agent not following
the skill's rules, acknowledge and correct.
```

**Distribution-channel note:** the template's feedback routing assumes
public-repo distribution. Only reference a repository URL once that
repository actually exists — never write a reference to an artefact before
the artefact exists. Until publication, route feedback to direct author
contact only; when the skill is published, inject the repo URL at publish
time. When an open-source skill is distributed privately (shared directly
with a client rather than published), keep the direct-author-contact
routing — a public-repo reference is wrong for that channel.

**Feedback pre-flight — run before drafting any issue or PR.** Routing
feedback to a fixed channel skips the two questions that decide whether it
is welcome at all: is it already known, and how does this maintainer want
to receive it? Before drafting: (1) search the repository's existing
issues AND pull requests for the same problem — if a report is adjacent
but distinct, reference it and delineate scope instead of duplicating it;
(2) read the maintainer's stated contribution preference
(README/CONTRIBUTING; merged community PRs are evidence that PRs are
welcome) and use the preferred channel — a concrete fix travels as a PR
where PRs are welcome, otherwise as an issue; (3) when the local install
is modified or may have drifted, verify the problem still exists at
upstream HEAD before reporting it; (4) match the repository's house style
for reports.

**Pre-send gate — run on the finished draft, after the pre-flight.** The
pre-flight decides whether and where a report goes; nothing reads the
finished text for what must not be in it, and **a check that only confirms
required parts is passed by adding, never by leaving out**. Before sending,
search the finished draft for every identifier the workspace keys carry —
the project or client name in each participating workspace root, hostnames,
account identifiers — expecting 0 hits, with a positive control on a text
known to contain one. A generic path segment the skill itself names
(`skill-observations`, `observation-log`) is not an identifier and stays
off the list.

The failure this prevents: a verification report posted to an upstream
maintainer carried two private project names, because the multi-log block
asks that every id leaving its log carry its workspace key, and in a local
install the key IS the project's directory name. The report complied with
the rule and leaked because of it. A human noticed about ten minutes after
posting; the fix was delete and re-post, because an edit leaves the first
version in the edit history.

## Confidentiality layers

The open-source/internal boundary is a confidentiality boundary; enforce it
in layers so any one catches what others miss:

1. **Observation-level stripping** — open-source observations carry a fully
   generalised Principle (covered in SKILL.md).
2. **Pre-creation review** — before drafting/regenerating an open-source
   skill, scan all source material for client names, URLs, domains,
   internal terminology, identifiably-specific structures; replace with
   generic equivalents first.
3. **Post-draft sweep** — a separate re-read focused only on leakage.
   Read the examples, usage strings, default arguments and fixtures
   **first** (layer 8 says why), then proper nouns besides the author,
   domains/URLs/project identifiers, vertical details that narrow the
   client, examples traceable to a real project.
4. **Structural principle** — when in doubt, remove. Slightly more generic
   beats slightly leaky.
5. **Cross-product re-identifiability sweep** — the final pass before any
   public release. Individually-sanitised examples can combine to identify
   a client (enumerated counts matching a public client list; specific
   numbers in a thin vertical; thinly-disguised placeholder names in the
   same vertical as a real client). List every example and its fields
   (vertical, geography, numbers, timing, counts); ask whether a reader
   with the author's public client list could map them; mitigate by
   blurring counts, widening verticals, using illustrative ranges, or
   consolidating into composites. Run this mechanically — the author is the
   least reliable judge because they know the ground truth.
6. **Recipient-perspective pass (client-shared artifacts)** — the layers
   above guard one direction only: other parties' information leaking IN.
   An artifact prepared FOR a specific recipient also leaks the author's
   own workspace OUT: references to internal working files (analysis
   workbook filenames, private docs) that the recipient never received are
   both confusing (citing sources the reader can't open) and factual
   errors (e.g. describing them as "delivered"). Check: every referenced
   file, document, or source must be either included in the share or
   actually in the recipient's possession; replace internal-artifact
   citations with a plain description of the analysis they came from.
   Test: "can the recipient open or verify every reference in this
   document?"
7. **Cross-skill reference scan (published skills)** — a skill written
   inside a personal library accumulates pointers into sibling skills
   ("see {other-skill}'s Hyperlink markers"). Inside the library they
   help; published, they dangle — the reader cannot resolve them, and
   they leak the existence and naming of the author's internal tooling.
   A published skill must either work standalone or declare its
   dependency on a specific *published* open-source companion. Scan for
   references to other skills by name; each hit is (a) a declared
   published companion — keep, ideally with a repo link; (b) inlineable —
   pull the needed rule into the text; or (c) internal tooling — rephrase
   to capability language ("whatever presentation tooling the subagent
   runs"). Make it mechanical in any publishing workflow: grep the
   content against the list of installed-but-unpublished skill names.
   Every pointer in a published document must resolve in the reader's
   world, not the author's.

   Authoring-time corollary — sibling-sync and family notes. The family
   machinery legitimately writes sync notes into skill files so shared
   cores don't drift, and those notes are the expected shape of a
   violation of this layer: the sync rule and the standalone rule are
   each followed correctly and still jointly produce an unpublishable
   skill neither anticipated. When adding a sibling-sync or family note
   to an open-source skill that is published or a publication candidate,
   phrase the family reference in capability language from the start
   ("the sibling skills implementing this methodology for other
   platforms") or point generically at the private family registry —
   never name installed-but-unpublished skills, because the publishing
   workflow's standalone scan blocks those names, the sync itself becomes
   the publication blocker, and the only fix is rewording at the live
   skill. When a maintenance mechanism writes cross-references into an
   artefact, it must respect the publication boundary of the artefact it
   writes into: internal registries can name internal things; public
   artefacts reference them by capability, not by name.

8. **Examples are synthetic by construction, not scrubbed afterwards** —
   examples, usage strings, default arguments, test fixtures and sample
   payloads are the highest-risk text in a skill and the least reviewed.
   Their whole purpose is to have been real: an example is pasted from a
   working run precisely so that it works, so live data survives a scrub
   there more reliably than anywhere else. Write them synthetic at
   authoring time — a generic label and a round number in the shape the
   argument or payload actually takes, which still teaches the format.
   This layer is prospective, and it has to be, because no term-based
   scan can back it up: a confidentiality scan matches names, domains and
   handles, and a **number** is not one of those. Nor can such a scan
   usefully be keyed on a vertical or category noun — a generic category
   word fires on nearly every file, so adding it to the tripwire list
   trades one failure mode for a worse one. A clean scan is therefore
   evidence about the listed terms only, and says nothing about whether
   an example carries a real allocation, count, date or sample payload.
   This is the house rule that every instrument gets the same guard, in
   its confidentiality form: an empty result from an instrument that
   cannot see the object is not evidence of absence. So the sweep in
   layer 3 reads examples first, not last, and the pre-publication read
   includes a human pass over every example, usage string and fixture in
   the tree — the one check no script can do for the author.

## Timelessness — shared skills must not capture current state

Any skill leaving the author's own maintenance loop (published, or shared
with a client for ongoing use) has no update cadence — undated
present-tense claims become silently wrong, and an agent will act on
them. "A recommendation on how it should be is timeless; a capture of the
current state is not." Rules: (a) recommendations and rules — keep; (b)
dated historical facts ("in June 2026 the share was ~50%") — keep, they
stay true as history; (c) undated/present-tense current-state claims
("X is currently blocked", "the site now does Y") — replace with a check
instruction ("verify the current state of X before acting"). Sweep
pattern: grep for "currently", "right now", "as of", "now", and
present-tense state verbs near infrastructure nouns. (Internal client
dossiers are exempt: they live in a maintenance loop where current-state
capture is the point.)

**First-party observation dates: prefer verification-based phrasing in
published skills.** Rule (b) above — dated facts stay — holds for
third-party-published facts (vendor changelogs, API announcements,
calendar-date examples). It does NOT extend to dates that reveal when the
AUTHOR observed, verified or reviewed something ("as of April 2026", "a
June 2026 re-check"): in a published artefact those are metadata about
the author's process, leak the internal review cadence, and read as
staleness markers. Replace them with verification-based phrasing — "at
last verification", "a later re-check found" — plus an explicit
instruction for how to re-verify against the live source; readers need to
know how to re-check a claim, not when the author last did. The same rule
covers commit messages on published repos. And make the check mechanical:
this rule was violated during fluent drafting while fully documented, so
any publishing workflow must scan public artefacts for **dated first-party
claims in any notation** before committing — a scan, not a reminder, and
specified by what it forbids rather than by one shape it takes. Month-name
plus year is one shape; `2026-06-10`, `10/06/2026`, `Q2 2026` and "as of
2026" are the same claim, and a grep written from the rule's own example
matches only the example. Put the pattern set in a script, where it can be
extended in one place, and record which patterns ran — a green scan whose
coverage is unstated is unauditable.

**The internal-document exemption assumes a maintenance loop that is only
prose.** The taxonomy sanctions internal skills that describe one
installation, and those may legitimately name current state. The unstated
assumption is that someone re-checks that state — but nothing fires when it
drifts, so "internal, therefore dated facts are fine" becomes "internal,
therefore permanently wrong".

Observed: an internal skill opened with a hardcoded version number of the
system it described; the deployed system moved on and the skill's first
paragraph was wrong within a day. It was the second time the same number
had gone stale, and five further copies sat in memory files and handover
notes. The first paragraph is where it does most damage, because it is read
before anything that might contradict it.

So an internal skill may carry current state only with a **named
re-check**: what re-derives the value, and when. "Version X (re-derive from
`<command>` at each review)" is acceptable; a bare number is not. Where no
re-check exists, do not record the value — record how to obtain it.

## Editing skills — always start from the live file

1. The live file is the authoritative source: in Claude Code,
   `~/.claude/skills/{skill}/SKILL.md`; in Cowork, a read-only mount at
   `.claude/skills/{skill}/SKILL.md` (writes fail with EROFS by design).
   **That guard exists in Cowork only.** In Claude Code and most
   local-filesystem environments the same files are ordinary writable
   files and nothing stops the write — the discipline is the only thing
   preventing the overwrite. Assume you are in the unprotected case unless
   you have seen an EROFS yourself. Do not edit skill files in place, in
   any environment — staging-only is what keeps the autonomous review
   safe, and the way to make it hold where no guard exists is to begin
   every edit with the copy (`mkdir -p` the staging dir, `cp` the live
   file in, `diff -q` to prove it matches), so the live path is never the
   one in hand. Scope and precedence: staging-only governs every context
   and every size of change. The direct-apply clause in SKILL.md ("Acting
   on Observations") decides *when* a small change is made — now, rather
   than at the next review — never *where*; it does not license an
   in-place edit.
2. Always base edits on a fresh read of the live file — never a workspace
   copy, prior draft, or memory.
3. Before overwriting any staged/workspace copy, diff it against the live
   file; if they differ, rebase your edits on the live version. (Observed
   failure: an update built on a stale snapshot silently dropped two
   sections added to the live skill the same day; only a pre-merge diff
   caught it.)
4. Before any cross-copy sync or publish work (live install, workspace
   copies, published repo/branch), enumerate all copies of the artefact
   and establish freshness PER COPY from evidence — mtime, content
   probes, hashes — never from role ("the repo", "the live version");
   then pick the base explicitly. During multi-pass work in interactive
   sessions, re-verify the baseline before interpreting any diff: a diff
   that SHRINKS against a supposedly-fixed baseline means the baseline
   absorbed earlier changes (e.g. the user installed a staged update
   mid-session), not that edits vanished. Treat unexpected diff-stat
   direction as a baseline-moved signal, not an error in the edits.
   Recorded measurements obey the same rule: when a handoff records a
   test-merge result (a conflict count, a diff-stat), record it WITH the
   base commit or install date it was measured against, and re-run the
   test merge against the current base before planning around it — treat
   the old result as a pointer to where conflicts will cluster, not as a
   count. When a change's whole purpose is to make a class of machinery
   unnecessary, every conflict hunk in that class resolves the same way
   (take the change's side), so classification matters more than the
   count.
5. Stage every update to
   `[workspace folder]/skill-updates/[date]/[skill-name]/` (a second round on
   the same day takes a discriminated anchor — weekly-review.md, Delivery) — the FULL
   skill directory (SKILL.md plus references/, scripts/, assets/ where
   present), never SKILL.md alone — and present it for review and
   installation; nothing goes live until the user installs it. Where no
   presentation/upload tool exists (e.g. Claude Code CLI), present the
   staged path and a change summary in chat instead; staging-only applies
   in every environment — it's the review loop's safety property, not a
   filesystem constraint. For **every**
   skill, zip the staged directory into a `.skill`
   bundle and present the bundle, never the bare SKILL.md — one delivery
   format regardless of file count: a single-file
   delivery convention applied to a multi-file skill truncates it
   silently (the install succeeds, the skill loads, and the missing
   pieces only surface when a reference load or script call fails
   mid-task), and a convention that switches on file count leaves that
   boundary to be re-derived every time. **Pre-delivery gate — three items, checked at the moment of
   delivery, not just at drafting time:** (1) every UNQUALIFIED
   `references/`, `scripts/`, `assets/` path in the staged SKILL.md body
   has its file in the staged set — a path qualified with an owning skill
   name (`<skill-name>/references/<file>`) is a cross-reference, exempt by
   construction, and that is the convention for citing another skill's
   file (weekly-review.md, Delivery); (2) the delivery artefact
   is the `.skill` bundle — bare file links fail this gate even when all
   files are staged; (3) frontmatter constraints — measure the description
   (the FOLDED value, not the raw YAML block) and fail the delivery above
   1024 characters, with a soft warning above ~900 so a near-boundary
   description gets tightened before it becomes someone else's install
   error. Trigger coverage survives compression: the fix is tightening
   phrasing, not dropping triggers. Measure every skill in the delivery
   set, not just the one that failed — the pressure toward trigger-rich
   descriptions puts others near the boundary too, and only measuring the
   set reveals it; (4) `name` is kebab-case and matches the containing
   directory; the frontmatter parses as YAML with both required keys. A
   `description:` (or `name:`) value that contains `: ` anywhere, or that
   starts or ends with a YAML-special character (`#`, `*`, `&`, `!`, `|`,
   `>`, `{`, `[`, `'`, `"`), must be double-quoted or reworded to avoid
   the colon — an unquoted `: ` inside the value turns the rest of the
   line into a nested key and the whole block stops parsing. The
   validator reporting "`name` missing" on a frontmatter that visibly has
   a `name:` line is the signature of an unparseable block, not of a
   missing field: fix the quoting, not the name. The quoting rule for the
   observation-log frontmatter does not travel here by analogy; it is
   stated here because this is where skill frontmatter is authored;
   (5) the packed archive's member paths contain no backslash — read as
   RAW central-directory bytes, because CPython's `zipfile` normalises
   `0x5C` to `/` on read and reports a malformed archive as clean; Windows
   `Compress-Archive` produces exactly this defect for any skill with a
   subdirectory. `scripts/validate-skill-bundle.py` implements all seven as
   assertions and packs a well-formed bundle on any platform; run it where
   Python is available. Generally: any hard limit the consuming platform
   imposes belongs in this gate as a measurement compared to a bound in
   the same step, not as a rule the author is expected to remember — an
   unasserted metric does not merely miss defects, it manufactures
   confidence that none exist. State limits as numbers and label them as
   the consumer's. **A recommended check carries three measured numbers.**
   When proposing an automated check or a new gate rule, run each
   candidate over the existing corpus first and record, per rule: (a)
   hits on today's corpus, (b) true defects among them, (c) the
   normalisation needed to remove the rest. None of that is knowable from
   the idea alone — measured on one corpus, one candidate class produced
   35 hits that were nearly all locale artefacts, two produced 0 hits
   (free locks, addable today), one produced 7 of which 2 were defects
   and 5 legitimate content a naive rule would flag forever. A proposed
   guard is a claim about the future, and the cheapest evidence for it
   already exists in the artefact being audited; a rule with an
   unmeasured false-positive rate is a proposal to make people ignore a
   test. (Reading this rule while drafting does not enforce it
   at delivery; run the gate as the last step before presenting.)
   Packaging hygiene: before zipping, sweep the staged tree for build
   artefacts (`__pycache__/`, `*.pyc`, `.DS_Store`, `.~lock.*`) left by
   in-session checks, and read the archive listing back after zipping —
   the listing catches two defect classes, leaked artefacts AND wrong
   path separators, and it gets read only for the one you name, so check
   for both explicitly.
6. When seeding a staged copy by copying from the read-only mount, reset
   write permissions immediately — the mount's read-only mode travels with
   the copy, for directories as well as files, and the follow-up edit
   otherwise fails with a permission error. For a SINGLE FILE, `cp`
   followed by `chmod u+w` (or `cp --no-preserve=mode`) works. For a
   DIRECTORY TREE, `cp --no-preserve=mode` is NOT reliable in this
   environment: it has failed with "Permission denied" while creating
   files inside copied subdirectories (`references/`, `scripts/`) even
   though top-level files copied. The only verified sequence for trees is:
   `mkdir -p` the directory structure first, then per-file `cp`, then
   `chmod -R u+w` on the staged path. A workaround documented as
   equivalent to another must be re-verified per failure surface — an
   option that works for single files can still fail for directory trees.
   Use the exact snippet in `weekly-review.md` Step 5 rather than
   reconstructing it: strip the live prefix **without** a trailing slash so
   the top-level directory maps to the staged root, and keep `IFS=` on the
   read loops for paths containing spaces. Both are easy to get wrong from
   this description alone, and the resulting phantom directories cannot be
   deleted on a mount that denies `unlink`.
7. Match process rigour to the change: complex/open-source/uncertain design
   → use the skill-creator if available; internal skills with requirements
   already established in conversation → write directly, flagging
   substantial changes for review.
8. **Before staging a rewrite, enumerate the surfaces outside the skill
   directory that restate its rules.** A rule's home is a location; its
   reach is every artefact that paraphrases it, and those live in
   directories the owning skill cannot see, so a grep across the skill
   folder — however thorough — reports clean. The surfaces that matter are
   the ones that execute independently of the skill: scheduled-task prompts
   above all, then activation fragments in CLAUDE.md or its equivalent,
   saved shortcuts, and handoff docs. Enumerate them (a scheduled-task
   listing returns a path per task, so this is one call plus a grep), then
   either update each in the same round or replace the copy with a stop
   condition per "Configuration vs process" above — including the clause
   that says the omission is deliberate, or a later editor helpfully
   restores the summary. The asymmetry is what makes the step
   non-negotiable: a stale copy never fails loudly. It runs, plausibly,
   doing the superseded thing, and the only witness is the user noticing an
   old behaviour reappear weeks after asking for it to stop. And drift is
   fast — it is the source's rate of change that predicts it, not the
   copy's age (observed: four days from an anti-drift rule being written to
   a live violation of it, in a skill whose rules were under active
   rewrite, found only because the prompt happened to be opened for an
   unrelated reason).

**Verifying an edit is a separate problem, and the loader works against
you.** The rules above cover editing safely; nothing covers confirming
afterwards that the edit took. Three things get conflated — the file on
disk, the copy the loader injected into this session, and what a fresh
session would receive — and they can all differ at once.

Observed, in one session: the loader served a *previous lineage* of a
`SKILL.md`, missing an entire section and stating the opposite rule on a
formatting standard, while the file on disk (hash-verified, edited 45
minutes earlier in that same session) carried the corrected text. Answering
from the injected copy would have given the user exactly the wrong answer
on a normative question. It was caught only because someone grepped the
file on disk for a literal quote.

The obvious remedy fails too: launching a subagent to simulate "a fresh
session" does not break the freeze. A subagent's context starts cold but
its skills come from the **same snapshot**, so it reports the same stale
content with the added authority of an independent-looking check.

So: **verify an edit by reading the file on disk**, never by asking the
loaded skill what it says and never by asking a subagent. And when the
question is what a *future* session will receive, only a genuinely new
session answers it — the installing session structurally cannot.

## Declaring a block's scope mechanically

A section of an instructions file, a skill, or a shared config often applies
to only some of the work done on a machine. The usual approach is to trust
the agent to notice the irrelevance from context. That fails quietly, and it
fails in the direction that does damage: advice written for one domain is
applied confidently to another.

Instead, open the block with a **detectable condition** — something the
agent can check rather than infer:

> Applies ONLY when the project has a `package.json` with a `react`
> dependency. In every other project, ignore this section entirely.

Observed working in both directions inside a single session on a machine
serving two unrelated lines of work: the section stayed inert while the
working directory was a documents folder, and governed the whole workflow
once a React project was open.

**State the negative branch explicitly**, because it is the half that gets
left out. "Applies when X" reads as complete and leaves the agent to decide
what happens otherwise; "and in every other case ignore this entirely" is
the sentence that makes the block safe to carry. A condition with no stated
negative is a preference, not a scope.

Prefer conditions the agent can test cheaply and locally — a file's
presence, a path segment, a declared dependency — over ones needing
judgement ("when working on frontend tasks").

## Verifying relocations and restructures

Two modes, and picking the wrong one is why a good move looks broken.
**Verbatim relocation** — the content moves unchanged — is verified line
by line, with the two-tier check below. **Merge or extraction** — several
overlapping sources are reconciled into one destination, wording is
rewritten, tool or client names are generalised into capability language
on the way — is verified section by section, because every moved line
legitimately fails an exact match and the line-grep tier reports total
loss on a perfect merge.

For a merge or extraction, replace the line-grep tier with a **mandatory
source-section → destination coverage table, one per mover**: every
heading of every source is accounted for as moved / merged-into /
left-in-companion / dropped-with-reason, alongside the content only one
source carried, the generic content deliberately left behind, and the
substitutions made. Keep the word-count sanity check per file — it is
cheap and catches wholesale omission. And **the mover of a split section
writes both destinations**: whoever moves material out of a skill also
writes that skill's remainder for the same sections. Complementarity —
nothing in both places, nothing fallen between them — cannot be checked
afterwards as cheaply as it can be produced, and splitting the two halves
across two authors guarantees the seam nobody owns. The coverage table
is the artefact a merge is admitted on; a diff is not.

For a verbatim relocation, "nothing was lost" is checkable mechanically —
but only with a two-tier check:

1. Enumerate every added/moved line via `diff` of the old base vs the new
   base.
2. Exact-match each non-empty line against the restructured file set:
   `grep -qF -- "$line" "$file"` — the `--` is required, because markdown
   lines starting with `-` are otherwise parsed as options, `grep` errors
   out, and the line is scored as missing (31 false losses beside 35 real
   ones in one run). The same applies to every embedded snippet that
   interpolates file content into an argument position.
3. For misses, substance-check via a distinctive mid-line substring before
   concluding loss — most misses are container artifacts (heading-level
   changes, list-to-prose adaptation, re-wrapped lines splitting a phrase
   across newlines), not real losses.
4. Word-count sanity check per file.
5. Normalise line endings before any of the above. A checkout made with
   `core.autocrlf=true`, or a download that kept LF beside a clone that
   rewrote to CRLF, makes `diff -rq` report every text file changed and
   `grep -qF` score every line as missing — while the two PNGs in the
   same bundle compare identical, which is the tell. Compare with
   `diff --strip-trailing-cr` (or `dos2unix` / `tr -d '\r'` into temp
   copies) and strip the `\r` from each `$line` before grepping; a
   verification whose only difference is the line terminator has found
   nothing.

**Cross-reference-dense monoliths: keep section numbers global, add a
map.** When the document being split is dense with internal
cross-references (§13.6, §8.3.3b …) that will span the new file set, do
NOT renumber per file — renumbering breaks every reference or forces a
risky rewrite pass. The verified pattern (a ~5k-line skill split into a
core + 8 reference files with zero content loss): keep the original
section numbers global across the whole file set; move content by
verbatim line-range extraction so nothing is retouched; add a "Section
map" table to the core resolving §N → file; give each reference file a
one-line header pointing back to the map. The identifier space is an
interface — keep it stable across the split and add a resolution layer,
rather than renumbering content to match its new container. Corollaries:
the map's load triggers must be phrased as mandatory ("read X before
running Y"), and a core-size overshoot past the ~500-line guideline is
acceptable when the overage is genuinely per-invocation principle content
— record that reasoning where the next editor will see it.

One tier alone either misses losses (substance-only) or cries wolf
(exact-only). **The checker is code too, and the least-tested code in the
pipeline** — written once, for one run, never exercised against a fixture.
Count record headers by matching per line (split, then anchor), not with a
`^`-anchored regex over whole-file text, whose multiline-flag semantics
vary by language and fail silently to "1". Before trusting any checker's
verdict, calibrate it: assert that it reproduces a KNOWN count on the
untouched original. A checker that fails calibration is a false alarm
today and a silent pass tomorrow. Additionally, inventory the original's enforcement
mechanisms (checkpoints, assertions, invariants, mandatory-write rules,
defaults) as an explicit checklist — compression preferentially destroys
enforcement machinery because it reads as redundancy — and sweep any "pure
restructuring" change for net-new behaviour, which hides well in a large
rewording diff.

**Build a relocation as a re-runnable derivation, not a one-off patch.** A
relocation verified against the base as it stood at build time is correct
only for that base. If a concurrent edit lands on the live file before
delivery — inside the very block being moved — laying the finished target
files over the new tip **silently reverts it, with no conflict reported**,
because nothing in the delivery compares against the tip it is landing on.
Git will not save you here: the relocation is a set of whole files, not a
patch, so there is nothing to conflict.

Observed: a split of one file into a core plus new reference files, built
and verified three ways (line-range diff against the old base, exact-match
against the new files, word-count check), all of which passed — against the
base from an hour earlier. One line inside a moved block had changed in the
meantime.

So express the relocation as something **re-derivable from whatever the
live base currently is** — a script, or a procedure precise enough to
re-run — and re-derive immediately before delivery rather than trusting the
earlier verification. Where a manual patch is genuinely the only option,
diff the moved regions against the live tip at delivery time and stop on
any difference. The verification that matters is the one against the state
you are landing on, not the state you started from.

## Trial design — measuring whether a behaviour fires unprompted

When a change is on trial and what's being measured is whether the agent
does something ORGANICALLY (loads a reference file because the work called
for it, applies a rule without being told), record the trial's trigger
condition somewhere the agent under test does not read — a maintainer note
or the review report, never a task file, CLAUDE.md, or a handoff prompt.
Add an explicit invalidation clause to the trial's definition: any session
whose priming text names the trigger condition, or instructs the behaviour
directly, is discarded rather than counted either way. And instrument the
negative case deliberately — log "substantive work in scope, no organic
load" as its own data point. Principle: an instruction that names what is
being measured is an intervention, not a description; priming text and
measurement apparatus must live in separate channels, or the trial measures
compliance with the prompt. Corollary: null results need active recording,
or the trial cannot distinguish "not yet observed" from "not happening".

## New skills

**Building next to siblings: separate the inherited steps from the
necessary ones.** A new member of a family inherits its siblings' shape,
and consistency with the family is a legitimate design value — which is
precisely why the inherited-but-unnecessary step survives review. It does
not read as wrong; it reads as belonging.

Observed: a family whose every member opens by identifying a governing
institution, because every member's output depends on which rulebook
applies. The new member copied that step and required the institution to be
resolved before any work. The user's correction was one line: *don't ask
about the institution — just ask how many items I want of each kind.* They
already knew the numbers. An inherited question had turned a one-line input
into a two-step interrogation.

The person who detects this is the one who has to answer the question, and
by then it has shipped. So make it an explicit authoring step: for each
step copied from a sibling, state **what this skill's output would lose if
the step were removed**. A step whose answer is "consistency" is inherited,
not necessary — drop it, or demote it to an optional branch.

**Before authoring a skill for a library, tool or project, check whether
the project already ships one.** The reflex on "make a skill for X" is to
read X's documentation and write a `SKILL.md` from scratch; many projects
now carry their own agent skill in-repo — commonly under `agent-skill/`,
`.claude/skills/`, `skills/`, or named in the README — and hand-authoring
beside it duplicates work, drifts from the maintainers' version and misses
their updates. One search of the upstream repository (those paths, plus
"skill" in the README and release notes) settles it. If an official skill
exists, install it and put only the local delta into a companion, exactly
as for any other upstream-maintained skill (weekly-review.md, Step 2);
if it does not, author, and consider offering the result upstream.

Use the skill-creator when available, passing the observation(s) as the
brief. Determine type early: open-source → strip and generalise; internal →
include specifics freely; uncertain → default open-source and let the user
add internal detail afterwards.

**First decide whether the skill joins a family, and if it does, read the
siblings before drafting.** Intent → interview → draft is right for a
genuinely new skill and wrong for one joining an existing family (the
registry is `skill-observations/skill-families.md`; see
`observation-log.md`), because the family already contains most
of the answer, distributed unevenly across its members. Drafting from the
current session's experience and consulting the siblings only for the
tool-agnostic parts you happen to remember produces a third divergent
member: observed, a new skill carried a category neither sibling had and
omitted two that one sibling had, taking a family from two inconsistent
members to three. The cost is asymmetric and front-loaded — reading two
siblings first is minutes; reconciling three divergent skills afterwards
means re-reading all of them, deciding per rule whether each divergence is
intentional, and editing in three places, with the divergences hardest to
spot precisely because each skill reads as coherent on its own.

Read every sibling in full, draft against them, and produce a short
**reconciliation note** as part of the deliverable, not as optional
homework: (a) content taken from the siblings; (b) content the new skill
adds that the siblings should also get — logged as observations naming
those siblings, never silently absorbed; (c) sibling content deliberately
omitted, with the reason. The note is what converts a new family member
from a source of drift into a correction of it. Then update the registry
with the new member.

Two corollaries. Where the family's shared material is substantial, the
pre-draft read is the natural moment to ask whether it should be extracted
into a core skill the members load (`shared-core`) rather than restated a
third time — the question is cheapest to answer before the third copy
exists. And the general form, which reaches well past skills: **a new
instance is the best available audit of the existing set.** Drafting it
forces the shared material to be restated from scratch, and every place
the fresh statement differs from a sibling's is either an improvement or a
gap. Whenever a new instance joins a set of parallel implementations, read
the set first — not because the new instance needs their content, but
because it is the only moment when the differences between all of them are
being actively thought about by anyone. Skipping the comparison wastes the
audit and creates the divergence.

**When the extraction does happen, work brief-first.** Write the split
rule once, before any file is touched — the test that decides where a
sentence goes (*could this sentence survive having the platform's name
removed?* → core; otherwise → companion), the confidentiality rules, the
generalisation substitutions, and any global numbering rule — and hand
that one brief to every mover, rather than letting each rediscover the
rule from the material in front of them. Then give each destination file
one mover, and require that mover to produce **both** halves of every
section it splits (the core text and the companion's remainder) plus the
coverage table from "Verifying relocations and restructures". The
reconciliation notes, not the diffs, are the deliverable the maintainer
reviews. Two independent movers flagging the same ambiguity means the
brief is the defect: fix the brief and re-issue rather than adjudicating
the outputs.

## Retiring skills — harvest before you retire

An ending engagement is a harvest trigger, not only a cleanup trigger.
Before retiring any operational skill tied to that engagement, assess each
section as either transferable methodology or client-specific
configuration. If the transferable share is substantial, extract a
client-agnostic skill FIRST and retire the original afterwards, keeping the
client dossier for reference. The extraction pattern that works: keep the
platform mechanics concrete — real interface quirks, DOM patterns, layered
diagnostics, everything earned through real runs and real corrections — and
replace the client's answers (account IDs, domains, source rosters,
criteria) with the intake questions that produced them. The resulting skill
tells the next agent what to ask, rather than what the previous client
happened to say. Principle: the methodology in an operational skill and the
client configuration wrapped around it have very different shelf lives.
Retiring them together discards the durable half at the moment its cost is
already sunk, and the loss is invisible because nothing errors.

## Principle Propagation

When an observation's Principle applies to skills in general, log it with
`Skill: All skills` and surface it; if the user approves, add it to
`[workspace folder]/skill-observations/cross-cutting-principles.md`. That
file is a mandatory checklist during any skill creation or regeneration.
The user chooses propagation timing: immediate (update all skills now — for
things like confidentiality rules) or opportunistic (apply at each skill's
next update).

```markdown
# Cross-Cutting Principles

Principles that apply to all skills. Read as a mandatory checklist during
any skill creation or regeneration.

---

## Active Principles

### 1. [Principle title]
**Added:** [date]
**Applies to:** [all skills | all open-source skills | all skills with rules]
**Requirement:** [what it requires]
**Propagation:** [immediate | opportunistic]
**Status:** [active]
```
