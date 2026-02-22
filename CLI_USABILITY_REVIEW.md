# xnatctl CLI Usability Review

Comparison benchmarks: **`gh` (GitHub CLI)**, **`wrangler` (Cloudflare Workers CLI)**, and **Airflow CLI**.

---

## Executive Summary

xnatctl already has a solid foundation: resource-centric `<noun> <verb>` grammar, consistent global flags, profile management, and thoughtful features like `--dry-run` and `--quiet`. The issues below are friction points that separate a *functional* CLI from a *delightful* one like `gh` or `wrangler`.

---

## 1. Positional Arguments vs Named Options Are Inconsistent

**Problem:** The same conceptual thing (identifying a target resource) is sometimes a positional argument and sometimes a required option, with no predictable pattern.

| Command | How you identify the target |
|---|---|
| `subject show SUB001` | Positional argument |
| `session show -E XNAT_E00001` | Required `--experiment/-E` option |
| `scan show -E XNAT_E00001 1` | Mix: `-E` option + positional `SCAN_ID` |
| `resource list XNAT_E00001` | Positional argument |
| `prearchive archive PROJ TS NAME` | Three positional arguments |

**Why it matters:** Users can't predict the syntax. `gh` is consistent: `gh pr view 123`, `gh issue view 456` -- the primary identifier is always the first positional argument. Wrangler does the same: `wrangler d1 info <database-id>`.

**Recommendation:** Adopt a consistent rule: **the primary resource identifier is always a positional argument**. Parent/scope identifiers (`--project`, `--experiment`) stay as options.

```bash
# Current (inconsistent)
xnatctl session show -E XNAT_E00001
xnatctl session download -E XNAT_E00001 --out ./data
xnatctl scan list -E XNAT_E00001

# Proposed (consistent)
xnatctl session show XNAT_E00001
xnatctl session download XNAT_E00001 --out ./data
xnatctl scan list --session XNAT_E00001      # parent scope stays as option
```

The `-E/--experiment` flag name is also confusing since user-facing docs call these "sessions". See point 3.

---

## 2. Short Flag Collisions and Confusing Choices

**Problem:** `-P` means different things in different contexts, and some short flags are unintuitive.

| Flag | Used as | Context |
|---|---|---|
| `-P` | `--project` | subject, session, scan commands |
| `-P` | `--param` | pipeline run, api commands |
| `-p` | `--profile` | global option |
| `-E` | `--experiment` | session/scan (but the command group is called "session") |
| `-S` | `--subject` | session upload |
| `-s` | `--scans` | scan download/delete |
| `-O` | `--option` | admin refresh-catalogs |
| `-r` | `--resource` | scan download |
| `-r` | `--recursive` | dicom validate, local extract |

**Why it matters:** `-P MYPROJ` vs `-P key=value` across commands is a trap. `gh` avoids this: it reserves single letters for consistent meanings globally (e.g., `-R` always means `--repo`).

**Recommendation:**
- Reserve uppercase short flags for globally-consistent meanings: `-P` always means `--project`, `-S` always means `--subject`.
- Drop the `-P` alias for `--param`; just use `--param` or a less ambiguous short form.
- Rename `-E/--experiment` to be consistent with the command group name (see next point).

---

## 3. "Experiment" vs "Session" Naming Confusion

**Problem:** The command group is `session`, but the flag is `--experiment/-E`. The README says "session" everywhere but the API uses "experiment". This is XNAT's internal terminology leaking into user-facing UX.

```bash
xnatctl session show --experiment XNAT_E00001   # "session show --experiment" is confusing
xnatctl session download --experiment XNAT_E00001
```

**How `gh` handles this:** `gh` uses user-facing terms consistently. The GitHub API calls them "pulls", but `gh` uses `pr` everywhere.

**Recommendation:** Since the command group is `session`, the option should be `--session` or simply a positional argument:

```bash
# Option A: positional (preferred, like gh)
xnatctl session show XNAT_E00001

# Option B: consistent naming
xnatctl session show --session XNAT_E00001
```

Keep `--experiment` as a hidden alias for backward compatibility if needed.

---

## 4. Prearchive Commands Require Too Many Positional Arguments

**Problem:** Prearchive commands require **three** positional arguments to identify a session:

```bash
xnatctl prearchive archive MYPROJ 20240115_120000 Session1
xnatctl prearchive move MYPROJ 20240115_120000 Session1 OTHERPROJ
```

The `move` command has **four** positional arguments. Users have to remember the exact order. One mistake and the wrong thing gets deleted.

**How `gh` handles this:** `gh` typically takes one identifier and resolves the rest. `gh pr merge 123` -- just the number.

**Recommendation:**
- Accept a composite identifier: `xnatctl prearchive archive MYPROJ/20240115_120000/Session1`
- Or use a named `--id` option with interactive selection from `prearchive list`
- At minimum, make `timestamp` and `session_name` named options instead of positional:

```bash
xnatctl prearchive archive --project MYPROJ --timestamp 20240115_120000 --name Session1
```

