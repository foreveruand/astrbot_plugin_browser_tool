# Changelog

## 1.1.1 - 2026-06-21

- Fix LLM tool permission checks on AstrBot v4.26+ by resolving the real message event from `ContextWrapper`.
- Respect AstrBot per-tool permission configuration for `browse_webpage_tool` while keeping `only_admin` as a legacy fallback when no tool permission is configured.
- Fix custom tool description lookup to use the registered `browse_webpage_tool` name.

## 1.1.0 - 2026-05-17

- Add local `cloakbrowser` support and remove the local `webkit` option.
- Enable storage-state session persistence by default and save it under `data/plugin_data/astrbot_plugin_browser_tool/storage_state.json`.
- Allow `storage_state_path` to be configured as either a JSON file path or a directory path.
