# Configuration

Find the directory with `herdr plugin config-dir hhl.hermes-manager`. On a default macOS
installation it is `~/.config/herdr/plugins/config/hhl.hermes-manager/`. The plugin honors
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, and Herdr's injected plugin config/state paths.

Example `config.json`:

```json
{
  "sidebar_width": 46,
  "conversation_limit": 50,
  "hermes_root": "~/.hermes",
  "hermes_bin": "/path/to/your/hermes",
  "agent_root": "~/.hermes/hermes-agent",
  "profile_labels": {"research": "Research"},
  "profile_cwds": {"research": "~/Projects/research"}
}
```

Only override paths when necessary. `hermes_bin` otherwise comes from PATH, and
`agent_root` defaults to `hermes_root/hermes-agent`. Profile names come from your local
Hermes install. Labels prefer explicit overrides, then public `display_name` metadata,
then a readable profile name. The default working directory is your home directory;
set `profile_cwds` to choose project folders. Existing workspaces retain their labels/CWDs.

Widths are clamped to 30–80 columns and adjust to available space. The recent-chat limit
is clamped to 10–200. Lists remain scrollable. Small panels page the Bots grid as selection
moves; use `b`, arrows and Enter to reach every bot. Panels shorter than 24 rows show an
instruction to enlarge them rather than overlapping controls.

## Existing custom Spaces layouts

Setup adds the following only if you do not already have `[ui.sidebar.spaces]`:

```toml
[ui.sidebar.spaces]
rows = [
  [{ token = "$hermes-divider", fg = "#7a83a8", bold = true }],
  ["state_icon", "workspace"],
  ["branch", "git_status"],
]
```

If you already customize these rows, insert the `$hermes-divider` row yourself. The plugin
publishes that token only on the first managed workspace. The row above uses Herdr's
standard metadata formatting; no renderer changes are needed.

## Shell setup

`python3 install.py --shell zsh --activate`, `--shell bash`, or `--shell none` selects
which startup file receives the marked source block. zsh honors `ZDOTDIR`; bash uses
`~/.bashrc` (source it from your login profile if your terminal does not already do so).
The shell function follows `runtime.json` in the plugin config directory. Setup or the
startup initializer updates that pointer when the installed checkout changes.

The configuration files and `runtime.json` should remain private: they contain local
paths. The function never rewrites the original Hermes executable. A pre-existing local
profile-restore launcher is preserved if present; it is optional. Advanced installations
may set `hermes_delegate` to an explicit argv array for their existing launcher. Set
`hermes_bin` to the original executable if a third-party wrapper causes a routing loop.

## Updates and live work

Herdr updates replace its own executable; this plugin uses only ordinary panes/APIs, so
there is no custom binary to reinstall. API changes can still require a plugin update.
Run setup after changing plugin versions. Running terminal sessions are not automatically
restarted. `python3 hermes_manager.py refresh` reloads only verified Manager processes.