---

## 5. Missing Interactive Affordances

**Problem:** When a required option is missing, the CLI just errors out. Modern CLIs prompt interactively.

```bash
$ xnatctl session download
Error: Missing option '--experiment / -E'.

# vs what gh does:
$ gh pr create
? Title: _                    # interactive prompt
? Body: _
```

**What's already done well:** `config init` prompts for URL. `auth login` prompts for credentials. But this pattern isn't applied elsewhere.

**Recommendation:** For the most common workflows, add interactive fallbacks:
- `session download` without `-E`: prompt or show a list from the default project and let the user pick.
- `subject delete` without a subject: show the subject list.
- At minimum, when `--project` is missing and there's no default, prompt: "Which project?"

---

## 6. `session download` Should Accept a Positional Argument

**Problem:** The most common operation -- downloading a session -- requires a flag:

```bash
xnatctl session download -E XNAT_E00001 --out ./data
```

Compare with natural CLI patterns:

```bash
gh pr checkout 123
wrangler d1 export my-db --output=dump.sql
scp user@host:file ./local
```

**Recommendation:** Make the session ID a positional argument with the option as a fallback:

```bash
xnatctl session download XNAT_E00001 --out ./data
xnatctl session download XNAT_E00001 ./data    # even shorter with positional out
```

---

## 7. `resource` Commands Use Positional Args Where Options Would Be Clearer

**Problem:** Resource commands pack multiple positional arguments:

```bash
xnatctl resource upload XNAT_E00001 NIFTI ./file.nii.gz
xnatctl resource download XNAT_E00001 DICOM -f ./dicom.zip --scan 1
```

Three positional args in `upload` makes it hard to remember the order (is it `SESSION LABEL PATH` or `SESSION PATH LABEL`?).

**Recommendation:**

```bash
# Keep session ID as positional, make label and path clearer
xnatctl resource upload XNAT_E00001 --label NIFTI --file ./file.nii.gz
xnatctl resource upload XNAT_E00001 --label NIFTI ./file.nii.gz   # path as positional, label as option

# Or with a more natural flow:
xnatctl resource upload ./file.nii.gz --to XNAT_E00001/NIFTI
```

---

## 8. Kubernetes-Style Context Names Feel Out of Place

**Problem:** `config use-context` and `config current-context` are borrowed from `kubectl`. While familiar to Kubernetes users, the XNAT audience (neuroscience researchers) is unlikely to have that mental model.

**How `gh` handles this:** No context abstraction at all. Wrangler doesn't use it either. AWS CLI uses `--profile`.

**Recommendation:** Rename to simpler, self-describing names:

```bash
xnatctl config use-context dev    # current
xnatctl config use dev            # proposed (shorter, clearer)

xnatctl config current-context    # current
xnatctl config which              # proposed (or "config active")
```

---

## 9. `--output json|table` Should Also Support `yaml` and `csv`

**Problem:** Only `json` and `table` are available. Researchers working with data often want CSV for spreadsheet import, and YAML for config-file round-tripping.

**How `gh` handles this:** `gh` supports `json` with `--jq` for filtering -- very powerful.

**Recommendation:**
- Add `csv` output (big win for the research audience).
- Consider `--jq` filtering support for JSON output (like `gh`).
- `yaml` is lower priority but nice for config-related commands.

---

## 10. `session upload` Has Too Many Flags

**Problem:** `session upload` has **13 options** beyond the global ones:

```
--project, --subject, --session, --username, --password, --gradual,
--archive-format, --zip-to-tar, --workers, --overwrite, --direct-archive,
--ignore-unparsable, --dry-run
```

Compare: `gh release upload TAG FILES...` has ~4 options.

**Why it matters:** This is an expert-level command being presented as a general-purpose one. New users see the `--help` and feel overwhelmed.

**Recommendation:**
- Keep `--project`, `--subject`, `--session`, `--dry-run` as the primary interface.
- Group the rest under an "Advanced" section in help text (Click supports this via `cls=` or help formatting).
- Use sensible defaults so users rarely need the advanced flags (most defaults already look good).
- Consider subcommands: `session upload` (simple) vs `session upload --mode gradual` or `session upload-advanced`.

---

## 11. No Aliases or Shorthand Commands

**Problem:** There's no way to shorten common operations. Every command requires the full noun-verb form.

```bash
xnatctl project list          # 19 chars
xnatctl session download ...  # 25+ chars
```

**How `gh` handles this:** `gh` has aliases (`gh alias set`). Wrangler has shorthands.

**Recommendation:**
- Add built-in aliases for the most common operations:
  ```bash
  xnatctl ls projects          # alias for project list
  xnatctl dl XNAT_E00001       # alias for session download
  xnatctl up ./dicoms ...      # alias for session upload
  ```
- Or support user-defined aliases in `config.yaml`.

---

## 12. Missing `--format` for `scan download` Resource Filtering

