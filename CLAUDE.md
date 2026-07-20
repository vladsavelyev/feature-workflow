# feature-workflow

## Pushing to the remote

This repo was moved out of the `azu-oncology-rd` org (that copy is **archived and read-only**)
to the personal repo **`vladsavelyev/feature-workflow`**. To push:

1. Switch `gh` to the personal account (the default active account may be the org one):
   ```
   gh auth switch --user vladsavelyev
   ```
2. Make sure `origin` points at the moved repo (not the old archived org URL):
   ```
   git remote set-url origin https://github.com/vladsavelyev/feature-workflow.git
   ```

If a push is rejected as non-fast-forward, the moved repo's history was squash-scrubbed on
publish — rebase local commits onto `origin/main` (their trees match the pre-scrub state) rather
than force-pushing full history. Never `git reset --hard`.
