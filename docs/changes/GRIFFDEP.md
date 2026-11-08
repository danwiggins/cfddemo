- 2026-11-07: `pyBigWig` (pinned 0.3.26) is now in a `griffin` dependency group
  that is synced by default, so a plain `uv sync` installs it and Griffin's ONT
  port no longer skips with `missing_toolchain`. On macOS it builds from the
  source distribution and needs a C compiler; Linux x86_64 uses a wheel.
