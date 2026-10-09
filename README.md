# Ludusavi Save Backup for Playlite

Requires Playlite 0.2.68+, Ludusavi 0.31+ and PyYAML. The default command uses the Ludusavi Flatpak (`com.github.mtkennerly.ludusavi`). Open Ludusavi and run Preview once before using the plugin. Configure your backup destination, retention, custom save paths and cloud account in Ludusavi.

## Use

- **Settings → Plugins → Ludusavi Save Backup:** enable/disable automatic backups or cloud upload, optionally select a native executable and configuration folder. Opening settings does not change saved preferences.
- **Edit → Installation:** edit save files/folders/patterns and registry rules, override the Ludusavi title or host Wine/Proton prefix, and enable/disable automatic backups per game.
- **Game menu → Ludusavi Save Backup → Back up saves…:** back up just the selected game. With multiple host actions, choose the prefix to use.
- **Inspect detected saves…:** show configured rules, a read-only preview of detected files, progress confirmation and the last recorded backup. Detection alone does not prove a file contains progress.
- **Select multiple games → Ludusavi Save Backup → Edit selected save definitions…:** preview and change automatic backup settings, file rules or registry rules without changing individual titles or prefixes.
- **Save backup status…:** view the latest result for the current Playlite session, retry a backup or open Ludusavi.
- **Restore saves in Ludusavi…:** opens Ludusavi. Choose Restore Mode and review the destination before restoring. Playlite never restores automatically.
- **Configure save locations in Ludusavi…:** opens that game’s Custom Games entry.

Automatic backups run in a serialized background queue after a game launched through Playlite is confirmed stopped. The original chosen action is captured, so selecting another game or changing its actions does not redirect the backup. External launches and closing Playlite do not trigger backups. Manual backups and restore access require the game to be closed.

## Custom save definitions

Use **Sync library save definitions from manifest…** in plugin settings, or **Sync save definition from manifest…** in a game menu. Matching uses Steam ID first, then the title. The plugin copies the cached manifest rules into a custom definition, pins the host prefix and installation folder, and preserves existing additional paths and per-game edits. New entries without detected saves are skipped. Close the Ludusavi GUI before syncing; the configuration is backed up before changes.

Edit the same definition through **Edit → Installation** or **Game menu → Ludusavi Save Backup → Edit save definition…**. Enter one path or pattern per line. Saved edits override automatic manifest rules and are used by backups even before a configuration sync succeeds.

## Host-only saves

The first version supports host Lutris Wine and Steam Proton actions. VM and unknown integration actions are skipped automatically. Each job uses an isolated copy of the Ludusavi configuration with just its host prefix and installation root; VM drive roots and redirects are omitted. Paths containing VM directory markers are rejected if found in the save preview. Configure an explicit host prefix if Proton discovery is ambiguous. Native Linux games without a Wine/Proton prefix are not supported in this first version.

The existing Ludusavi configuration, game saves and Playlite metadata are unchanged by a backup. Only the configured backup folder is written. If a VM writes into the same shared save directory as the host, the origin cannot be distinguished; use separate save directories or exclude that path in Ludusavi.

## Cloud behavior

Local backups use Ludusavi’s configured destination and retention. With cloud synchronization enabled both in Ludusavi and this plugin, the plugin checks the selected game’s previous cloud backup, saves locally, then uploads only that game. Divergent or deleted cloud entries are left untouched. Cloud failures keep the successful local backup and appear in backup status; resolve conflicts or perform an initial upload in Ludusavi when needed. No cloud download or forced conflict resolution occurs automatically.

Do not run manual Ludusavi backups/restores concurrently with queued Playlite backups to the same destination. Playlite jobs are serialized across its installed and repo profiles, but the separate Ludusavi GUI does not share the plugin’s lock.

## Development

From the workspace root:

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=playlite playlite/.dev/venv/bin/python -m unittest discover -s playlite-plugin-ludusavi/tests
python3 playlite-plugin-ludusavi/tools/build_release.py
```

The plugin is available locally; it has not been published to a remote repository or the plugin catalogue.

Save backups run in a serial queue. When Ludusavi is already uploading from its own window or CLI, a queued backup waits for that transfer to finish before checking cloud conflicts and saving. Save backup status updates while waiting and uploading. Plugin uploads have no fixed three-minute cutoff; slow uploads complete before the next queued backup starts.

Backup results persist across restarts. **Playlite menu → Background tasks…** shows queued backups, cloud uploads, definition syncs and save inspections alongside downloads. Retry actions are available for work created in the current session; previous sessions retain their logs. Details are expandable and can be copied.
