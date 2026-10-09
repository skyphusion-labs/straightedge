import { createAITools } from "@cloudflare/computer/tools";
import { describe, expect, it } from "vitest";
import { deskTools } from "../src/desk-agent";
import { LOG_READ_MAX_BYTES, LOG_READ_MAX_LINES } from "../src/log-retention";
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
 * quietly invalidate this one is a single added argument: a `shell` option on
 * the `createAITools` call in `deskTools()`. `exec` is gated on that option
 * ALONE: the installed source reads
 * `if (options.shell === void 0) return void 0;` and the type says "Omit for no
 * exec tool". So this enumerates the tool set and fails the moment a shell
 * appears, which is the moment the reasoning stops holding rather than the
 * moment someone notices.
 *
 * NOT `backends` on the `Workspace` constructor, which an earlier version of
 * this file named and which is wrong: that is the half that puts just-bash's
 * CODE in the bundle, and on its own it adds no tool and nothing the model can
 * call. The bundle half is re-checked by the `wrangler deploy --dry-run` grep
 * recorded in `agent/README.md`, not here.
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

  it("rejects a REAL tool set that contains a shell", async () => {
    // The positive control, and it has to build a real tool set to be one.
    //
    // An earlier version of this test compared the two local constants to each
    // other, which cannot go red on the code under test and was therefore
    // decoration in the one file whose whole purpose is re-checkability. This
    // calls `createAITools` with a `shell` option, which is the actual
    // invalidator, and checks that the assertion above REJECTS what comes back.
    const names = await withDeskEnv("tools-shape-control", {}, async (instance) => {
      const tools = createAITools({
        workspace: instance.workspace as unknown as Parameters<
          typeof createAITools
        >[0]["workspace"],
        read: { maxBytes: LOG_READ_MAX_BYTES, maxLines: LOG_READ_MAX_LINES },
        // The only difference from `deskTools()`. A descriptor is enough: the
        // tool appears because `shell` was passed, not because a backend works.
        shell: {
          backends: { control: { label: "control", description: "positive control" } },
          defaultBackend: "control",
        } as unknown as Parameters<typeof createAITools>[0]["shell"],
      });
      return Object.keys(tools).sort();
    });

    console.log(`#191 positive control: with a shell option = ${names.join(", ")}`);

    expect(names).toContain("exec");
    expect(names).not.toEqual(EXPECTED);
    expect(SHELL_TOOLS.some((name) => names.includes(name))).toBe(true);
  });
});
