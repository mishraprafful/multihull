# 2026-10-09 Handover session files

- All four open PRs (128, 130, 131, 132) conflicted with `main` only on `docs/handover.md`: every session inserted its entry at the top of "Session log" and a bullet into "Current state", so any two branches collided at the same lines.
- Session entries are now one file each under `docs/handover/log/`, named `YYYY-MM-DD-<topic>.md`; new files never conflict. The entries already in `docs/handover.md` stay there as an archive. `AGENTS.md` steps 1 and 3 point at the directory.
- The two standalone "Test counts" bullets left "Current state" (one was stale, both changed on every PR); counts belong in the session file.
- `.gitattributes` `merge=union` was considered and dropped: GitHub's PR merge does not apply merge drivers, so it would only have helped local merges.