**Problem:** `scan download` uses `--resource` for the resource type (DICOM, NIFTI), but the flag name `--resource` can be confused with the `resource` command group. The short flag `-r` also collides with `--recursive` in other commands.

**Recommendation:** Rename to `--type` or `--format` for clarity:

```bash
xnatctl scan download -E XNAT_E00001 -s 1 --type DICOM
```

---

## 13. `--file` / `--out` / `--name` Inconsistency for Output Paths

**Problem:** Different commands use different option names for the output destination:

| Command | Output option |
|---|---|
| `session download` | `--out` (directory) + `--name` (subdirectory name) |
| `scan download` | `--out` (directory) + `--name` (subdirectory name) |
| `resource download` | `--file/-f` (file path) |

**Recommendation:** Standardize:
- `--out/-o` for output directory (but this collides with `--output/-o` for format!)
- `--dest` or `--dir` for output directory to avoid the `-o` collision
- `--file/-f` for single file output

The collision between `--out` (destination path) and `--output/-o` (format) is particularly confusing. `--output` should be renamed to `--format` to free up `--out/-o` for destination paths.

---

## 14. No `--json` Shorthand

**Problem:** Getting JSON output requires `--output json` or `-o json` (3 extra characters over what `gh` needs).

**How `gh` handles this:** `gh pr list --json title,number` -- `--json` both selects JSON output and lets you pick fields.

**Recommendation:** Add `--json` as a shorthand flag:

```bash
xnatctl project list --json              # equivalent to -o json
xnatctl project list --json id,name      # with field selection (stretch goal)
```

---

## 15. `local` Command Group Is Oddly Placed

**Problem:** `local extract` lives under a `local` top-level group. It only has one subcommand and its relationship to the rest of the CLI is unclear.

**Recommendation:** Move it under `session`:

```bash
xnatctl session extract ./data/XNAT_E00001    # since it extracts session ZIPs
```

Or integrate it into the download flow: `session download --extract` (which already exists as `--unzip`).

---

## 16. Boolean Flag Pairs Are Noisy

**Problem:** Several commands use Click's boolean flag pair syntax:

```
--unzip/--no-unzip
--cleanup/--no-cleanup
--direct-archive/--prearchive
--parallel/--no-parallel
--ignore-unparsable/--no-ignore-unparsable
```

`--ignore-unparsable/--no-ignore-unparsable` is 40 characters of flags. These show up in `--help` and make it harder to scan.

**Recommendation:**
- Use simple flags where the negative is the obvious default: `--unzip` (flag) instead of `--unzip/--no-unzip`.
- For the archive routing, use a choice: `--dest archive|prearchive` instead of `--direct-archive/--prearchive`.
- `--ignore-unparsable` should just be a flag (default True, rarely overridden).

---

## 17. Help Text Could Be More Descriptive

**Problem:** Group-level help is minimal:

```
$ xnatctl session --help
Manage XNAT sessions/experiments.

Commands:
  download      Download session data.
  list          List sessions/experiments in a project.
  show          Show session details including scans and resources.
  upload        Upload DICOM session via REST import.
  upload-dicom  Upload DICOM files via C-STORE network protocol.
```

**How `gh` does it:**

```
$ gh pr --help
Work with GitHub pull requests.

USAGE
  gh pr <command> [flags]

CORE COMMANDS
  create:     Create a pull request
  list:       List pull requests in a repository
  ...

EXAMPLES
  $ gh pr list --label bug
  $ gh pr create --title "my pr"

LEARN MORE
  Use `gh pr <command> --help` for more information about a command.
```

**Recommendation:**
- Add 1-2 quick examples at the group level.
- Add a `LEARN MORE` footer pointing users to subcommand help.
- Add short descriptions that mention the most common use case, not just restate the command name.

---

## Summary: Priority Ranking

| Priority | Issue | Impact |
|---|---|---|
| **High** | #1 Inconsistent positional vs option args | Unpredictable UX, steep learning curve |
| **High** | #3 Experiment vs Session naming | Constant confusion |
| **High** | #6 Download should be positional | Most common operation is harder than needed |
| **High** | #13 `--out` vs `--output` collision | Easy to use wrong one |
| **Medium** | #2 Short flag collisions | Muscle-memory traps |
| **Medium** | #4 Prearchive triple positional | Error-prone for destructive ops |
| **Medium** | #5 Missing interactive prompts | Poor experience for new users |
| **Medium** | #8 Kubernetes context naming | Unfamiliar to target audience |
| **Medium** | #10 Upload has too many flags | Intimidating help output |
| **Medium** | #17 Help text depth | Discoverability |
| **Low** | #7 Resource positional args | Minor confusion |
| **Low** | #9 Output format options | Nice-to-have |
| **Low** | #11 No aliases | Power-user convenience |
| **Low** | #14 No --json shorthand | Minor convenience |
| **Low** | #15 `local` group placement | Organizational nit |
| **Low** | #16 Boolean flag pairs | Help text noise |
| **Low** | #12 --resource naming | Naming nit |
