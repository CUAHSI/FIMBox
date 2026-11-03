# Issue tracker: Jira (CAM)

## Scope and hierarchy

- Site: https://cuahsi.atlassian.net; project: CAM.
- Research Project: CAM-1034 (FIM Sandbox).
- Objective: CAM-1059 (Assist in the Design and Development of the Back-end Architecture for the FIMBox).
- Only touch items under this Objective, or new items created under these rules.
- Every new Epic and Story must have the label FIMBox; preserve existing labels.
- Create Stories under Epics. Create an Epic under CAM-1059 only for several
  related Stories spanning approximately one week to three months.
- Never create Research Projects, Objectives, or Sub-tasks.
- Known Epics (recheck before choosing): CAM-1215, Docker architecture;
  CAM-1061, code/workflow collection; CAM-1476, cloud HAND-FIM performance.
- Ask the user if no Epic clearly fits. Labels alone do not establish scope.
- Order backlogs by Rank: ORDER BY cf[10116] ASC.
- Statuses: To Do, Up Next, In Progress, In Review, Done, Won't Do.

## Jira CLI

Use acli for supported operations. Prefix every command with PAGER=cat;
view also requires piping through cat. Surface failures; verify writes by
reading the changed fields back.

- List Epics: `PAGER=cat acli jira workitem search --jql 'parent = CAM-1059 ORDER BY cf[10116] ASC' --fields 'key,issuetype,summary,status,labels' --csv`.
- List Stories: query `parent = <EPIC-KEY>` with the same fields/order;
  refresh Objective children first instead of relying on the known Epic list.
- Read: `PAGER=cat acli jira workitem view <KEY> --fields 'summary,issuetype,status,parent,labels,description,comment' --json | cat`.
- Create Story: `PAGER=cat acli jira workitem create --project CAM --type Story --parent <EPIC-KEY> --label FIMBox --summary '...' --description-file <file>`.
- Create Epic: use the same command with `--type Epic --parent CAM-1059`.
- Comment: `PAGER=cat acli jira workitem comment create --key <KEY> --body-file <file>`.
- Transition: `PAGER=cat acli jira workitem transition --key <KEY> --status 'In Progress' --yes`.
- Close: comment with the reason, then transition to Done or Won't Do.
- Use temporary plain-text description files; Jira does not render Markdown.

## REST, credentials, scheduling, and dependencies

Use curl REST only when acli cannot perform an operation reliably, including
custom fields, dates, sprint membership, and reading link direction.
Credentials: ~/.jira/netrc; directory permissions 700, file permissions 600.
Never read, print, copy, or commit that file or expose its token. If missing,
ask the user to create it using an editor and a classic Atlassian API token
(not a scoped token). Authenticate acli separately with its browser login.
Before REST use, check permissions without reading the file and verify
GET /rest/api/3/myself; expect HTTP 200, print only displayName or HTTP status.
Use `curl --netrc-file ~/.jira/netrc` against the Jira site with bounded timeouts.

- CAM board: 18; sprint: customfield_10115; start: customfield_10504;
  rank: customfield_10116; due: duedate.
- Read fields via acli view; use REST PUT /rest/api/3/issue/<KEY> with
  {"fields":{...}} for fields unsupported by acli edit.
- Every scheduled item needs both start and due dates. Before overwriting
  either, check GET /rest/api/3/issue/<KEY>/changelog and tell the user if a
  person set it. A blocker's due date must not exceed the blocked item's start.
- Active sprint: `PAGER=cat acli jira board list-sprints --id 18 --state active --json | cat`.
- Add Stories, not Epics, via POST /rest/agile/1.0/sprint/<ID>/issue with
  {"issues":["<STORY-KEY>"]}. Date updates and sprint additions expect HTTP 204.
- Link: `PAGER=cat acli jira workitem link create --out <BLOCKED> --in <BLOCKER> --type Blocks --yes`.
  These flags and CLI success text are misleading: verify using REST
  GET /rest/api/3/issue/<KEY>?fields=issuelinks. An inwardIssue means KEY is
  blocked by that item; an outwardIssue means KEY blocks that item.
- Verify every write by reading affected fields or links back.

## GitHub and publishing

Repository: CUAHSI/FIMBox. GitHub is only for source and PRs; never create GitHub issues.
Work on a Story branch from latest main: <KEY>-<short-lowercase-description>.
Preserve dirty worktrees; never switch branches or pull over unrelated changes.
Open PRs with gh pr create --base main; include the Jira key in the PR title
and link the Story in the body. Transition the Story to In Review after opening.
PRs as a request surface: no.

When a skill says publish to the issue tracker, create a labelled Jira Story
under a verified Epic in CAM-1059, or an Epic under CAM-1059 for multi-Story work.
When it says fetch a ticket, use Jira for CAM keys; bare # numbers identify
GitHub PRs or historical GitHub issues, not new Jira work.

## Wayfinding

If a wayfinding skill is installed later, its map is an Epic under CAM-1059
with FIMBox and wayfinder:map labels. Children are Stories with FIMBox and
wayfinder:<type> labels. Query unassigned open children in Rank order and
check Blocks links; blockers must be Done or Won't Do. Claim by assigning
@me, then transition to In Progress. Resolve with a comment, status change,
and a summary/key pointer in the map's Decisions-so-far.
