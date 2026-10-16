# Windows return work-root owner guard

This independent follow-up starts at `baa4aff959e4a8866f4b71f135a40c1f104d35c1`.
It prevents a newly installed return work root from inheriting an elevated
installer's owner while Hermes runs as the ordinary user. It does not repeat the
production directory repair or change the plugin's authorization checks.

## Behavior and scope

`site_windows.plan` freezes the current caller's ordinary Windows TokenUser and
checks that the unique observed Hermes process has the same TokenUser and is not
elevated. The existing process PID, creation time and executable checks remain.
The installer rejects an elevated caller before acquiring its state lock,
creating directories, or stopping Hermes. Different users or a changed running
identity also reject before the initial stop. The operator must continue using
the verified ordinary Hermes identity; the installer never impersonates,
automatically lowers a token, or configures a new service/account.

Only the return `work_root` lifecycle changes. A missing leaf is created with one
`CreateDirectoryW` call whose security descriptor explicitly specifies that
TokenUser owner and the existing protected OW/SYSTEM full-control DACL. Parent
directories must already exist. Ancestors and the leaf are pinned without delete
sharing; junction/reparse paths reject. The descriptor is then read back.

Existing roots receive no owner, ACL, content, or child changes. They must already
have the exact owner and protected private DACL. An actual filesystem open under
the ordinary installer token requires list/create-file/create-directory,
read-attributes and read-control access. This is a real access operation under
the caller matched to Hermes' observed TokenUser/elevation; it is not simulated
ACL evaluation or a claim that a probe ran inside Hermes. It does not inspect or
rewrite an existing SQLite ledger or any business file.

The reviewed identity is rechecked for apply/start; work-root access is checked
before official start and the newly observed process identity is checked again
before a ready receipt. A failed access check cannot start Hermes. A subsequent
ACL drift does not block stopping an already bound process. Legacy rollback
before apply may have no new work root and remains supported. Global
`enablement._private`, old journal semantics, capability/replay enforcement and
the official management implementation are unchanged. Old reviewed plans lacking
the new identity proof are not silently rewritten or adopted.

## Focused evidence

The final local command uses the existing isolated Gateway test interpreter and
a new short pytest temporary path, avoiding Windows MAX_PATH in long journal
fixture names:

```powershell
& 'C:\Users\Admin\.codex\worktrees\d02d\CF_agent-gateway\.task-deps\gateway-venv\Scripts\python.exe' -B -m pytest tests/test_return_work_root_security.py tests/test_return_site_windows.py -q --tb=short --basetemp C:\Users\Admin\AppData\Local\Temp\cf-og-20261016-r5 -p no:cacheprovider
```

The focused owner module covers elevated and different caller/runtime rejection
before stop, unchanged existing roots, apply-time identity drift, changed started
identity, access drift before start, bound quiescence despite later directory
drift, and legacy rollback before creation. Two Windows-only tests execute real
Win32 calls under a non-elevated TokenUser against new temporary directories:
atomic owner/DACL creation and actual access; repeat preparation preserves root
and child security/content; an inherited unknown DACL is rejected without repair.
Service-phase tests use synthetic managers and no production service or model.

Earlier attempts are preserved: the first lacked the test artifact parent, and
the second encountered WinError 206 only in deep synthetic journal paths.
Switching to a fresh short temporary path yielded 58 passed; the two subsequent
recovery-boundary additions passed in the 10-test owner module. Final exact-source run: **60 passed, 1 existing Starlette deprecation warning,
13.06 seconds, exit 0**. Ruff passed. Final results and
source hashes are recorded locally in `.task-artifacts/owner-guard-validation.json`.

These are isolated Windows/regression results. No production directory, running
Hermes process, credential, CFserver state, or real attachment flow was modified
or exercised by this branch. Remote CI and production adoption remain separate.
