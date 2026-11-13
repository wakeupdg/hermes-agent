# DG's local Hermes patches

Branch `dg/local-patches` in this fork holds every change we make to Hermes. The installed copy at `~/.hermes/hermes-agent` stays a normal, updatable install.

## Workflow

1. Make changes here on `dg/local-patches` and commit them.
2. Run `dg/reapply.sh` to apply them to the installed Hermes.
3. After every `hermes update`, run `dg/reapply.sh` again. It rebases our commits onto the new version, pushes the branch to the fork, and re-applies the changes. If a rebase conflicts, it stops and tells you how to finish.

`dg/reapply.sh --check` is a dry run that changes nothing.

`refs/dg/base` records which upstream commit the patches sit on. The script moves it forward after each rebase.

## Patches

(none yet)
