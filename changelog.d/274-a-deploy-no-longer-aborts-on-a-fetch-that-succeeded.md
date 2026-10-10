### A deploy no longer aborts on a `git fetch` that succeeded

- **`Deploy-Desk.ps1` stopped the desk, disabled both supervision tasks, and
  then threw on an ordinary `git fetch`, leaving a real-money box with no desk
  and no supervision.** `git fetch` writes `From https://github.com/...` to
  stderr on every fetch that advances a ref, and on Windows PowerShell 5.1
  native stderr under `$ErrorActionPreference = "Stop"` is a terminating
  `NativeCommandError`, so a fetch that SUCCEEDED aborted the run. The
  mitigation in the file, `$PSNativeCommandUseErrorActionPreference = $false`,
  exists only on PowerShell 7.3 and later; the box has 5.1 and no `pwsh`, so the
  control was present in the script and dead on the only shell it runs.
  **A fetch with nothing to report writes nothing, so the script passed every
  rehearsal against an already-current tree and failed on every deploy that
  moved the tree.**
- **The exit code is now the authority on whether git failed, and stderr is
  not.** `Invoke-Git` moved to `deploy/windows/DeskDeployLib.ps1`, sets
  `$ErrorActionPreference` to `Continue` for its own scope only, and judges
  `$LASTEXITCODE`, which it clears first so a git that never ran cannot inherit
  the previous command's 0. No version test remains in the fix, so there is
  nothing left that can be inert.
- **A half-landed deploy now announces itself and leaves a marker.** A `finally`
  prints the phase reached, whether the tree moved and whether the config
  validated, and re-enables supervision when that is safe: safe when the tree
  never moved, or when it moved and the loader plus `doctor` accepted it. When
  the tree moved and validation refused it the box is left down deliberately,
  because re-enabling would crash-loop a desk on a tree the script already
  rejected. `deploy-in-progress.json` survives a kill or a reboot, where no
  `finally` runs, and the next `-Apply` run refuses to step over it unless the
  operator passes `-PreviousDeployResolved`; `-Rollback` is let through, because
  refusing the recovery tool over the state it recovers from is a lockout.
- **The shell is why CI never saw it.** Every CI step that ran this script used
  `shell: pwsh`, and PowerShell 7 does not turn native stderr into a terminating
  error at all, so the gate could not produce the red.
  `deploy/windows/Test-DeployScripts.ps1` runs under both `shell: powershell`
  (5.1) and `shell: pwsh`, and the 5.1 leg drives the PRE-FIX function red
  before asserting the fix, because a fix that swallowed stderr and a real
  failure alike would satisfy the stderr case on its own.
