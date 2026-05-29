# Carto Documentation

This folder contains the user documentation for the Carto ArcGIS Python toolboxes.

## Persian (فارسی) — primary, complete set

Start here: **[راهنمای کلی Carto](fa/overview/00_Carto_Overview_FA.md)**

- **Overview & guides:** [`fa/overview/`](fa/overview/)
  - [راهنمای کلی](fa/overview/00_Carto_Overview_FA.md)
  - [جدول فهرست افزونه‌ها](fa/overview/01_Plugin_Index_FA.md)
  - [واژه‌نامه](fa/overview/02_Glossary_FA.md)
  - [عیب‌یابی](fa/overview/03_Troubleshooting_FA.md)
  - [پرسش‌های پرتکرار](fa/overview/04_FAQ_FA.md)
  - [مهاجرت از ArcMap به Pro](fa/overview/05_Migration_ArcMap_to_Pro_FA.md)
  - [وضعیت و تغییرات](fa/overview/06_Changelog_Status_FA.md)
- **Per-plugin guides:** [`fa/plugins/`](fa/plugins/) — Plugin 01 … 07
- **Template for new plugins:** [`fa/templates/Plugin_README_Template_FA.md`](fa/templates/Plugin_README_Template_FA.md)

## English — concise navigation summary

- [`en/overview/00_Carto_Overview_EN.md`](en/overview/00_Carto_Overview_EN.md)
- [`en/plugins/README.md`](en/plugins/README.md)

## Related (existing engineering docs, repo root)

- `ARCHITECTURE_SUMMARY.md` — shared design rules across plugins.
- `PROJECT_MAP.md` — file inventory and project map.
- `MIGRATION_PLAN.md` — developer recipe for porting ArcMap → Pro.
- `PLUGIN_06_STATUS.md` — Plugin 06 status and parity notes.

## Tidy file layout

The [`organized/`](../organized/) folder holds tidy **copies** of every plugin grouped by
number and platform (`PluginNN/{ArcMap,Pro,Legacy}`) plus a `Master/` group. The original
`.pyt` files remain in the repository root, untouched, so the Master toolboxes can load them.
