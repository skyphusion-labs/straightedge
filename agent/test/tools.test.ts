import { describe, expect, it } from "vitest";
import { deskTools } from "../src/desk-agent";
import { withDeskEnv } from "./helpers";

/**
 * What tools the model is actually given, and why that is a security control.
 *
 * straightedge#191: `GHSA-hp3w-g68c-fv3c` is a sprintf-js denial of service
 * with **no patched version**, so no bump, no Dependabot PR and no `overrides`
 * pin can resolve it. It is in this Worker's RUNTIME tree, measured with
 * `npm ls sprintf-js --all`:
 *
 *     mt5-risk-agent -> @cloudflare/computer@0.4.0 -> just-bash@3.4.2
 *                    -> sprintf-js@1.1.3
 *
 * `just-bash` is the shell behind `@cloudflare/computer`'s `exec` tool, and
 * sprintf is reached from its `printf` builtin. So the alert is dismissed as
 * unreachable on ONE fact: this Workspace registers no exec backend, so
 * `createAITools` returns no `exec` tool and nothing in this Worker can invoke
 * a shell at all.
 *
 * A dismissal nobody can re-check is how a dismissal rots, and what would
 * quietly invalidate this one is a single added argument: `backends` on the
 * `Workspace` constructor in `desk-agent.ts`. So this enumerates the tool set
 * and fails the moment a shell appears, which is the moment the reasoning
 * stops holding rather than the moment someone notices.
 *
 * It asserts on an ALLOW-LIST rather than `not.toContain("exec")`, because a
 * tool named `shell`, `run` or `bash` would reach `just-bash` just as well and
 * a negative assertion about one name would pass for all the others.
 */

/** Every tool the desk's workspace is allowed to offer. */
const EXPECTED = ["delete", "edit", "find", "grep", "ls", "read", "write"];

/** Any of these in the tool set means a shell is in play. */
const SHELL_TOOLS = ["exec", "shell", "run", "bash", "sh", "command", "process"];

describe("the tool set the model is given", () => {
  it("offers no way to execute a shell, which is why #191 is dismissed", async () => {
    const names = await withDeskEnv("tools-shape", {}, async (instance) =>
      Object.keys(deskTools(instance.workspace)).sort(),
    );

    // Printed so a change in the upstream default is readable in CI output
    // rather than only as a diff against an expectation.
    console.log(`#191: agent tool set = ${names.join(", ")}`);

    expect(names).toEqual(EXPECTED);
    for (const shell of SHELL_TOOLS) {
      expect(names).not.toContain(shell);
    }
  });

  it("would reject a tool set that did contain a shell", () => {
    // The control on the control. The assertion above means something only if
    // a tool set WITH a shell would fail it, so check the predicate against
    // one rather than trusting that it would.
    const withExec = [...EXPECTED, "exec"].sort();
    expect(withExec).not.toEqual(EXPECTED);
    expect(SHELL_TOOLS.some((s) => withExec.includes(s))).toBe(true);
    expect(SHELL_TOOLS.some((s) => EXPECTED.includes(s))).toBe(false);
  });
});
