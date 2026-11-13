# DG's local Hermes patches

Branch `dg/local-patches` in this fork holds every change we make to Hermes. The installed copy at `~/.hermes/hermes-agent` stays a normal, updatable install.

## Workflow

1. Make changes here on `dg/local-patches` and commit them.
2. Run `dg/reapply.sh` to apply them to the installed Hermes. It reverts whatever it applied last time (saved in `~/.hermes/hermes-agent/.git/dg-applied.patch`), then applies the current diff, so running it again after a new commit is always safe.
3. Restart **both** the Hermes TUI (`/exit`, then `hermes --continue`) and the gateway (`hermes gateway restart`). `tools/voice_mode.py` and `tools/tts_tool_speaker.py` run in both processes.

`dg/reapply.sh --check` is a dry run that changes nothing. `dg/reapply.sh --revert` removes our patch from the install.

`refs/dg/base` records which upstream commit the patches sit on. The script moves it forward after each rebase.

`dg/` and `tests/` are never shipped into the install.

## Updating Hermes

1. **Before** `hermes update`: run `dg/reapply.sh --revert` (or answer **n** at the updater's "Keep these changes after updating?" prompt).
2. Run `hermes update`.
3. Run `dg/reapply.sh`. It rebases our commits onto the new version, pushes the branch to the fork, and re-applies the changes. If a rebase conflicts, it stops and tells you how to finish.
4. Restart the TUI and the gateway (see above).

If you forgot step 1 and answered yes, step 3 still works when the restored patch is intact: the script reverse-applies the saved patch first. If it reports a "partial or conflicting copy", follow the printed instructions.

`dg/tests/test_reapply.sh` exercises all of this against a throwaway upstream/fork/install in `$TMPDIR`.

## Patches

(none yet)
