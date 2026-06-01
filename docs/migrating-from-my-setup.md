# Upgrading from my-setup v0.x

setforge is the post-rename, post-split form of the older `my-setup` tool. If
you have an existing my-setup checkout, migrate as follows. (If you are not a
prior my-setup user, you can ignore this page.)

## 1. Rename

The Python package, CLI binary, env vars, and XDG dirs all changed
(`my_setup` → `setforge`, `MY_SETUP_` → `SETFORGE_`,
`~/.local/state/my-setup/` → `~/.local/state/setforge/`, etc.). Migrate XDG
state:

```bash
mv ~/.config/my-setup ~/.config/setforge            # if it exists
mv ~/.local/state/my-setup ~/.local/state/setforge  # if it exists
```

## 2. User-section markers in deployed live files

The marker namespace changed from `my-setup:user-section` to
`setforge:user-section`. Run this on every host, per markered live file:

```bash
sed -i 's/my-setup:user-section/setforge:user-section/g' ~/.claude/CLAUDE.md
# repeat for any other live file you installed with markers
```

`setforge install` detects pre-rename markers and refuses to clobber section
bodies, pointing you at this command — but running it preemptively is safer.

## 3. Split the repo

Your old monorepo had engine + config together. Separate them:

- **Option A (clean):** clone the new engine repo afresh, then create or clone a
  config repo containing your `my_setup.yaml` + `tracked/`. You can extract them
  from the old monorepo with full history via
  `git filter-repo --path tracked/ --path my_setup.yaml`.
- **Option B (migrated):** if you are `raulfrk` (the author), your config now
  lives at `git@github.com:raulfrk/setforge-config.git`.

## 4. Rename the config file

setforge expects `setforge.yaml` (was `my_setup.yaml`). In your config repo:

```bash
git mv my_setup.yaml setforge.yaml
git commit -m "Rename my_setup.yaml to setforge.yaml"
```

If you forget, setforge refuses to run with a `ConfigError` pointing at this
exact command.

## 5. Configure the source layer

Point setforge at your config repo — see
[configuration.md](configuration.md#source-discovery). `setforge init` writes
the `local.yaml` `source:` block for you.

## 6. Run

```bash
setforge install --profile=<your-profile>
```

On a host already on the latest my-setup state, this should be a no-op.
