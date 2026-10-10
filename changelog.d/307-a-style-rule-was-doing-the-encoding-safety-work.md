### A style rule was doing the encoding-safety work (#307)

- **40 text-mode file operations in `tests/` named no encoding, and the thing
  keeping them green on `windows-latest` was the no-em-dash STYLE rule.**
  `read_text()` without `encoding=` decodes through the platform locale, which
  is cp1252 on that leg, and every file this project writes is utf-8. Nothing
  those sites read happened to carry a cp1252-fatal byte, so the suite was
  green on a convention rather than on a guard; the repo already contains such
  a byte (`0x81`, from #197's corpus) one file away from the sites that read.
  All 40 now name `encoding="utf-8"`, including
  `test_the_replace_idiom_is_guarded_everywhere.py:614`, where the encoding-less
  read was the `assert`'s own FAILURE MESSAGE, so the diagnostic would have
  raised instead of printing on the only platform where it mattered.
- **`tests/encoding_census.py` is the guard, because 40 hand-fixed sites rot
  back.** It reads source with `ast`, classifies every call site by its real
  signature position, and FAILS on anything it cannot classify rather than
  skipping it. Collected by `tests/test_the_censuses_have_an_invoker.py`, so it
  gates inside a required context and adds no new required check.
- **Measured, and the reason a lint rule is not the fix:** ruff's own
  `PLW1514` (`unspecified-encoding`) found 6 of the 40. It fires only where it
  can infer a `pathlib.Path` receiver, so it was blind to `path.read_text()` on
  an annotated parameter, to `(tmp_path / "f").read_text()` on a fixture, to
  every `write_text`, and to the `:614` failure-message site. It is also
  preview-only, which would opt a dependabot-bumped `ruff>=` into unstable
  rules.
- **The hazard is now asserted, not cited.** A test proves both halves on
  synthesized bytes: the `UnicodeDecodeError` on a byte undefined in cp1252,
  and the SILENT misread where every byte of a utf-8 Latin-1 character is
  itself defined in cp1252, so an accented word comes back with each
  character split into two and no exception at all.
