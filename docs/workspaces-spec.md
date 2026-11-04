# CAM-1524: Persistent collaborative workspaces

Status: Implemented contract; live HydroShare/deployment validation remains required.
Issue: [CAM-1524](https://cuahsi.atlassian.net/browse/CAM-1524).

## Goal

Move durable job history from a user-scoped archive into a shared workspace.
After signing in, a user creates or opens a workspace before beginning workspace
work. Another authorized user can open the same workspace, inspect its jobs and
results, and submit new jobs if they have write access.

Completed and failed jobs remain discoverable beyond 24 hours and after a service
restart. Redis remains necessary for execution and transient state, but is not
the durable source of workspace history.

## Baseline before CAM-1524

Pre-workspace repository behavior, not verified against the deployed service:

- Redis tracks jobs by user and expires results, ownership, and logs.
- Optional S3 history already retains queued, running, and terminal records and
  terminal logs. Export also supports completed Redis-only jobs.
- Archives use `<prefix>/users/<SHA256(user_id)>/jobs/`. Both object lookup and
  record validation restrict access to the signed-in user. Selecting the same
  bucket/prefix did not share history between users.
- History and job outputs had independently selected destinations.
- Redis exports cannot reconstruct original inputs, job types, or submission
  timestamps that were not retained.
- Abrupt worker loss can leave the last queued/running archive snapshot.

Updated implementation: [history](../src/fimbox_api/history.py),
[job routes](../src/fimbox_api/main.py), and
[workspace guide](../docker/api/README.md#persistent-s3-workspaces).

The original issue requires user ownership checks. This contract deliberately
replaces submitter-only reads with authorized workspace reads, while preserving
submitter attribution and submitter-only cancellation. Acceptance criteria in
the issue should be updated to reflect that agreed change.

## Agreed decisions

1. **Workspace access follows existing HydroShare/S3 permissions.** FIMBox does
   not introduce a separate membership, invitation, or role-management system.
   Knowing a workspace location is not sufficient to access it.
2. **History, logs, and all new job outputs live within the workspace's
   bucket/prefix.** Users do not choose an unrelated output destination for a
   workspace job.
3. **Only the job submitter can cancel a live job.** Workspace write access does
   not confer cancellation rights over another user's jobs.
4. **Users create or open a workspace before any job work.** Anonymous/local
   workflows are removed. Missing OAuth configuration disables protected work.
5. **Creation requires an empty subprefix beneath
   `<resource_id>/data/contents/`.** Multiple workspaces may share a resource.
6. **No legacy import or shared deletion in this version.** Existing personal
   archives remain untouched; forgetting a saved location does not delete data.
7. **Comparison and parity inputs must be inside the active workspace.**

Reading two existing results or an existing comparison requires read access.
Running a new comparison creates a job and outputs, so it requires write access.

## User workflow

### Create a workspace

1. Sign in.
2. Choose **Create workspace** and enter a name and S3 bucket/prefix.
3. FIMBox validates the location and required storage access, then creates a
   versioned workspace descriptor without overwriting an existing workspace.
4. The workspace becomes active and initially has no jobs.

Creation means initializing an empty subprefix in storage the user can already
access, not provisioning a bucket or granting permissions. Occupied subprefixes
are rejected, including existing workspace descriptors.
Emptiness is checked before the conditional descriptor write. S3 cannot reserve
a prefix transactionally, so unrelated writers must not populate that prefix
during creation.

### Open a workspace

1. Sign in.
2. Choose **Open workspace** and provide its S3 bucket/prefix.
3. FIMBox reads and validates the workspace descriptor and lists its job history
   using the current user's credentials.
4. The user can inspect all accessible workspace jobs, regardless of submitter.

Opening must not mutate the workspace, so read-only access works. A missing,
unsupported, or unreadable descriptor produces an explicit error; opening must
not silently create a workspace or fall back to personal Redis history.

The browser remembers successfully created/opened workspaces per signed-in
account, showing their names and locations most-recent-first when switching.
Opening a remembered workspace revalidates access with the server; a shortcut
grants no access. Forgetting removes only that browser's shortcut, not shared
workspace data. The old generic S3 locations list is not imported.

### Collaborate

Alice creates a workspace and submits a flood inundation job. Bob receives
storage access through HydroShare/S3 and opens the same workspace location.

- Bob sees Alice's job, attribution, available inputs, status, logs, and results.
- With read access, Bob can inspect results and existing comparisons.
- With write access, Bob can submit another run or a new comparison; its outputs
  and history are saved to the same workspace.
- Bob cannot cancel Alice's job, even with write access.
- Both users see jobs submitted by either person, including after Redis expiry.

Sharing a location does not grant storage access. FIMBox should explain that
permission changes happen in the underlying storage system.

### Switch or leave

Switching changes the context of subsequent requests, not the workspace of jobs
already submitted. Running jobs continue writing to their original workspace.
Leaving a workspace or forgetting a recent location does not delete shared data.

## Proposed persistence and execution contract

### Workspace identity and layout

A versioned descriptor identifies the workspace independently of its display
name. The bucket/prefix is the location used to open it.

Implemented layout:

```text
<prefix>/
  workspace.json
  jobs/<job_id>/record.json
  jobs/<job_id>/display-name.json  # only after the name is edited
  jobs/<job_id>/worker.log
  jobs/<job_id>/outputs/...
```

Records include workspace identity, immutable job ID/type, optional user-supplied
display name, submitter identity, original request when available, timestamps,
status, and result/error metadata. Display names may repeat within a workspace;
the job ID remains the unique identifier. A separate `display-name.json`
override keeps post-submission edits from being overwritten by an active
worker's status snapshots; it changes neither the original request nor job ID.
Any workspace user with S3 write access can rename a job. No provider tokens,
credentials, or credential-bearing URLs belong in durable records.

Use independent job objects rather than a single mutable history index.
Concurrent submissions must not overwrite one another's records or outputs.
Descriptor creation must be conditional so concurrent creators cannot replace
an existing workspace.

### Submission and live state

- Every workspace job, including HAND-FIM, parity, and comparison jobs, is bound
  to one workspace at submission.
- The server derives output paths under that workspace; supplied destinations
  outside it are rejected.
- Persist initial metadata before enqueueing. Failure prevents submission.
  An enqueue failure after persistence is recorded explicitly.
- Persist running and terminal updates and terminal logs. Archive failures are
  reported rather than silently presenting the operation as fully successful.
- Record trusted submitter/workspace associations for live authorization.
  Do not authorize cancellation from a client-supplied identity or an editable
  S3 record alone.
- Collaborators can read live state/logs for authorized workspace jobs, not
  just the submitter. A job ID alone does not grant access.
- A selected workspace lists only its own jobs, not a union with personal
  history or jobs from another workspace.
- Requests carry their workspace context explicitly; no server-global active
  workspace. UI, CLI, SDK, and worker behavior must agree.

### Storage permissions and failures

Use the requesting user's storage credentials for workspace reads and new
submissions; do not borrow the creator's credentials.

Storage capabilities may differ by operation and path: listing, reading,
writing, and deleting are not one indivisible permission. Do not equate write
access with delete access or infer workspace-wide write permission from a
successful descriptor read. Exact capability checks and UI affordances need
validation against HydroShare's storage permission model.

Temporary credentials already issued to a running worker may outlive a logout
or permission change. Immediate revocation is not promised; the credential
lifetime and failure behavior are documented in the API guide. Live permission/revocation
semantics still require HydroShare validation.

### Retention and result availability

History has no application-imposed 24-hour expiry. Storage lifecycle policies
and external deletions still apply.

History retention and output availability remain separate guarantees even
though they share a workspace root. If outputs are missing, retain the job
record and show that the result files are unavailable. Do not silently hide the
job or report a computation failure for a successful job with deleted outputs.

A nonterminal archive is a last-known snapshot, not proof that a worker is still
running. Redis loss must not turn an unknown execution state into fabricated
success, failure, or a fresh queued job. Responses retain last-known status with
`execution_state_known: false` when a trusted live binding or task state is
unavailable. Automatic reconciliation is not included.

## Compatibility and migration

- Do not automatically expose existing per-user archives to collaborators.
- Do not reinterpret a legacy history prefix as a workspace without explicit
  initialization/import.
- An explicit import may publish the user's old records into a workspace, but
  requires a separate decision about copying outputs, incomplete provenance,
  permissions, and partial failures.
- Keep unavailable Redis-export metadata explicitly unavailable; do not invent
  original submission details.
- Existing **Clear history** behavior must not silently become deletion of
  everyone's shared history. Shared deletion is undecided and is not included
  in the initial proposed workflow.
- Anonymous/local work is deliberately unsupported. Every protected operation
  requires configured authentication, and job/output operations require a
  validated workspace header.

## Acceptance scenarios

1. Alice creates a workspace; Bob can open it with independently granted access.
2. Alice and Bob can each submit jobs with write access. Both see both users'
   jobs, and all new history/log/output objects remain inside the workspace.
3. A read-only user can open the workspace, inspect jobs, and view existing
   comparisons, but cannot submit a run or a new comparison.
4. A user without storage access cannot open the workspace or access its job
   metadata, logs, or outputs by supplying a location or job ID.
5. Bob cannot cancel Alice's job; Alice can cancel it. A modified archive cannot
   change the trusted submitter for cancellation.
6. Completed and failed jobs remain discoverable after more than 24 hours and
   after service restart/Redis result expiry. Available outputs remain viewable.
7. Missing output files are shown explicitly without removing retained history.
8. Switching workspaces during execution does not redirect records or outputs.
   Job lists and new submissions use only the selected workspace.
9. Concurrent workspace creation cannot overwrite a descriptor; concurrent jobs
   cannot overwrite one another's history or outputs.
10. Storage and enqueue failures are explicit, with no silent personal-history
    fallback. Opening a workspace does not require writes.
11. Legacy archives remain private until an explicit, authorized migration.
12. UI, CLI, and SDK enforce the same workspace and authorization contract.

## API contract and remaining operational validation

- `POST /workspaces` accepts `{location, name}` and creates a version-1
  workspace descriptor with a stable UUID, normalized location, creator, and
  creation time. Conditional writes protect concurrent creation.
- `POST /workspaces/open` accepts `{location}` and reads the descriptor and
  checks listing access without writing.
- Job and output requests require `X-FIMBox-Workspace: s3://bucket/prefix`.
- Version-2 job records use workspace rather than submitter identity for read
  scoping. Cancellation additionally uses server-trusted Redis ownership.
- S3 job listing paginates all storage pages; per-request history response
  pagination/indexing is not yet included.
- Legacy import, shared deletion/rename, and automatic stale-job reconciliation
  are deferred. Comparison inputs outside the workspace are not accepted.

Verify deployed behavior as required by CAM-1524 and update the issue's scope
and acceptance criteria before rollout. Automated storage mocks do not certify
HydroShare permission semantics, conditional-write support, or live worker
credential revocation. No cloud deployment is performed by this change.
